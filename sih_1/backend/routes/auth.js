const express = require('express');
const jwt = require('jsonwebtoken');
const { body, validationResult } = require('express-validator');
const User = require('../models/User');
const { auth } = require('../middleware/auth');
const config = require('../config/env');

const router = express.Router();

/**
 * Generate JWT token for a user
 */
const generateToken = (userId) => {
  return jwt.sign({ id: userId }, config.jwtSecret, {
    expiresIn: config.jwtExpiresIn,
  });
};

// ─── POST /api/auth/register ────────────────────────────────
// Register a new user
router.post(
  '/register',
  [
    body('name')
      .trim()
      .notEmpty()
      .withMessage('Name is required')
      .isLength({ min: 2, max: 100 })
      .withMessage('Name must be 2-100 characters'),
    body('email')
      .trim()
      .isEmail()
      .withMessage('Valid email is required')
      .normalizeEmail(),
    body('password')
      .isLength({ min: 6 })
      .withMessage('Password must be at least 6 characters'),
    body('role')
      .optional()
      .isIn(['driver', 'official', 'admin'])
      .withMessage('Role must be driver, official, or admin'),
    body('phone')
      .optional()
      .matches(/^[0-9]{10}$/)
      .withMessage('Phone must be a valid 10-digit number'),
    body('vehicleId')
      .optional()
      .trim()
      .toUpperCase(),
  ],
  async (req, res, next) => {
    try {
      // Check validation
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const { name, email, password, role, phone, vehicleId } = req.body;

      // Check if user already exists
      const existingUser = await User.findOne({ email });
      if (existingUser) {
        return res.status(409).json({
          success: false,
          error: 'Email already registered',
        });
      }

      // Create user
      const user = await User.create({
        name,
        email,
        password,
        role: role || 'driver',
        phone,
        vehicleId,
      });

      // Generate token
      const token = generateToken(user._id);

      res.status(201).json({
        success: true,
        data: {
          user: user.toJSON(),
          token,
        },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── POST /api/auth/login ───────────────────────────────────
// Login user
router.post(
  '/login',
  [
    body('email')
      .trim()
      .isEmail()
      .withMessage('Valid email is required')
      .normalizeEmail(),
    body('password')
      .notEmpty()
      .withMessage('Password is required'),
  ],
  async (req, res, next) => {
    try {
      // Check validation
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        return res.status(400).json({
          success: false,
          error: 'Validation failed',
          details: errors.array().map((e) => e.msg),
        });
      }

      const { email, password } = req.body;

      // Find user with password field
      const user = await User.findOne({ email }).select('+password');
      if (!user) {
        return res.status(401).json({
          success: false,
          error: 'Invalid email or password',
        });
      }

      // Check if account is active
      if (!user.isActive) {
        return res.status(403).json({
          success: false,
          error: 'Account is deactivated. Contact admin.',
        });
      }

      // Verify password
      const isMatch = await user.comparePassword(password);
      if (!isMatch) {
        return res.status(401).json({
          success: false,
          error: 'Invalid email or password',
        });
      }

      // Update last login
      user.lastLogin = new Date();
      await user.save();

      // Generate token
      const token = generateToken(user._id);

      res.json({
        success: true,
        data: {
          user: user.toJSON(),
          token,
        },
      });
    } catch (error) {
      next(error);
    }
  }
);

// ─── GET /api/auth/me ───────────────────────────────────────
// Get current user profile (protected)
router.get('/me', auth, async (req, res) => {
  res.json({
    success: true,
    data: {
      user: req.user,
    },
  });
});

module.exports = router;
