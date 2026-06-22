/**
 * ingester.js — Phase 0 persistence: NATS -> TimescaleDB (thermal_zones).
 *
 * Subscribes (core NATS; plant-sim is deterministic so at-least-once durability
 * is unnecessary — re-run the campaign to reproduce):
 *   sensor.thermal.<tenant>        -> metric_name 'measured'
 *   plant.truth.<tenant>           -> metric_name 'truth'
 *   rom.static/rls.<tenant>.pred     -> metric_name pred_static / pred_rls (+ eps_rls)
 *   rom.static/rls.<tenant>.residual -> metric_name residual_static / residual_rls
 *
 * Writes the narrow (time, zone_id, metric_name, value) layout via a single
 * UNNEST bulk insert per flush. Dual flush trigger (whichever first):
 *   FLUSH_MS time bound (crash-loss bound)  OR  BATCH_MAX rows (warp throughput).
 *
 * CRITICAL: `time` is derived from the payload sim-`ts`, NOT NOW(). Under
 * SIM_SPEED warp, NOW() would collapse many sim-seconds onto one wall-second and
 * destroy the time axis. time = CAMPAIGN_EPOCH + ts seconds.
 */
const nats = require('nats');
const { Pool } = require('pg');
const pino = require('pino')();

const TENANT = process.env.TENANT || 'demo';
const FLUSH_MS = parseInt(process.env.FLUSH_MS || '1000', 10);
const BATCH_MAX = parseInt(process.env.BATCH_MAX || '2000', 10);
const BUFFER_CAP = parseInt(process.env.BUFFER_CAP || '50000', 10);
// Campaign epoch: sim ts=0 maps here. Default = service start. Override to align
// or separate concurrent campaigns into distinct time ranges.
const CAMPAIGN_EPOCH_MS = process.env.CAMPAIGN_EPOCH
  ? Date.parse(process.env.CAMPAIGN_EPOCH)
  : Date.now();

// ── Pure payload -> rows mapping (unit-testable, no I/O) ────────────────────
// Returns array of { ts, zone, metric, value }. `time` is computed at flush.
function rowsFor(subject, data) {
  const rows = [];
  const ts = Number(data.ts) || 0;
  const push = (zone, metric, value) => {
    if (value != null && Number.isFinite(value)) rows.push({ ts, zone, metric, value });
  };

  if (subject.startsWith('sensor.thermal')) {
    for (const r of data.rack_inlet_c || []) if (r.valid) push(r.sensor_id, 'measured', r.temp_c);
    for (const z of data.zones || []) if (z.valid) push(z.zone_id, 'measured', z.temp_c);
    push('crac', 'airflow_pct', data.crac_airflow_pct); // fault provenance
  } else if (subject.startsWith('plant.truth')) {
    const roles = data.zone_roles || [];
    (data.state || []).forEach((v, i) => push(roles[i], 'truth', v));
  } else if (subject.startsWith('rom.static') || subject.startsWith('rom.rls')) {
    const model = subject.startsWith('rom.rls') ? 'rls' : 'static';
    if (subject.endsWith('.pred')) {
      for (const [zone, v] of Object.entries(data.state || {})) push(zone, `pred_${model}`, v);
      // persist RLS-identified parameters for convergence backtests
      if (model === 'rls' && data.params) {
        push('rom', 'eps_rls', data.params.eps);
        push('rom', 'u_scale_rls', data.params.u_scale);
      }
    } else if (subject.endsWith('.residual')) {
      for (const [sid, v] of Object.entries(data.residuals || {})) push(sid, `residual_${model}`, v);
    }
  }
  return rows;
}

module.exports = { rowsFor }; // for tests

if (require.main === module) {
  (async () => {
    // ── NATS ────────────────────────────────────────────────
    let nc;
    for (let i = 1; i <= 15; i++) {
      try {
        nc = await nats.connect({ servers: process.env.NATS_URL || 'nats://nats:4222' });
        pino.info('ingester connected to NATS');
        break;
      } catch (err) {
        pino.warn({ attempt: i, err: err.message }, 'NATS connect failed, retrying in 3s');
        await new Promise((r) => setTimeout(r, 3000));
      }
    }
    if (!nc) throw new Error('Cannot connect to NATS after 15 attempts');

    // ── Postgres / TimescaleDB ──────────────────────────────
    const pool = new Pool({
      host:     process.env.DB_HOST,
      port:     process.env.DB_PORT || 5432,
      database: process.env.DB_NAME,
      user:     process.env.DB_USER,
      password: process.env.DB_PASSWORD,
    });

    // CRITICAL: without this, an idle-client error (Postgres restart, dropped
    // connection) is thrown as an uncaught exception and kills the process —
    // the classic node-postgres crash-loop. Log and let the pool recover.
    pool.on('error', (err) => pino.error({ err: err.message }, 'idle pg client error (recovering)'));

    // Ensure schema exists even on a pre-002 volume (migration handles policies).
    // Retry rather than crash if Postgres isn't ready the instant we connect.
    for (let i = 1; i <= 15; i++) {
      try {
        await pool.query(`
          CREATE TABLE IF NOT EXISTS thermal_zones (
            time TIMESTAMPTZ NOT NULL, zone_id TEXT NOT NULL,
            metric_name TEXT NOT NULL, value DOUBLE PRECISION NOT NULL
          );`);
        try {
          await pool.query(`SELECT create_hypertable('thermal_zones','time', if_not_exists => TRUE);`);
        } catch (e) {
          pino.warn({ err: e.message }, 'create_hypertable skipped (no timescaledb extension?)');
        }
        pino.info('thermal_zones schema ready');
        break;
      } catch (err) {
        pino.warn({ attempt: i, err: err.message }, 'schema bootstrap failed, retrying in 3s');
        if (i === 15) throw err;
        await new Promise((r) => setTimeout(r, 3000));
      }
    }

    // ── Buffer + flush machinery ────────────────────────────
    let buffer = [];
    let flushing = false;
    let dropped = 0;
    let written = 0;

    async function flush() {
      if (flushing || buffer.length === 0) return;
      flushing = true;
      const batch = buffer;
      buffer = [];
      try {
        const times = batch.map((r) => new Date(CAMPAIGN_EPOCH_MS + r.ts * 1000));
        const zones = batch.map((r) => r.zone);
        const metrics = batch.map((r) => r.metric);
        const values = batch.map((r) => r.value);
        await pool.query(
          `INSERT INTO thermal_zones (time, zone_id, metric_name, value)
           SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::float8[])`,
          [times, zones, metrics, values]
        );
        written += batch.length;
      } catch (err) {
        // Drop on write error (don't requeue -> unbounded growth if DB is down).
        dropped += batch.length;
        pino.error({ err: err.message, dropped: batch.length }, 'flush failed, batch dropped');
      } finally {
        flushing = false;
      }
    }

    setInterval(flush, FLUSH_MS);
    setInterval(() => pino.info({ written, dropped, buffered: buffer.length }, 'ingester stats'), 30000);

    function enqueue(rows) {
      for (const r of rows) buffer.push(r);
      if (buffer.length >= BUFFER_CAP) {
        // backpressure: drop oldest, keep the newest BUFFER_CAP
        const over = buffer.length - BUFFER_CAP;
        buffer.splice(0, over);
        dropped += over;
      }
      if (buffer.length >= BATCH_MAX) flush();
    }

    async function handle(sub) {
      for await (const msg of sub) {
        try {
          enqueue(rowsFor(msg.subject, JSON.parse(msg.data)));
        } catch (err) {
          pino.error({ err: err.message, subject: msg.subject }, 'parse error');
        }
      }
    }

    handle(nc.subscribe(`sensor.thermal.${TENANT}`));
    handle(nc.subscribe(`plant.truth.${TENANT}`));
    handle(nc.subscribe(`rom.static.${TENANT}.pred`));
    handle(nc.subscribe(`rom.static.${TENANT}.residual`));
    handle(nc.subscribe(`rom.rls.${TENANT}.pred`));
    handle(nc.subscribe(`rom.rls.${TENANT}.residual`));
    pino.info({ tenant: TENANT, flushMs: FLUSH_MS, batchMax: BATCH_MAX, bufferCap: BUFFER_CAP,
      campaignEpoch: new Date(CAMPAIGN_EPOCH_MS).toISOString() }, 'ingester subscribed (Phase 0)');

    // ── Graceful shutdown: flush what's buffered ────────────
    const shutdown = async () => {
      pino.info('shutting down, final flush…');
      await flush();
      await pool.end().catch(() => {});
      await nc.drain().catch(() => {});
      process.exit(0);
    };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
  })();
}
