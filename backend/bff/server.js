const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const nats = require('nats');
const jwt = require('jsonwebtoken');
const { Pool } = require('pg');
const cors = require('cors');
const rateLimit = require('express-rate-limit');
const pino = require('pino')();
const promClient = require('prom-client');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// ── Prometheus metrics ────────────────────────────────────────
const httpRequestCounter = new promClient.Counter({
  name: 'http_requests_total',
  help: 'Total HTTP requests',
  labelNames: ['method', 'route', 'status'],
});
const wsConnectionsGauge = new promClient.Gauge({
  name: 'ws_connections_active',
  help: 'Number of active WebSocket connections',
});
promClient.collectDefaultMetrics();

// ── PostgreSQL ────────────────────────────────────────────────
const pool = new Pool({
  host:     process.env.DB_HOST,
  port:     process.env.DB_PORT,
  database: process.env.DB_NAME,
  user:     process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  max: 20,
  idleTimeoutMillis: 30000,
});

// ── NATS ─────────────────────────────────────────────────────
let nc;
(async () => {
  const retries = 10;
  for (let i = 1; i <= retries; i++) {
    try {
      nc = await nats.connect({ servers: process.env.NATS_URL || 'nats://nats:4222' });
      pino.info('NATS connected');
      break;
    } catch (err) {
      pino.warn({ attempt: i, err: err.message }, 'NATS connect failed, retrying in 3s');
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  if (!nc) {
    pino.error('Could not connect to NATS — BFF will start without NATS');
  }
})();

// ── Middleware ────────────────────────────────────────────────
// Behind nginx (compose) and the k8s ingress, the client IP arrives in
// X-Forwarded-For. Without this, express-rate-limit keys every request to
// the proxy IP — so the whole fleet shares one bucket. Trust exactly one
// proxy hop (the ingress/nginx in front of us), not an open chain.
app.set('trust proxy', 1);

app.use(cors({ origin: process.env.CORS_ORIGIN || '*' }));
app.use(express.json());

const limiter = rateLimit({
  windowMs: 60 * 1000,
  max: 100,
  handler: (req, res) => res.status(429).json({ error: 'Too many requests' }),
});
app.use('/api', limiter);

app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const duration = Date.now() - start;
    pino.info({ method: req.method, url: req.url, status: res.statusCode, duration });
    // Use the matched route template (e.g. "/api/mode"), never req.url, which
    // carries query strings / path params and would explode metric cardinality.
    const route = req.route?.path || 'unmatched';
    httpRequestCounter.labels(req.method, route, String(res.statusCode)).inc();
  });
  next();
});

// ── Auth middleware ───────────────────────────────────────────
const authenticate = (req, res, next) => {
  const token = req.headers.authorization?.split(' ')[1];
  if (!token) return res.status(401).json({ error: 'Missing token' });
  try {
    // Pin the algorithm. Without this, a token signed with alg:"none" (or an
    // attacker-chosen alg) would be accepted — classic JWT alg-confusion.
    req.user = jwt.verify(token, process.env.JWT_SECRET, { algorithms: ['HS256'] });
    next();
  } catch {
    res.status(403).json({ error: 'Invalid or expired token' });
  }
};

// ── Routes ────────────────────────────────────────────────────

// Root health — used by K8s liveness/readiness probes
app.get('/health', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  } catch (err) {
    res.status(503).json({ status: 'unhealthy', error: err.message });
  }
});

// API health (for direct client calls)
app.get('/api/health', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  } catch (err) {
    res.status(503).json({ status: 'unhealthy', error: err.message });
  }
});

app.get('/api/user', authenticate, (req, res) => {
  res.json({ userId: req.user.userId, tenant: req.user.tenant, roles: req.user.roles });
});

app.post('/api/mode', authenticate, async (req, res) => {
  const { mode } = req.body;
  if (!['shadow', 'active'].includes(mode))
    return res.status(400).json({ error: 'Invalid mode: must be "shadow" or "active"' });
  if (!req.user.roles?.includes('admin'))
    return res.status(403).json({ error: 'Insufficient privileges: admin role required' });

  try {
    if (nc) {
      nc.publish(
        'control.mode.change',
        JSON.stringify({ mode, user: req.user.userId, tenant: req.user.tenant })
      );
    }

    await pool.query(
      'INSERT INTO audit_logs (user_id, tenant, action, details) VALUES ($1, $2, $3, $4)',
      [req.user.userId, req.user.tenant, 'mode_change', JSON.stringify({ mode })]
    );

    res.json({ status: 'accepted', mode });
  } catch (err) {
    // Without this, a rejected DB write becomes an unhandledRejection and Node
    // (>=18) terminates the entire BFF — one transient DB blip would take the
    // whole service down.
    pino.error({ err: err.message }, 'Mode change failed');
    res.status(500).json({ error: 'Failed to record mode change' });
  }
});

app.get('/metrics', async (req, res) => {
  res.set('Content-Type', promClient.register.contentType);
  res.end(await promClient.register.metrics());
});

// ── WebSocket ─────────────────────────────────────────────────
wss.on('connection', (ws, req) => {
  const url = new URL(req.url, 'http://dummy');
  const token = url.searchParams.get('token');

  if (!token) {
    ws.close(1008, 'Unauthorized');
    return;
  }

  let user;
  try {
    user = jwt.verify(token, process.env.JWT_SECRET, { algorithms: ['HS256'] });
  } catch {
    ws.close(1008, 'Invalid token');
    return;
  }

  wsConnectionsGauge.inc();
  ws.isAlive = true;
  ws.on('pong', () => { ws.isAlive = true; });
  pino.info({ userId: user.userId, tenant: user.tenant }, 'WebSocket connected');

  // Subscribe to every subject scoped to this tenant. Our subjects place the
  // tenant in the 3rd token and are either:
  //   3 tokens — physics.forecast.<tenant>, physics.points.<tenant>,
  //              health.system.<tenant>, workload.forecast.<tenant>
  //   4 tokens — bms.telemetry.<tenant>.<crac_id>
  // NATS wildcard rules: '*' matches exactly one token; '>' matches one-or-more
  // trailing tokens and MUST be the final token. So a pattern like
  // ">.<tenant>.>" is invalid (">" is not last) and silently matches nothing /
  // errors. We subscribe to both valid shapes instead.
  const subs = [
    nc?.subscribe(`*.*.${user.tenant}`),
    nc?.subscribe(`*.*.${user.tenant}.>`),
  ].filter(Boolean);

  for (const sub of subs) {
    (async () => {
      for await (const msg of sub) {
        try {
          if (ws.readyState !== WebSocket.OPEN) break;
          ws.send(JSON.stringify({ subject: msg.subject, data: JSON.parse(msg.data) }));
        } catch (err) {
          pino.error({ err }, 'Failed to forward NATS message to WebSocket');
        }
      }
    })();
  }

  ws.on('close', () => {
    subs.forEach((s) => s.unsubscribe());
    wsConnectionsGauge.dec();
    pino.info({ userId: user.userId }, 'WebSocket disconnected');
  });
});

// Heartbeat: terminate half-open connections (e.g. client vanished without a
// TCP FIN). Without this, ws_connections_active leaks and dead sockets pin
// NATS subscriptions open.
const heartbeat = setInterval(() => {
  wss.clients.forEach((ws) => {
    if (ws.isAlive === false) return ws.terminate();
    ws.isAlive = false;
    ws.ping();
  });
}, 30000);
wss.on('close', () => clearInterval(heartbeat));

// ── Start ─────────────────────────────────────────────────────
const PORT = process.env.PORT || 8080;
server.listen(PORT, () => pino.info(`BFF listening on port ${PORT}`));
