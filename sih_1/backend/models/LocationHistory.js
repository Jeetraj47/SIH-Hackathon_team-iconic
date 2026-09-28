const mongoose = require('mongoose');

/**
 * LocationHistory Model
 * Time-series log of GPS breadcrumbs for route replay and analytics.
 * Documents auto-expire after a configurable TTL (default 30 days).
 */
const locationHistorySchema = new mongoose.Schema({
  user: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'User',
    required: [true, 'User reference is required'],
    index: true,
  },
  location: {
    type: {
      type: String,
      enum: ['Point'],
      required: true,
      default: 'Point',
    },
    coordinates: {
      type: [Number], // [longitude, latitude]
      required: [true, 'Coordinates are required'],
    },
  },
  speed: {
    type: Number,
    min: 0,
    default: 0,
  },
  heading: {
    type: Number,
    min: 0,
    max: 360,
    default: 0,
  },
  accuracy: {
    type: Number,
    min: 0,
    default: null,
  },
  source: {
    type: String,
    enum: ['gps', 'network', 'mesh'],
    default: 'gps',
  },
  recordedAt: {
    type: Date,
    default: Date.now,
    // Indexed via the TTL index below — no inline index needed
  },
});

// Geospatial index for spatial queries on historical data
locationHistorySchema.index({ location: '2dsphere' });

// Compound index for efficient user + time range queries
locationHistorySchema.index({ user: 1, recordedAt: -1 });

// TTL index — auto-delete documents after 30 days (configurable via env)
const ttlDays = parseInt(process.env.LOCATION_HISTORY_TTL_DAYS, 10) || 30;
locationHistorySchema.index(
  { recordedAt: 1 },
  { expireAfterSeconds: ttlDays * 24 * 60 * 60 }
);

module.exports = mongoose.model('LocationHistory', locationHistorySchema);
