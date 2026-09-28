const express = require('express');
const { body, query, validationResult } = require('express-validator');
const Location = require('../models/Location');
const LocationHistory = require('../models/LocationHistory');
const User = require('../models/User');
const { auth, authorize } = require('../middleware/auth');
const { buildNearQuery, isValidCoordinate } = require('../utils/geo');
const config = require('../config/env');

const router = express.Router();

// ─── PUT /api/location/update ───────────────────────────────
// Push GPS coordinates — upserts current location + appends to history
router.put(
  '/update',
  auth,
  [
    body('longitude')
      .isFloat({ min: -180, max: 180 })
      .withMessage('Longitude must be between -180 and 180'),
    body('latitude')
      .isFloat({ min: -90, max: 90 })
      .withMessage('Latitude must be between -90 and 90'),
    body('altitude').optional().isFloat().withMessage('Altitude must be a number'),
    body('speed')
      .optional()
      .isFloat({ min: 0 })
      .withMessage('Speed must be >= 0'),
    body('heading')
      .optional()
      .isFloat({ min: 0, max: 360 })
      .withMessage('Heading must be 0-360'),
    body('accuracy')
      .optional()
      .isFloat({ min: 0 })
      .withMessage('Accuracy must be >= 0'),
    body('source')
      .optional()
      .isIn(['gps', 'network', 'mesh'])
      .withMessage('Source must be gps, network, or mesh'),
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
        longitude,
        latitude,
        altitude,
        speed,
        heading,
        accuracy,
        source,
      } = req.body;

      const coordinates = [parseFloat(longitude), parseFloat(latitude)];

      // Upsert current location
      const location = await Location.findOneAndUpdate(
        { user: req.user._id },
        {
          user: req.user._id,
          location: {
            type: 'Point',
            coordinates,
          },
          altitude: altitude || null,
          speed: speed || 0,
          heading: heading || 0,
          accuracy: accuracy || null,
          source: source || 'gps',
          isOnline: true,
          lastUpdated: new Date(),
        },
        { upsert: true, new: true, runValidators: true }
      );

      // Append to history (fire-and-forget for performance)
      LocationHistory.create({
        user: req.user._id,
        location: {
          type: 'Point',
          coordinates,
        },
        speed: speed || 0,
        heading: heading || 0,
        accuracy: accuracy || null,
        source: source || 'gps',
        recordedAt: new Date(),
      }).catch((err) => {
        console.error('⚠️  Failed to save location history:', err.message);
      });

      // Update user's lastKnownLocation as well
      User.findByIdAndUpdate(req.user._id, {
        lastKnownLocation: {
          type: 'Point',
          coordinates,
        },
      }).catch((err) => {
        console.error('⚠️  Failed to update user location:', err.message);
      });

      // Broadcast via Socket.IO
      const io = req.app.get('io');
      if (io) {
        io.to('fleet').emit('location:updated', {
          userId: req.user._id,
          name: req.user.name,
          role: req.user.role,
          coordinates,
          speed: speed || 0,
          heading: heading || 0,
          timestamp: new Date().toISOString(),
        });
      }

      res.json({
        success: true,
        data: { location },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/location/me ───────────────────────────────────
// Get own current location
router.get('/me', auth, async (req, res, next) => {
  try {
    const location = await Location.findOne({ user: req.user._id });

    if (!location) {
      return res.status(404).json({
        success: false,
        error: 'No location data found. Push a GPS update first.',
      });
    }

    res.json({
      success: true,
      data: { location },
    });
  } catch (error) {
    next(error);
  }
});

// ─── GET /api/location/nearby ───────────────────────────────
// Find users within a radius
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

      const locations = await Location.find({
        ...nearQuery,
        isOnline: true,
      })
        .populate('user', 'name email role vehicleId')
        .limit(100);

      res.json({
        success: true,
        count: locations.length,
        radiusKm,
        data: { locations },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/location/history ──────────────────────────────
// Get own location history with pagination and optional date range
router.get('/history', auth, async (req, res, next) => {
  try {
    const page = parseInt(req.query.page, 10) || 1;
    const limit = Math.min(parseInt(req.query.limit, 10) || 50, 200);
    const skip = (page - 1) * limit;

    // Optional date range filters
    const dateFilter = {};
    if (req.query.from) {
      dateFilter.$gte = new Date(req.query.from);
    }
    if (req.query.to) {
      dateFilter.$lte = new Date(req.query.to);
    }

    const filter = { user: req.user._id };
    if (Object.keys(dateFilter).length > 0) {
      filter.recordedAt = dateFilter;
    }

    const [history, total] = await Promise.all([
      LocationHistory.find(filter)
        .sort({ recordedAt: -1 })
        .skip(skip)
        .limit(limit)
        .lean(),
      LocationHistory.countDocuments(filter),
    ]);

    res.json({
      success: true,
      count: history.length,
      total,
      page,
      totalPages: Math.ceil(total / limit),
      data: { history },
    });
  } catch (error) {
    next(error);
  }
});

// ─── GET /api/location/all ──────────────────────────────────
// All online users' locations (fleet view) — admin/official only
router.get(
  '/all',
  auth,
  authorize('admin', 'official'),
  async (req, res, next) => {
    try {
      const filter = {};

      // Optional filters
      if (req.query.online === 'true') {
        filter.isOnline = true;
      }

      const locations = await Location.find(filter)
        .populate('user', 'name email role vehicleId phone')
        .sort({ lastUpdated: -1 })
        .lean();

      res.json({
        success: true,
        count: locations.length,
        data: { locations },
      });
    } catch (error) {
      next(error);
    }
  }
);

module.exports = router;
