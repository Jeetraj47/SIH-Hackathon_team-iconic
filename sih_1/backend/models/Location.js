const mongoose = require('mongoose');

/**
 * Location Model
 * Stores the CURRENT (latest) GPS position for each user.
 * Uses GeoJSON Point for MongoDB 2dsphere geospatial indexing.
 */
const locationSchema = new mongoose.Schema(
  {
    user: {
      type: mongoose.Schema.Types.ObjectId,
      ref: 'User',
      required: [true, 'User reference is required'],
      unique: true, // One location doc per user (upsert pattern)
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
        validate: {
          validator: function (coords) {
            return (
              coords.length === 2 &&
              coords[0] >= -180 &&
              coords[0] <= 180 &&
              coords[1] >= -90 &&
              coords[1] <= 90
            );
          },
          message: 'Coordinates must be [longitude, latitude] with valid ranges',
        },
      },
    },
    altitude: {
      type: Number,
      default: null, // Elevation in meters
    },
    speed: {
      type: Number,
      min: [0, 'Speed cannot be negative'],
      default: 0, // Speed in km/h
    },
    heading: {
      type: Number,
      min: [0, 'Heading must be 0-360'],
      max: [360, 'Heading must be 0-360'],
      default: 0, // Compass bearing in degrees
    },
    accuracy: {
      type: Number,
      min: [0, 'Accuracy cannot be negative'],
      default: null, // GPS accuracy in meters
    },
    source: {
      type: String,
      enum: {
        values: ['gps', 'network', 'mesh'],
        message: 'Source must be gps, network, or mesh',
      },
      default: 'gps',
    },
    isOnline: {
      type: Boolean,
      default: true,
    },
    lastUpdated: {
      type: Date,
      default: Date.now,
    },
  },
  {
    timestamps: true,
  }
);

// Geospatial index for $near / $geoWithin queries
locationSchema.index({ location: '2dsphere' });

// Index for quick user lookup (unique already creates one, but explicit for clarity)
locationSchema.index({ isOnline: 1, lastUpdated: -1 });

module.exports = mongoose.model('Location', locationSchema);
