const jwt = require('jsonwebtoken');
const config = require('../config/env');
const User = require('../models/User');

/**
 * JWT Authentication Middleware
 * Extracts token from Authorization header, verifies it,
 * and attaches the user to req.user
 */
const auth = async (req, res, next) => {
  try {
    // Get token from header
    const authHeader = req.header('Authorization');
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return res.status(401).json({
        success: false,
        error: 'Access denied. No token provided.',
      });
    }

    const token = authHeader.replace('Bearer ', '');

    // Verify token
    const decoded = jwt.verify(token, config.jwtSecret);

    // Find user and attach to request
    const user = await User.findById(decoded.id).select('-password');
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Token is valid but user no longer exists.',
      });
    }

    req.user = user;
    req.token = token;
    next();
  } catch (error) {
    if (error.name === 'JsonWebTokenError') {
      return res.status(401).json({
        success: false,
        error: 'Invalid token.',
      });
    }
    if (error.name === 'TokenExpiredError') {
      return res.status(401).json({
        success: false,
        error: 'Token expired. Please login again.',
      });
    }
    return res.status(500).json({
      success: false,
      error: 'Authentication error.',
    });
  }
};

/**
 * Role-based authorization middleware
 * Use after auth middleware: router.get('/admin', auth, authorize('admin'), handler)
 */
const authorize = (...roles) => {
  return (req, res, next) => {
    if (!req.user) {
      return res.status(401).json({
        success: false,
        error: 'Authentication required.',
      });
    }
    if (!roles.includes(req.user.role)) {
      return res.status(403).json({
        success: false,
        error: `Access denied. Required role(s): ${roles.join(', ')}`,
      });
    }
    next();
  };
};

module.exports = { auth, authorize };
