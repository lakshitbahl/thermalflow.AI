/**
 * slurm-sim.js — DEV/DEMO ONLY synthetic Slurm node-state publisher.
 *
 * The twin (backend/twin) subscribes to `slurm.nodes.<tenant>` — the real GPU
 * allocation state that a production Slurm→NATS bridge would publish. There is
 * no such bridge in local dev, so without a source the twin never has input and
 * the whole pipeline produces nothing.
 *
 * This service stands in for that bridge: it models a small fleet across a few
 * halls and walks GPU allocation up and down (a slow diurnal-ish baseline plus
 * occasional large "training job" steps) so the twin's cooling-lag and
 * throttling-probability logic has real, moving signal to work with.
 *
 * It is intentionally NOT part of the k8s manifests — production gets a real
 * Slurm feed. It only exists in docker-compose for `make up`.
 *
 * Published message (matches what twin.js expects):
 *   subject: slurm.nodes.<tenant>
 *   payload: { tenant, nodes: [{ node_name, hall, gpus_total, gpus_alloc, state }], ts }
 */

const nats = require('nats');
const pino = require('pino')();

const TENANT = process.env.TENANT || 'demo';
const TICK_MS = parseInt(process.env.SLURM_TICK_MS || '5000', 10);
const GPUS_PER_NODE = parseInt(process.env.SLURM_GPUS_PER_NODE || '8', 10);

// Fleet: hall -> node count. Hall B is deliberately the dense/hot one.
const HALLS = {
  'Hall A': parseInt(process.env.SLURM_HALL_A_NODES || '16', 10),
  'Hall B': parseInt(process.env.SLURM_HALL_B_NODES || '24', 10),
  'Hall C': parseInt(process.env.SLURM_HALL_C_NODES || '12', 10),
};

function buildFleet() {
  const nodes = [];
  for (const [hall, count] of Object.entries(HALLS)) {
    const prefix = hall.replace(/\s+/g, '').toLowerCase();
    for (let i = 0; i < count; i++) {
      nodes.push({
        node_name: `${prefix}-gpu-${String(i).padStart(3, '0')}`,
        hall,
        gpus_total: GPUS_PER_NODE,
        gpus_alloc: 0,
        state: 'idle',
      });
    }
  }
  return nodes;
}

(async () => {
  let nc;
  for (let i = 1; i <= 15; i++) {
    try {
      nc = await nats.connect({ servers: process.env.NATS_URL || 'nats://nats:4222' });
      pino.info('slurm-sim connected to NATS');
      break;
    } catch (err) {
      pino.warn({ attempt: i, err: err.message }, 'NATS connect failed, retrying in 3s');
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  if (!nc) throw new Error('slurm-sim: cannot connect to NATS after 15 attempts');

  const fleet = buildFleet();
  // Per-hall target utilization in [0,1], evolved each tick.
  const target = {};
  for (const hall of Object.keys(HALLS)) target[hall] = 0.3;

  let t = 0;
  function tick() {
    t += 1;
    for (const hall of Object.keys(HALLS)) {
      // Slow baseline oscillation, phase-shifted per hall.
      const phase = Object.keys(HALLS).indexOf(hall);
      const baseline = 0.45 + 0.25 * Math.sin(t / 12 + phase);
      // Occasional large training-job step (pushes Hall B toward throttling).
      const jobSpike = Math.random() < 0.08 ? (hall === 'Hall B' ? 0.5 : 0.3) : 0;
      target[hall] = Math.min(1, Math.max(0, baseline + jobSpike));
    }

    // Allocate per node toward the hall target, in whole-GPU steps.
    for (const node of fleet) {
      const want = Math.round(node.gpus_total * target[node.hall]);
      // Move gradually so load-change detection in the twin sees real steps.
      if (node.gpus_alloc < want) node.gpus_alloc = Math.min(want, node.gpus_alloc + 2);
      else if (node.gpus_alloc > want) node.gpus_alloc = Math.max(want, node.gpus_alloc - 2);
      node.state = node.gpus_alloc > 0 ? 'allocated' : 'idle';
    }

    try {
      nc.publish(
        `slurm.nodes.${TENANT}`,
        JSON.stringify({ tenant: TENANT, nodes: fleet, ts: new Date().toISOString() })
      );
    } catch (err) {
      // nats.js throws if the connection is mid-reconnect; it auto-recovers, so
      // log and skip this tick rather than crash the process.
      pino.warn({ err: err.message }, 'publish failed (NATS reconnecting?), skipping tick');
    }
  }

  const totalNodes = fleet.length;
  pino.info({ tenant: TENANT, totalNodes, tickMs: TICK_MS }, 'slurm-sim running (DEV ONLY)');
  tick();
  setInterval(tick, TICK_MS);
})();
