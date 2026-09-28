const mongoose = require('mongoose');
const config = require('./env');

/**
 * Connect to MongoDB with retry logic
 */
const connectDB = async () => {
  try {
    const conn = await mongoose.connect(config.mongoUri, {
      // Modern mongoose defaults handle most options
    });

    console.log(`✅ MongoDB connected: ${conn.connection.host}/${conn.connection.name}`);

    // Handle connection events
    mongoose.connection.on('error', (err) => {
      console.error(`❌ MongoDB connection error: ${err.message}`);
    });

    mongoose.connection.on('disconnected', () => {
      console.warn('⚠️  MongoDB disconnected. Attempting reconnect...');
    });

    mongoose.connection.on('reconnected', () => {
      console.log('✅ MongoDB reconnected');
    });

    return conn;
  } catch (error) {
    console.error(`❌ MongoDB connection failed: ${error.message}`);
    // Retry after 5 seconds
    console.log('🔄 Retrying connection in 5 seconds...');
    await new Promise((resolve) => setTimeout(resolve, 5000));
    return connectDB();
  }
};

module.exports = connectDB;
