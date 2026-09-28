const multer = require('multer');
const path = require('path');
const fs = require('fs');
const config = require('../config/env');

/**
 * Upload Middleware
 * Multer-based middleware for handling incident image uploads.
 * Stores files in ./uploads/incidents/ with timestamped filenames.
 */

// Ensure upload directory exists
const uploadDir = path.join(config.uploadDir, 'incidents');
if (!fs.existsSync(uploadDir)) {
  fs.mkdirSync(uploadDir, { recursive: true });
}

// Configure storage
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, uploadDir);
  },
  filename: (req, file, cb) => {
    const userId = req.user ? req.user._id : 'unknown';
    const timestamp = Date.now();
    const ext = path.extname(file.originalname).toLowerCase();
    const filename = `incident-${userId}-${timestamp}-${Math.round(Math.random() * 1000)}${ext}`;
    cb(null, filename);
  },
});

// File filter — only allow images
const fileFilter = (req, file, cb) => {
  const allowedTypes = ['image/jpeg', 'image/png', 'image/webp'];
  if (allowedTypes.includes(file.mimetype)) {
    cb(null, true);
  } else {
    cb(
      new Error(
        `Invalid file type: ${file.mimetype}. Only JPEG, PNG, and WebP are allowed.`
      ),
      false
    );
  }
};

// Create multer instance
const upload = multer({
  storage,
  fileFilter,
  limits: {
    fileSize: config.maxFileSize, // Default 10MB
    files: config.incidentImageMaxCount || 5,
  },
});

/**
 * Middleware for uploading incident images.
 * Accepts up to 5 files under the field name 'images'.
 */
const uploadIncidentImages = upload.array('images', config.incidentImageMaxCount || 5);

/**
 * Wrapper middleware that handles multer errors gracefully.
 */
const handleUpload = (req, res, next) => {
  uploadIncidentImages(req, res, (err) => {
    if (err instanceof multer.MulterError) {
      // Multer-specific errors
      if (err.code === 'LIMIT_FILE_SIZE') {
        return res.status(400).json({
          success: false,
          error: `File too large. Maximum size is ${Math.round(config.maxFileSize / (1024 * 1024))}MB.`,
        });
      }
      if (err.code === 'LIMIT_FILE_COUNT' || err.code === 'LIMIT_UNEXPECTED_FILE') {
        return res.status(400).json({
          success: false,
          error: `Too many files. Maximum ${config.incidentImageMaxCount || 5} images allowed.`,
        });
      }
      return res.status(400).json({
        success: false,
        error: `Upload error: ${err.message}`,
      });
    }
    if (err) {
      // Custom file-filter errors or others
      return res.status(400).json({
        success: false,
        error: err.message,
      });
    }
    next();
  });
};

module.exports = { handleUpload, uploadDir };
