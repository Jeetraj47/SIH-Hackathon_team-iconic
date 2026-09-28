const mongoose = require('mongoose');

/**
 * Incident Model
 * Represents a reported road incident — accident, pothole, landslide, etc.
 * Integrates with Phase 2 location infrastructure for geo-tagging.
 */
const incidentSchema = new mongoose.Schema(
  {
    reporter: {
      type: mongoose.Schema.Types.ObjectId,
      ref: 'User',
      required: [true, 'Reporter reference is required'],
      index: true,
    },
    title: {
      type: String,
      required: [true, 'Incident title is required'],
      trim: true,
      minlength: [5, 'Title must be at least 5 characters'],
      maxlength: [200, 'Title cannot exceed 200 characters'],
    },
    description: {
      type: String,
      required: [true, 'Incident description is required'],
      trim: true,
      minlength: [10, 'Description must be at least 10 characters'],
      maxlength: [2000, 'Description cannot exceed 2000 characters'],
    },
    category: {
      type: String,
      enum: {
        values: [
          'accident',
          'pothole',
          'landslide',
          'flood',
          'debris',
          'construction',
          'signal_failure',
          'other',
        ],
        message:
          'Category must be one of: accident, pothole, landslide, flood, debris, construction, signal_failure, other',
      },
      required: [true, 'Incident category is required'],
    },
    severity: {
      type: String,
      enum: {
        values: ['low', 'medium', 'high', 'critical'],
        message: 'Severity must be low, medium, high, or critical',
      },
      required: [true, 'Severity level is required'],
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
    highway: {
      type: String,
      trim: true,
      default: '',
      // e.g., "NH-48 KM 120", "NH-44 KM 350"
    },
    images: {
      type: [String],
      validate: {
        validator: function (arr) {
          return arr.length <= 5;
        },
        message: 'Maximum 5 images allowed per incident',
      },
      default: [],
    },
    status: {
      type: String,
      enum: {
        values: ['reported', 'verified', 'in_progress', 'resolved', 'dismissed'],
        message:
          'Status must be reported, verified, in_progress, resolved, or dismissed',
      },
      default: 'reported',
    },
    verifiedBy: {
      type: mongoose.Schema.Types.ObjectId,
      ref: 'User',
      default: null,
    },
    resolvedAt: {
      type: Date,
      default: null,
    },
    affectedLanes: {
      type: Number,
      min: [0, 'Affected lanes cannot be negative'],
      max: [10, 'Affected lanes cannot exceed 10'],
      default: 0,
    },
    estimatedClearTime: {
      type: Date,
      default: null,
    },
    metadata: {
      type: Map,
      of: mongoose.Schema.Types.Mixed,
      default: new Map(),
      // Flexible key-value store for extra data
      // e.g., { "vehiclesInvolved": 3, "injuriesReported": true }
    },
  },
  {
    timestamps: true, // createdAt, updatedAt
  }
);

// ─── Indexes ────────────────────────────────────────────────
// Geospatial index for nearby incident queries
incidentSchema.index({ location: '2dsphere' });

// Dashboard filtering — officials query by highway + status
incidentSchema.index({ highway: 1, status: 1 });

// User's own incidents
incidentSchema.index({ reporter: 1, createdAt: -1 });

// Priority queue for officials — active incidents by severity
incidentSchema.index({ status: 1, severity: 1 });

// Category-based lookups
incidentSchema.index({ category: 1, status: 1 });

module.exports = mongoose.model('Incident', incidentSchema);
