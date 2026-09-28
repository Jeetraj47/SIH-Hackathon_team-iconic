const config = require('../config/env');

/**
 * Global error handling middleware
 * Catches all errors and returns consistent JSON responses
 */
const errorHandler = (err, req, res, next) => {
  // Log error in development
  if (config.isDev) {
    console.error('❌ Error:', err.message);
    console.error(err.stack);
  }

  // Mongoose validation error
  if (err.name === 'ValidationError') {
    const messages = Object.values(err.errors).map((e) => e.message);
    return res.status(400).json({
      success: false,
      error: 'Validation failed',
      details: messages,
    });
  }

  // Mongoose duplicate key error
  if (err.code === 11000) {
    const field = Object.keys(err.keyValue)[0];
    return res.status(409).json({
      success: false,
      error: `Duplicate value: ${field} already exists`,
    });
  }

  // Mongoose cast error (invalid ObjectId etc.)
  if (err.name === 'CastError') {
    return res.status(400).json({
      success: false,
      error: `Invalid ${err.path}: ${err.value}`,
    });
  }

  // JWT errors
  if (err.name === 'JsonWebTokenError') {
    return res.status(401).json({
      success: false,
      error: 'Invalid token',
    });
  }

  if (err.name === 'TokenExpiredError') {
    return res.status(401).json({
      success: false,
      error: 'Token expired',
    });
  }

  // Multer file upload errors
  if (err.code === 'LIMIT_FILE_SIZE') {
    return res.status(400).json({
      success: false,
      error: 'File too large',
    });
  }

  // Default server error
  const statusCode = err.statusCode || 500;
  res.status(statusCode).json({
    success: false,
    error: config.isDev ? err.message : 'Internal server error',
    ...(config.isDev && { stack: err.stack }),
  });
};

/**
 * 404 handler — must be registered after all routes
 */
const notFound = (req, res) => {
  res.status(404).json({
    success: false,
    error: `Route not found: ${req.method} ${req.originalUrl}`,
  });
};

module.exports = { errorHandler, notFound };
