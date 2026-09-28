const express = require('express');
const { body, query, param, validationResult } = require('express-validator');
const MeshNode = require('../models/MeshNode');
const { auth, authorize } = require('../middleware/auth');
const { buildNearQuery, isValidCoordinate } = require('../utils/geo');
const config = require('../config/env');

const router = express.Router();

// ─── POST /api/mesh/register ────────────────────────────────
// Register a new mesh node — admin/official only
router.post(
  '/register',
  auth,
  authorize('admin', 'official'),
  [
    body('nodeId')
      .trim()
      .notEmpty()
      .withMessage('Node ID (hardware identifier) is required'),
    body('name')
      .trim()
      .notEmpty()
      .withMessage('Node name is required')
      .isLength({ max: 200 })
      .withMessage('Name cannot exceed 200 characters'),
    body('type')
      .isIn(['relay', 'sensor', 'gateway'])
      .withMessage('Type must be relay, sensor, or gateway'),
    body('longitude')
      .isFloat({ min: -180, max: 180 })
      .withMessage('Longitude must be between -180 and 180'),
    body('latitude')
      .isFloat({ min: -90, max: 90 })
      .withMessage('Latitude must be between -90 and 90'),
    body('highway')
      .trim()
      .notEmpty()
      .withMessage('Highway segment is required'),
    body('firmware').optional().trim(),
    body('connectedNodes').optional().isArray().withMessage('connectedNodes must be an array'),
  ],
  async (req, res, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const {
        nodeId,
        name,
        type,
        longitude,
        latitude,
        highway,
        firmware,
        connectedNodes,
        metadata,
      } = req.body;

      // Check if node already exists
      const existing = await MeshNode.findOne({ nodeId: nodeId.toUpperCase() });
      if (existing) {
        return res.status(409).json({
          success: false,
          error: `Node with ID ${nodeId} already exists`,
        });
      }

      const node = await MeshNode.create({
        nodeId,
        name,
        type,
        location: {
          type: 'Point',
          coordinates: [parseFloat(longitude), parseFloat(latitude)],
        },
        highway,
        firmware: firmware || '1.0.0',
        registeredBy: req.user._id,
        connectedNodes: connectedNodes || [],
        metadata: metadata || {},
      });

      // Broadcast new node via Socket.IO
      const io = req.app.get('io');
      if (io) {
        io.to('mesh').emit('mesh:nodeRegistered', {
          nodeId: node.nodeId,
          name: node.name,
          type: node.type,
          highway: node.highway,
          coordinates: node.location.coordinates,
          timestamp: new Date().toISOString(),
        });
      }

      res.status(201).json({
        success: true,
        data: { node },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/mesh/nodes ────────────────────────────────────
// List all nodes with optional filters
router.get('/nodes', auth, async (req, res, next) => {
  try {
    const filter = {};

    // Apply optional filters
    if (req.query.status) {
      filter.status = req.query.status;
    }
    if (req.query.type) {
      filter.type = req.query.type;
    }
    if (req.query.highway) {
      filter.highway = { $regex: req.query.highway, $options: 'i' };
    }

    const page = parseInt(req.query.page, 10) || 1;
    const limit = Math.min(parseInt(req.query.limit, 10) || 50, 200);
    const skip = (page - 1) * limit;

    const [nodes, total] = await Promise.all([
      MeshNode.find(filter)
        .populate('registeredBy', 'name email')
        .populate('connectedNodes', 'nodeId name status')
        .sort({ createdAt: -1 })
        .skip(skip)
        .limit(limit)
        .lean(),
      MeshNode.countDocuments(filter),
    ]);

    res.json({
      success: true,
      count: nodes.length,
      total,
      page,
      totalPages: Math.ceil(total / limit),
      data: { nodes },
    });
  } catch (error) {
    next(error);
  }
});

// ─── GET /api/mesh/stats ────────────────────────────────────
// Aggregate stats — admin/official only
router.get(
  '/stats',
  auth,
  authorize('admin', 'official'),
  async (req, res, next) => {
    try {
      const heartbeatTimeout = new Date(
        Date.now() - (config.meshHeartbeatTimeoutMinutes || 5) * 60 * 1000
      );

      const [stats] = await MeshNode.aggregate([
        {
          $facet: {
            overview: [
              {
                $group: {
                  _id: null,
                  total: { $sum: 1 },
                  active: {
                    $sum: { $cond: [{ $eq: ['$status', 'active'] }, 1, 0] },
                  },
                  inactive: {
                    $sum: { $cond: [{ $eq: ['$status', 'inactive'] }, 1, 0] },
                  },
                  maintenance: {
                    $sum: { $cond: [{ $eq: ['$status', 'maintenance'] }, 1, 0] },
                  },
                  avgBattery: { $avg: '$batteryLevel' },
                  avgSignal: { $avg: '$signalStrength' },
                  staleHeartbeats: {
                    $sum: {
                      $cond: [{ $lt: ['$lastHeartbeat', heartbeatTimeout] }, 1, 0],
                    },
                  },
                },
              },
            ],
            byType: [
              {
                $group: {
                  _id: '$type',
                  count: { $sum: 1 },
                  avgBattery: { $avg: '$batteryLevel' },
                },
              },
            ],
            byHighway: [
              {
                $group: {
                  _id: '$highway',
                  count: { $sum: 1 },
                  activeCount: {
                    $sum: { $cond: [{ $eq: ['$status', 'active'] }, 1, 0] },
                  },
                },
              },
              { $sort: { count: -1 } },
              { $limit: 20 },
            ],
          },
        },
      ]);

      res.json({
        success: true,
        data: {
          overview: stats.overview[0] || {
            total: 0,
            active: 0,
            inactive: 0,
            maintenance: 0,
            avgBattery: 0,
            avgSignal: 0,
            staleHeartbeats: 0,
          },
          byType: stats.byType,
          byHighway: stats.byHighway,
        },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/mesh/nodes/nearby ─────────────────────────────
// Find mesh nodes within a radius
router.get(
  '/nodes/nearby',
  auth,
  [
    query('lng')
      .isFloat({ min: -180, max: 180 })
      .withMessage('lng (longitude) is required'),
    query('lat')
      .isFloat({ min: -90, max: 90 })
      .withMessage('lat (latitude) is required'),
    query('radius')
      .optional()
      .isFloat({ min: 0.1, max: 500 })
      .withMessage('Radius must be 0.1 to 500 km'),
  ],
  async (req, res, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const { lng, lat, radius } = req.query;
      const radiusKm = parseFloat(radius) || config.defaultNearbyRadiusKm;

      const nearQuery = buildNearQuery(lng, lat, radiusKm);

      const nodes = await MeshNode.find({
        ...nearQuery,
        status: { $ne: 'inactive' },
      })
        .populate('registeredBy', 'name')
        .limit(100)
        .lean();

      res.json({
        success: true,
        count: nodes.length,
        radiusKm,
        data: { nodes },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/mesh/nodes/:id ────────────────────────────────
// Get single node details
router.get('/nodes/:id', auth, async (req, res, next) => {
  try {
    const node = await MeshNode.findById(req.params.id)
      .populate('registeredBy', 'name email')
      .populate('connectedNodes', 'nodeId name status location');

    if (!node) {
      return res.status(404).json({
        success: false,
        error: 'Mesh node not found',
      });
    }

    res.json({
      success: true,
      data: { node },
    });
  } catch (error) {
    next(error);
  }
});

// ─── PUT /api/mesh/nodes/:id ────────────────────────────────
// Update node info — admin/official only
router.put(
  '/nodes/:id',
  auth,
  authorize('admin', 'official'),
  [
    body('name').optional().trim().isLength({ max: 200 }),
    body('type').optional().isIn(['relay', 'sensor', 'gateway']),
    body('status').optional().isIn(['active', 'inactive', 'maintenance']),
    body('highway').optional().trim(),
    body('firmware').optional().trim(),
    body('longitude').optional().isFloat({ min: -180, max: 180 }),
    body('latitude').optional().isFloat({ min: -90, max: 90 }),
  ],
  async (req, res, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const updateData = {};
      const allowedFields = [
        'name',
        'type',
        'status',
        'highway',
        'firmware',
        'connectedNodes',
        'metadata',
      ];

      allowedFields.forEach((field) => {
        if (req.body[field] !== undefined) {
          updateData[field] = req.body[field];
        }
      });

      // Handle location update if coordinates provided
      if (req.body.longitude !== undefined && req.body.latitude !== undefined) {
        updateData.location = {
          type: 'Point',
          coordinates: [
            parseFloat(req.body.longitude),
            parseFloat(req.body.latitude),
          ],
        };
      }

      const node = await MeshNode.findByIdAndUpdate(
        req.params.id,
        updateData,
        { new: true, runValidators: true }
      );

      if (!node) {
        return res.status(404).json({
          success: false,
          error: 'Mesh node not found',
        });
      }

      // Broadcast status change via Socket.IO
      const io = req.app.get('io');
      if (io && updateData.status) {
        io.to('mesh').emit('mesh:statusChanged', {
          nodeId: node.nodeId,
          name: node.name,
          status: node.status,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        data: { node },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── PUT /api/mesh/nodes/:id/heartbeat ──────────────────────
// Record a heartbeat — admin/official (simulates device check-in)
router.put(
  '/nodes/:id/heartbeat',
  auth,
  authorize('admin', 'official'),
  [
    body('signalStrength').optional().isFloat().withMessage('Signal strength must be a number'),
    body('batteryLevel')
      .optional()
      .isFloat({ min: 0, max: 100 })
      .withMessage('Battery level must be 0-100'),
  ],
  async (req, res, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const updateData = {
        lastHeartbeat: new Date(),
        status: 'active', // Heartbeat implies the node is alive
      };

      if (req.body.signalStrength !== undefined) {
        updateData.signalStrength = req.body.signalStrength;
      }
      if (req.body.batteryLevel !== undefined) {
        updateData.batteryLevel = req.body.batteryLevel;
      }

      const node = await MeshNode.findByIdAndUpdate(
        req.params.id,
        updateData,
        { new: true, runValidators: true }
      );

      if (!node) {
        return res.status(404).json({
          success: false,
          error: 'Mesh node not found',
        });
      }

      // Broadcast heartbeat via Socket.IO
      const io = req.app.get('io');
      if (io) {
        io.to('mesh').emit('mesh:heartbeat', {
          nodeId: node.nodeId,
          name: node.name,
          batteryLevel: node.batteryLevel,
          signalStrength: node.signalStrength,
          lastHeartbeat: node.lastHeartbeat,
        });
      }

      res.json({
        success: true,
        data: { node },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── DELETE /api/mesh/nodes/:id ─────────────────────────────
// Decommission (delete) a node — admin only
router.delete(
  '/nodes/:id',
  auth,
  authorize('admin'),
  async (req, res, next) => {
    try {
      const node = await MeshNode.findById(req.params.id);

      if (!node) {
        return res.status(404).json({
          success: false,
          error: 'Mesh node not found',
        });
      }

      // Remove this node from other nodes' connectedNodes arrays
      await MeshNode.updateMany(
        { connectedNodes: node._id },
        { $pull: { connectedNodes: node._id } }
      );

      await node.deleteOne();

      // Broadcast removal
      const io = req.app.get('io');
      if (io) {
        io.to('mesh').emit('mesh:nodeRemoved', {
          nodeId: node.nodeId,
          name: node.name,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        message: `Node ${node.nodeId} decommissioned successfully`,
      });
    } catch (error) {
      next(error);
    }
  }
);

module.exports = router;
