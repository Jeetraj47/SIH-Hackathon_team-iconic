const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

/**
 * Centralized environment configuration
 * Validates required env vars and provides defaults
 */
const config = {
  // Server
  port: parseInt(process.env.PORT, 10) || 5000,
  nodeEnv: process.env.NODE_ENV || 'development',
  isDev: (process.env.NODE_ENV || 'development') === 'development',

  // Database
  mongoUri: process.env.MONGODB_URI || 'mongodb://localhost:27017/sih26002',

  // JWT
  jwtSecret: process.env.JWT_SECRET || 'fallback_dev_secret',
  jwtExpiresIn: process.env.JWT_EXPIRES_IN || '7d',

  // CORS
  corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:3000',

  // File Uploads
  uploadDir: process.env.UPLOAD_DIR || './uploads',
  maxFileSize: parseInt(process.env.MAX_FILE_SIZE, 10) || 10 * 1024 * 1024, // 10MB

  // Phase 2 — Location & Mesh
  locationHistoryTtlDays: parseInt(process.env.LOCATION_HISTORY_TTL_DAYS, 10) || 30,
  meshHeartbeatTimeoutMinutes: parseInt(process.env.MESH_HEARTBEAT_TIMEOUT_MINUTES, 10) || 5,
  defaultNearbyRadiusKm: parseFloat(process.env.DEFAULT_NEARBY_RADIUS_KM) || 10,

  // Phase 3 — Incidents
  incidentImageMaxCount: parseInt(process.env.INCIDENT_IMAGE_MAX_COUNT, 10) || 5,
  incidentAutoResolveDays: parseInt(process.env.INCIDENT_AUTO_RESOLVE_DAYS, 10) || 0,

  // External APIs (loaded when needed in later phases)
  openWeatherApiKey: process.env.OPENWEATHER_API_KEY || '',
  googleMapsApiKey: process.env.GOOGLE_MAPS_API_KEY || '',
  sentinelHubApiKey: process.env.SENTINEL_HUB_API_KEY || '',
  nasaEonetApiKey: process.env.NASA_EONET_API_KEY || '',
};

// Validate critical env vars in production
if (config.nodeEnv === 'production') {
  const required = ['JWT_SECRET', 'MONGODB_URI'];
  const missing = required.filter((key) => !process.env[key]);
  if (missing.length > 0) {
    console.error(`❌ Missing required env vars: ${missing.join(', ')}`);
    process.exit(1);
  }
}

module.exports = config;
