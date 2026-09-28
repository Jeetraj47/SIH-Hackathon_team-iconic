const express = require('express');
const { body, query, param, validationResult } = require('express-validator');
const mongoose = require('mongoose');
const Incident = require('../models/Incident');
const Location = require('../models/Location');
const { auth, authorize } = require('../middleware/auth');
const { handleUpload } = require('../middleware/uploadMiddleware');
const { buildNearQuery, isValidCoordinate } = require('../utils/geo');
const config = require('../config/env');

const router = express.Router();

// ─── POST /api/incidents ────────────────────────────────────
// Report a new incident (with optional image uploads)
router.post(
  '/',
  auth,
  handleUpload,
  [
    body('title')
      .trim()
      .notEmpty()
      .withMessage('Title is required')
      .isLength({ min: 5, max: 200 })
      .withMessage('Title must be 5-200 characters'),
    body('description')
      .trim()
      .notEmpty()
      .withMessage('Description is required')
      .isLength({ min: 10, max: 2000 })
      .withMessage('Description must be 10-2000 characters'),
    body('category')
      .isIn([
        'accident',
        'pothole',
        'landslide',
        'flood',
        'debris',
        'construction',
        'signal_failure',
        'other',
      ])
      .withMessage('Invalid category'),
    body('severity')
      .isIn(['low', 'medium', 'high', 'critical'])
      .withMessage('Severity must be low, medium, high, or critical'),
    body('longitude')
      .optional()
      .isFloat({ min: -180, max: 180 })
      .withMessage('Longitude must be between -180 and 180'),
    body('latitude')
      .optional()
      .isFloat({ min: -90, max: 90 })
      .withMessage('Latitude must be between -90 and 90'),
    body('highway')
      .optional()
      .trim(),
    body('affectedLanes')
      .optional()
      .isInt({ min: 0, max: 10 })
      .withMessage('Affected lanes must be 0-10'),
    body('estimatedClearTime')
      .optional()
      .isISO8601()
      .withMessage('Estimated clear time must be a valid ISO 8601 date'),
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
        title,
        description,
        category,
        severity,
        longitude,
        latitude,
        highway,
        affectedLanes,
        estimatedClearTime,
      } = req.body;

      // Determine coordinates — explicit or auto from user's current GPS
      let coordinates;
      if (longitude !== undefined && latitude !== undefined) {
        coordinates = [parseFloat(longitude), parseFloat(latitude)];
      } else {
        // Auto-read from user's last known location (Phase 2)
        const userLocation = await Location.findOne({ user: req.user._id });
        if (userLocation && userLocation.location && userLocation.location.coordinates) {
          coordinates = userLocation.location.coordinates;
        } else {
          return res.status(400).json({
            success: false,
            error:
              'No coordinates provided and no GPS location found. Push a location update first or provide longitude/latitude.',
          });
        }
      }

      // Collect uploaded image paths
      const images = req.files
        ? req.files.map((file) => file.path.replace(/\\/g, '/'))
        : [];

      // Parse metadata if provided as JSON string
      let metadata = {};
      if (req.body.metadata) {
        try {
          metadata =
            typeof req.body.metadata === 'string'
              ? JSON.parse(req.body.metadata)
              : req.body.metadata;
        } catch {
          // Ignore invalid metadata JSON
        }
      }

      const incident = await Incident.create({
        reporter: req.user._id,
        title,
        description,
        category,
        severity,
        location: {
          type: 'Point',
          coordinates,
        },
        highway: highway || '',
        images,
        affectedLanes: affectedLanes || 0,
        estimatedClearTime: estimatedClearTime || null,
        metadata,
      });

      // Populate reporter info for the response
      await incident.populate('reporter', 'name email role');

      // ─── Socket.IO: Broadcast new incident ───────────────
      const io = req.app.get('io');
      if (io) {
        const payload = {
          incidentId: incident._id,
          title: incident.title,
          category: incident.category,
          severity: incident.severity,
          coordinates: incident.location.coordinates,
          highway: incident.highway,
          reporter: {
            name: req.user.name,
            role: req.user.role,
          },
          timestamp: incident.createdAt,
        };

        // Broadcast to the incidents monitoring room
        io.to('incidents').emit('incident:new', payload);

        // For high/critical severity — also broadcast to fleet + highway rooms
        if (severity === 'high' || severity === 'critical') {
          io.to('fleet').emit('incident:alert', payload);

          if (highway) {
            io.to(`highway:${highway.trim()}`).emit('incident:alert', payload);
          }
        }
      }

      res.status(201).json({
        success: true,
        data: { incident },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/incidents ─────────────────────────────────────
// List incidents with optional filters + pagination
router.get('/', auth, async (req, res, next) => {
  try {
    const filter = {};

    // Apply optional filters
    if (req.query.category) {
      filter.category = req.query.category;
    }
    if (req.query.severity) {
      filter.severity = req.query.severity;
    }
    if (req.query.status) {
      filter.status = req.query.status;
    }
    if (req.query.highway) {
      filter.highway = { $regex: req.query.highway, $options: 'i' };
    }
    if (req.query.reporter) {
      filter.reporter = req.query.reporter;
    }

    // Date range filter
    if (req.query.from || req.query.to) {
      filter.createdAt = {};
      if (req.query.from) {
        filter.createdAt.$gte = new Date(req.query.from);
      }
      if (req.query.to) {
        filter.createdAt.$lte = new Date(req.query.to);
      }
    }

    const page = parseInt(req.query.page, 10) || 1;
    const limit = Math.min(parseInt(req.query.limit, 10) || 20, 100);
    const skip = (page - 1) * limit;

    // Sort: critical/high first, then by newest
    const sort = req.query.sort === 'newest'
      ? { createdAt: -1 }
      : { severity: 1, createdAt: -1 }; // severity enum order: critical < high < low < medium (alphabetical) — we handle this better via aggregation if needed

    const [incidents, total] = await Promise.all([
      Incident.find(filter)
        .populate('reporter', 'name email role')
        .populate('verifiedBy', 'name email')
        .sort({ createdAt: -1 })
        .skip(skip)
        .limit(limit)
        .lean(),
      Incident.countDocuments(filter),
    ]);

    res.json({
      success: true,
      count: incidents.length,
      total,
      page,
      totalPages: Math.ceil(total / limit),
      data: { incidents },
    });
  } catch (error) {
    next(error);
  }
});

// ─── GET /api/incidents/nearby ──────────────────────────────
// Find incidents near a location
router.get(
  '/nearby',
  auth,
  [
    query('lng')
      .isFloat({ min: -180, max: 180 })
      .withMessage('lng (longitude) is required and must be -180 to 180'),
    query('lat')
      .isFloat({ min: -90, max: 90 })
      .withMessage('lat (latitude) is required and must be -90 to 90'),
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

      // Only show active (non-resolved/dismissed) incidents by default
      const statusFilter = req.query.includeResolved === 'true'
        ? {}
        : { status: { $nin: ['resolved', 'dismissed'] } };

      const incidents = await Incident.find({
        ...nearQuery,
        ...statusFilter,
      })
        .populate('reporter', 'name role')
        .limit(100)
        .lean();

      res.json({
        success: true,
        count: incidents.length,
        radiusKm,
        data: { incidents },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/incidents/stats ───────────────────────────────
// Aggregate statistics — admin/official only
router.get(
  '/stats',
  auth,
  authorize('admin', 'official'),
  async (req, res, next) => {
    try {
      const [stats] = await Incident.aggregate([
        {
          $facet: {
            overview: [
              {
                $group: {
                  _id: null,
                  total: { $sum: 1 },
                  reported: {
                    $sum: { $cond: [{ $eq: ['$status', 'reported'] }, 1, 0] },
                  },
                  verified: {
                    $sum: { $cond: [{ $eq: ['$status', 'verified'] }, 1, 0] },
                  },
                  inProgress: {
                    $sum: { $cond: [{ $eq: ['$status', 'in_progress'] }, 1, 0] },
                  },
                  resolved: {
                    $sum: { $cond: [{ $eq: ['$status', 'resolved'] }, 1, 0] },
                  },
                  dismissed: {
                    $sum: { $cond: [{ $eq: ['$status', 'dismissed'] }, 1, 0] },
                  },
                },
              },
            ],
            byCategory: [
              {
                $group: {
                  _id: '$category',
                  count: { $sum: 1 },
                  activeCount: {
                    $sum: {
                      $cond: [
                        { $in: ['$status', ['reported', 'verified', 'in_progress']] },
                        1,
                        0,
                      ],
                    },
                  },
                },
              },
              { $sort: { count: -1 } },
            ],
            bySeverity: [
              {
                $group: {
                  _id: '$severity',
                  count: { $sum: 1 },
                },
              },
            ],
            byHighway: [
              {
                $match: { highway: { $ne: '' } },
              },
              {
                $group: {
                  _id: '$highway',
                  count: { $sum: 1 },
                  activeCount: {
                    $sum: {
                      $cond: [
                        { $in: ['$status', ['reported', 'verified', 'in_progress']] },
                        1,
                        0,
                      ],
                    },
                  },
                  criticalCount: {
                    $sum: { $cond: [{ $eq: ['$severity', 'critical'] }, 1, 0] },
                  },
                },
              },
              { $sort: { activeCount: -1 } },
              { $limit: 20 },
            ],
            recentCritical: [
              {
                $match: {
                  severity: { $in: ['high', 'critical'] },
                  status: { $nin: ['resolved', 'dismissed'] },
                },
              },
              { $sort: { createdAt: -1 } },
              { $limit: 10 },
              {
                $project: {
                  title: 1,
                  category: 1,
                  severity: 1,
                  highway: 1,
                  status: 1,
                  createdAt: 1,
                  'location.coordinates': 1,
                },
              },
            ],
          },
        },
      ]);

      res.json({
        success: true,
        data: {
          overview: stats.overview[0] || {
            total: 0,
            reported: 0,
            verified: 0,
            inProgress: 0,
            resolved: 0,
            dismissed: 0,
          },
          byCategory: stats.byCategory,
          bySeverity: stats.bySeverity,
          byHighway: stats.byHighway,
          recentCritical: stats.recentCritical,
        },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/incidents/:id ─────────────────────────────────
// Get single incident details
router.get('/:id', auth, async (req, res, next) => {
  try {
    if (!mongoose.Types.ObjectId.isValid(req.params.id)) {
      return res.status(400).json({
        success: false,
        error: 'Invalid incident ID',
      });
    }

    const incident = await Incident.findById(req.params.id)
      .populate('reporter', 'name email role phone')
      .populate('verifiedBy', 'name email role');

    if (!incident) {
      return res.status(404).json({
        success: false,
        error: 'Incident not found',
      });
    }

    res.json({
      success: true,
      data: { incident },
    });
  } catch (error) {
    next(error);
  }
});

// ─── PUT /api/incidents/:id ─────────────────────────────────
// Update incident — admin/official only
router.put(
  '/:id',
  auth,
  authorize('admin', 'official'),
  [
    body('title')
      .optional()
      .trim()
      .isLength({ min: 5, max: 200 })
      .withMessage('Title must be 5-200 characters'),
    body('description')
      .optional()
      .trim()
      .isLength({ min: 10, max: 2000 })
      .withMessage('Description must be 10-2000 characters'),
    body('severity')
      .optional()
      .isIn(['low', 'medium', 'high', 'critical'])
      .withMessage('Invalid severity'),
    body('status')
      .optional()
      .isIn(['reported', 'verified', 'in_progress', 'resolved', 'dismissed'])
      .withMessage('Invalid status'),
    body('affectedLanes')
      .optional()
      .isInt({ min: 0, max: 10 })
      .withMessage('Affected lanes must be 0-10'),
    body('estimatedClearTime')
      .optional()
      .isISO8601()
      .withMessage('Invalid date format'),
    body('highway')
      .optional()
      .trim(),
  ],
  async (req, res, next) => {
    try {
      if (!mongoose.Types.ObjectId.isValid(req.params.id)) {
        return res.status(400).json({
          success: false,
          error: 'Invalid incident ID',
        });
      }

      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const allowedFields = [
        'title',
        'description',
        'severity',
        'status',
        'affectedLanes',
        'estimatedClearTime',
        'highway',
      ];

      const updateData = {};
      allowedFields.forEach((field) => {
        if (req.body[field] !== undefined) {
          updateData[field] = req.body[field];
        }
      });

      // If status is being set to 'resolved', record the timestamp
      if (updateData.status === 'resolved') {
        updateData.resolvedAt = new Date();
      }

      const incident = await Incident.findByIdAndUpdate(
        req.params.id,
        updateData,
        { new: true, runValidators: true }
      )
        .populate('reporter', 'name email role')
        .populate('verifiedBy', 'name email');

      if (!incident) {
        return res.status(404).json({
          success: false,
          error: 'Incident not found',
        });
      }

      // ─── Socket.IO: Broadcast update ─────────────────────
      const io = req.app.get('io');
      if (io) {
        io.to('incidents').emit('incident:updated', {
          incidentId: incident._id,
          title: incident.title,
          status: incident.status,
          severity: incident.severity,
          highway: incident.highway,
          updatedBy: req.user.name,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        data: { incident },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── PUT /api/incidents/:id/verify ──────────────────────────
// Mark incident as verified — admin/official only
router.put(
  '/:id/verify',
  auth,
  authorize('admin', 'official'),
  async (req, res, next) => {
    try {
      if (!mongoose.Types.ObjectId.isValid(req.params.id)) {
        return res.status(400).json({
          success: false,
          error: 'Invalid incident ID',
        });
      }

      const incident = await Incident.findById(req.params.id);

      if (!incident) {
        return res.status(404).json({
          success: false,
          error: 'Incident not found',
        });
      }

      if (incident.status !== 'reported') {
        return res.status(400).json({
          success: false,
          error: `Cannot verify an incident with status '${incident.status}'. Only 'reported' incidents can be verified.`,
        });
      }

      incident.status = 'verified';
      incident.verifiedBy = req.user._id;
      await incident.save();

      await incident.populate('reporter', 'name email role');
      await incident.populate('verifiedBy', 'name email');

      // ─── Socket.IO: Broadcast verification ───────────────
      const io = req.app.get('io');
      if (io) {
        io.to('incidents').emit('incident:updated', {
          incidentId: incident._id,
          title: incident.title,
          status: 'verified',
          severity: incident.severity,
          highway: incident.highway,
          verifiedBy: req.user.name,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        message: 'Incident verified successfully',
        data: { incident },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── PUT /api/incidents/:id/resolve ─────────────────────────
// Mark incident as resolved — admin/official only
router.put(
  '/:id/resolve',
  auth,
  authorize('admin', 'official'),
  async (req, res, next) => {
    try {
      if (!mongoose.Types.ObjectId.isValid(req.params.id)) {
        return res.status(400).json({
          success: false,
          error: 'Invalid incident ID',
        });
      }

      const incident = await Incident.findById(req.params.id);

      if (!incident) {
        return res.status(404).json({
          success: false,
          error: 'Incident not found',
        });
      }

      if (incident.status === 'resolved' || incident.status === 'dismissed') {
        return res.status(400).json({
          success: false,
          error: `Incident is already '${incident.status}'.`,
        });
      }

      incident.status = 'resolved';
      incident.resolvedAt = new Date();
      await incident.save();

      await incident.populate('reporter', 'name email role');
      await incident.populate('verifiedBy', 'name email');

      // ─── Socket.IO: Broadcast resolution ─────────────────
      const io = req.app.get('io');
      if (io) {
        const payload = {
          incidentId: incident._id,
          title: incident.title,
          status: 'resolved',
          highway: incident.highway,
          resolvedBy: req.user.name,
          timestamp: new Date().toISOString(),
        };

        io.to('incidents').emit('incident:resolved', payload);

        // Notify fleet and highway rooms
        io.to('fleet').emit('incident:resolved', payload);
        if (incident.highway) {
          io.to(`highway:${incident.highway.trim()}`).emit('incident:resolved', payload);
        }
      }

      res.json({
        success: true,
        message: 'Incident resolved successfully',
        data: { incident },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── DELETE /api/incidents/:id ──────────────────────────────
// Delete an incident — admin only
router.delete(
  '/:id',
  auth,
  authorize('admin'),
  async (req, res, next) => {
    try {
      if (!mongoose.Types.ObjectId.isValid(req.params.id)) {
        return res.status(400).json({
          success: false,
          error: 'Invalid incident ID',
        });
      }

      const incident = await Incident.findById(req.params.id);

      if (!incident) {
        return res.status(404).json({
          success: false,
          error: 'Incident not found',
        });
      }

      await incident.deleteOne();

      // Broadcast removal
      const io = req.app.get('io');
      if (io) {
        io.to('incidents').emit('incident:deleted', {
          incidentId: incident._id,
          title: incident.title,
          deletedBy: req.user.name,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        message: `Incident "${incident.title}" deleted successfully`,
      });
    } catch (error) {
      next(error);
    }
  }
);

module.exports = router;
