const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const morgan = require('morgan');
const { createServer } = require('http');
const { Server } = require('socket.io');

// Load config first (loads .env)
const config = require('./config/env');
const connectDB = require('./config/database');
const { errorHandler, notFound } = require('./middleware/errorHandler');

// Import routes
const authRoutes = require('./routes/auth');
const locationRoutes = require('./routes/location');
const meshRoutes = require('./routes/mesh');
const incidentRoutes = require('./routes/incident');

// ─── Initialize Express ─────────────────────────────────────
const app = express();
const httpServer = createServer(app);

// ─── Initialize Socket.IO ───────────────────────────────────
const io = new Server(httpServer, {
  cors: {
    origin: config.corsOrigin,
    methods: ['GET', 'POST'],
  },
});

// Make io accessible to routes via req.app
app.set('io', io);

// ─── Middleware Stack ────────────────────────────────────────
// Security headers
app.use(
  helmet({
    contentSecurityPolicy: config.isDev ? false : undefined,
  })
);

// CORS
app.use(
  cors({
    origin: config.corsOrigin,
    credentials: true,
  })
);

// Request logging
app.use(morgan(config.isDev ? 'dev' : 'combined'));

// Body parsing
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true }));

// ─── Health Check ────────────────────────────────────────────
app.get('/', (req, res) => {
  res.json({
    success: true,
    message: 'SIH 2026 (sih26002) — Highway Infrastructure Safety System API',
    version: '1.0.0',
    status: 'running',
    timestamp: new Date().toISOString(),
  });
});

app.get('/api/health', (req, res) => {
  res.json({
    success: true,
    uptime: process.uptime(),
    memory: process.memoryUsage(),
    timestamp: new Date().toISOString(),
  });
});

// ─── API Routes ──────────────────────────────────────────────
app.use('/api/auth', authRoutes);
app.use('/api/location', locationRoutes);
app.use('/api/mesh', meshRoutes);
app.use('/api/incidents', incidentRoutes);

// Phase 4: Route monitoring routes (will be added)
// Phase 5: Prediction & notification routes (will be added)
// Phase 6: Navigation routes (will be added)

// ─── Socket.IO Connection Handler ───────────────────────────
io.on('connection', (socket) => {
  console.log(`🔌 Client connected: ${socket.id}`);

  // Phase 2: Room subscriptions for real-time tracking
  socket.on('subscribe:fleet', () => {
    socket.join('fleet');
    console.log(`📍 ${socket.id} joined fleet tracking room`);
  });

  socket.on('subscribe:mesh', () => {
    socket.join('mesh');
    console.log(`🔗 ${socket.id} joined mesh monitoring room`);
  });

  socket.on('subscribe:highway', (highway) => {
    if (highway && typeof highway === 'string') {
      const room = `highway:${highway.trim()}`;
      socket.join(room);
      console.log(`🛣️  ${socket.id} joined room ${room}`);
    }
  });

  // Phase 3: Incident monitoring room
  socket.on('subscribe:incidents', () => {
    socket.join('incidents');
    console.log(`🚨 ${socket.id} joined incidents monitoring room`);
  });

  socket.on('unsubscribe:fleet', () => {
    socket.leave('fleet');
  });

  socket.on('unsubscribe:mesh', () => {
    socket.leave('mesh');
  });

  socket.on('unsubscribe:incidents', () => {
    socket.leave('incidents');
  });

  socket.on('disconnect', (reason) => {
    console.log(`🔌 Client disconnected: ${socket.id} (${reason})`);
  });
});

// ─── Error Handling ──────────────────────────────────────────
app.use(notFound);
app.use(errorHandler);

// ─── Start Server ────────────────────────────────────────────
const startServer = async () => {
  try {
    // Connect to database
    await connectDB();

    // Start listening
    httpServer.listen(config.port, () => {
      console.log('');
      console.log('═══════════════════════════════════════════════');
      console.log('  🛣️  SIH 2026 — Highway Safety System API');
      console.log(`  📡 Server running on port ${config.port}`);
      console.log(`  🌍 Environment: ${config.nodeEnv}`);
      console.log(`  🔗 http://localhost:${config.port}`);
      console.log('═══════════════════════════════════════════════');
      console.log('');
    });
  } catch (error) {
    console.error('❌ Failed to start server:', error.message);
    process.exit(1);
  }
};

// Graceful shutdown
const gracefulShutdown = (signal) => {
  console.log(`\n📴 ${signal} received. Shutting down gracefully...`);
  httpServer.close(() => {
    console.log('✅ HTTP server closed');
    process.exit(0);
  });
  // Force close after 10 seconds
  setTimeout(() => {
    console.error('⚠️  Forced shutdown after timeout');
    process.exit(1);
  }, 10000);
};

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

// Start the server
startServer();

module.exports = { app, httpServer, io };
