const mongoose = require('mongoose');

/**
 * MeshNode Model
 * Represents a physical relay, sensor, or gateway device deployed on a highway.
 * Used for the mesh network infrastructure layer.
 */
const meshNodeSchema = new mongoose.Schema(
  {
    nodeId: {
      type: String,
      required: [true, 'Node ID (hardware identifier) is required'],
      unique: true,
      trim: true,
      uppercase: true,
    },
    name: {
      type: String,
      required: [true, 'Node name is required'],
      trim: true,
      maxlength: [200, 'Name cannot exceed 200 characters'],
    },
    type: {
      type: String,
      enum: {
        values: ['relay', 'sensor', 'gateway'],
        message: 'Type must be relay, sensor, or gateway',
      },
      required: [true, 'Node type is required'],
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
      required: [true, 'Highway segment is required'],
      // e.g., "NH-48 KM 120", "NH-44 KM 350"
    },
    status: {
      type: String,
      enum: {
        values: ['active', 'inactive', 'maintenance'],
        message: 'Status must be active, inactive, or maintenance',
      },
      default: 'active',
    },
    signalStrength: {
      type: Number, // RSSI in dBm (typically -100 to 0)
      default: null,
    },
    batteryLevel: {
      type: Number,
      min: [0, 'Battery level must be 0-100'],
      max: [100, 'Battery level must be 0-100'],
      default: 100,
    },
    firmware: {
      type: String,
      trim: true,
      default: '1.0.0',
    },
    lastHeartbeat: {
      type: Date,
      default: Date.now,
    },
    registeredBy: {
      type: mongoose.Schema.Types.ObjectId,
      ref: 'User',
      required: [true, 'Registered-by user is required'],
    },
    connectedNodes: [
      {
        type: mongoose.Schema.Types.ObjectId,
        ref: 'MeshNode',
      },
    ],
    metadata: {
      type: Map,
      of: mongoose.Schema.Types.Mixed,
      default: new Map(),
      // Flexible key-value store for sensor-specific data
      // e.g., { "sensorType": "temperature", "range": "100m" }
    },
  },
  {
    timestamps: true,
  }
);

// Geospatial index for nearby node queries
meshNodeSchema.index({ location: '2dsphere' });

// Compound indexes for common queries
meshNodeSchema.index({ status: 1, type: 1 });
meshNodeSchema.index({ highway: 1, status: 1 });
meshNodeSchema.index({ lastHeartbeat: 1 });

module.exports = mongoose.model('MeshNode', meshNodeSchema);
