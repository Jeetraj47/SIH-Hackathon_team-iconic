const mongoose = require('mongoose');
const bcrypt = require('bcryptjs');

/**
 * User Model
 * Supports roles: driver (truck driver), official (road inspector), admin
 */
const userSchema = new mongoose.Schema(
  {
    name: {
      type: String,
      required: [true, 'Name is required'],
      trim: true,
      minlength: [2, 'Name must be at least 2 characters'],
      maxlength: [100, 'Name cannot exceed 100 characters'],
    },
    email: {
      type: String,
      required: [true, 'Email is required'],
      unique: true,
      lowercase: true,
      trim: true,
      match: [/^\S+@\S+\.\S+$/, 'Please enter a valid email'],
    },
    password: {
      type: String,
      required: [true, 'Password is required'],
      minlength: [6, 'Password must be at least 6 characters'],
      select: false, // Don't include password in queries by default
    },
    role: {
      type: String,
      enum: {
        values: ['driver', 'official', 'admin'],
        message: 'Role must be driver, official, or admin',
      },
      default: 'driver',
    },
    phone: {
      type: String,
      trim: true,
      match: [/^[0-9]{10}$/, 'Please enter a valid 10-digit phone number'],
    },
    vehicleId: {
      type: String,
      trim: true,
      uppercase: true,
      // Only required for drivers, validated at route level
    },
    isActive: {
      type: Boolean,
      default: true,
    },
    lastLogin: {
      type: Date,
    },
    // GPS coordinates of last known position
    lastKnownLocation: {
      type: {
        type: String,
        enum: ['Point'],
      },
      coordinates: {
        type: [Number], // [longitude, latitude]
      },
    },
  },
  {
    timestamps: true, // adds createdAt and updatedAt
  }
);

// Index for geospatial queries (needed for Phase 2 fleet tracking)
userSchema.index({ lastKnownLocation: '2dsphere' });

// Hash password before saving
userSchema.pre('save', async function (next) {
  // Only hash if password is modified
  if (!this.isModified('password')) return next();

  const salt = await bcrypt.genSalt(12);
  this.password = await bcrypt.hash(this.password, salt);
  next();
});

// Compare password method
userSchema.methods.comparePassword = async function (candidatePassword) {
  return bcrypt.compare(candidatePassword, this.password);
};

// Remove sensitive data when converting to JSON
userSchema.methods.toJSON = function () {
  const user = this.toObject();
  delete user.password;
  delete user.__v;
  return user;
};

module.exports = mongoose.model('User', userSchema);
