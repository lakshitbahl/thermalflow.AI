# ThermalFlow

A datacenter thermal-orchestration testbed. An MPC controller proposes cooling
setpoints (fan airflow + chilled-water supply temperature) to minimize plant energy
while holding the hot aisle under limit; a **safety relay** in series decides whether
those proposals ever reach the plant. The whole thing runs as a co-simulation: a
12-zone RC plant, a 6-zone reduced-order model (ROM), online identification, and a
React dashboard, wired over NATS and TimescaleDB.

The defining principle: **control authority is earned, not coded.** The optimizer can
only ever *propose*; a simple, fast, measurement-driven relay is the sole writer to the
plant and can override the optimizer at any time. You run it in `SHADOW` (the relay
computes everything but the plant ignores it — "iron disconnected") until you trust it,
then flip to `ACTIVE` to close the loop.

---

## 1. Prerequisites

- Docker + Docker Compose v2
- `make` (optional — every target is a one-line `docker compose` command you can run directly)
- A browser

No language toolchains are needed on the host; everything builds in containers.

## 2. Quick start

```bash
make up          # copies .env.example -> .env, then `docker compose up -d --build`
```

This brings up 12 services. First boot takes a few minutes (the Python images install
cvxpy). Check everything is healthy:

```bash
make status      # docker compose ps
```

| Service        | What it does                                              | Port |
|----------------|-----------------------------------------------------------|------|
| frontend       | React dashboard (via nginx)                               | 3000 |
| bff            | WebSocket/REST gateway, JWT auth, NATS->browser fan-out   | 8080 |
| nats           | message bus (monitoring UI on 8222)                       | 4222 |
| postgres       | TimescaleDB (auth tables + `thermal_zones` hypertable)    | 5432 |
| slurm-sim      | synthetic GPU workload generator                          | —    |
| plant-sim      | 12-zone ground-truth RC plant + fault-injection API       | 8090 |
| rom-shadow     | open-loop static ROM -> L2 residual monitor               | —    |
| rls-rom        | RLS-tuned ROM (online identification)                     | —    |
| mpc            | advisory MPC (proposes setpoints, never actuates)         | —    |
| safety-relay   | **sole writer** to the plant; trips/latches; reset API    | 8091 |
| health-monitor | slow coil-degradation alarm (advisory only); reset API     | 8092 |
| ingester       | persists all streams to TimescaleDB                       | —    |

## 3. Open the dashboard

1. Go to **http://localhost:3000**.
2. The dashboard needs a JWT (tenant must be `demo`). Mint one from the BFF container:

   ```bash
   docker compose exec bff node -e "console.log(require('jsonwebtoken').sign({tenant:'demo',sub:'operator'},process.env.JWT_SECRET,{algorithm:'HS256',expiresIn:'12h'}))"
   ```

3. Paste the printed token into the **Connect** bar. The status dot turns green (LIVE).

> For production, set a real `JWT_SECRET` in `.env` (`openssl rand -hex 32`) before `make up`.

## 4. Read the dashboard

Seven panels, left to right:

1. **Residual Decomposition** — truth vs measured vs static-ROM vs RLS-ROM. Shows whether online tuning closes the model gap.
2. **Spatial** — per-zone temperatures.
3. **Trip-wire** — the weighted L2 residual norm (hot-aisle weighted heavily) vs a naive inlet-only check, side by side.
4. **Faults** — active faults + event log.
5. **MPC Advisory** — what the MPC *proposes* (baseline vs advisory airflow/supply) and its predicted hot-aisle trajectory vs the limit.
6. **L1 Absolute Interlock** — per-rack top/mid/bottom sensors with the 2-of-3 (>32 C) and any-single (>38 C) thresholds.
7. **Safety Relay** — `ARMED` / `TRIPPED·LATCHED`, the sole-writer path (severs on trip), the trip source, the commanded airflow/supply, data ages, and the **Operator Reset** button.
8. **Coil Health (slow)** — the long-horizon degradation drift (°C), the CUSUM change-detector vs its alarm threshold, `OK`/`COMMISSIONING`/`ALARM`, and the **Recommission** button.

The header shows live airflow %, sim time, and the RLS state (`adapting` in SHADOW, `frozen` in ACTIVE).

## 5. Inject faults

Faults go to the plant-sim API (`POST :8090/fault`). Clear them with `POST :8090/fault/clear`.

```bash
# Localized top-of-rack spike -> L1 single-sensor trip (break-glass)
curl -X POST localhost:8090/fault -H 'Content-Type: application/json' \
  -d '{"type":"sensor_spike","rack":4,"position":"top","delta_c":16}'

# CRAC capacity loss -> L2 residual trip
curl -X POST localhost:8090/fault -H 'Content-Type: application/json' \
  -d '{"type":"crac_degradation","airflow_drop_pct":75}'

# Recirculation leak into a cold aisle
curl -X POST localhost:8090/fault -H 'Content-Type: application/json' \
  -d '{"type":"recirculation","cold_aisle":0,"intensity_kgps":2.0}'

# Slow coil fouling (ramp) -> coil-health ALARM, not a trip
curl -X POST localhost:8090/fault -H 'Content-Type: application/json' \
  -d '{"type":"coil_fouling","rate_pct_per_hour":5,"max_drop_pct":55}'

# Clear all faults
curl -X POST localhost:8090/fault/clear
```

Available `type` values: `sensor_spike`, `crac_degradation`, `coil_fouling`,
`recirculation`, `sensor_dropout`, `sensor_drift`, `sensor_noise`.

## 6. SHADOW vs ACTIVE — closing the loop

By default everything runs in **SHADOW**: the relay computes commands and publishes them,
but plant-sim ignores them and runs its baseline. Nothing the optimizer does can move the
(simulated) iron. This is the safe mode for first runs and for validating the dashboard,
NATS routing, and the relay state machine.

To **close the loop**, set three services to `ACTIVE` — they must agree, or you get a
split-brain (the ROM tracking commands the plant ignores -> false trips). In
`docker-compose.yml`, change `ACTUATION_MODE` on **plant-sim, rom-shadow, and rls-rom**
from `SHADOW` to `ACTIVE`, then:

```bash
docker compose up -d plant-sim rom-shadow rls-rom
```

In ACTIVE: plant-sim applies relay commands (through a VFD slew limiter), rom-shadow's L2
monitor tracks the actuated plant, and the RLS engine freezes (adapting in closed loop
would bias the parameter estimates).

To roll back at any time: set the three flags to `SHADOW` and `docker compose up -d` them.
The plant stays safe regardless — see the dead-man below.

## 7. The safety relay

The relay is the only publisher to `plant.control.demo.setpoints`. It evaluates every
second (independent of the MPC's 60 s clock) and latches on a trip until an operator reset.

| Trip source       | Condition                                            | Severity     |
|-------------------|------------------------------------------------------|--------------|
| `L1_single`       | any single rack sensor > 38 C                        | break-glass  |
| `L1_vote`         | 2-of-3 sensors on a rack > 32 C                      | graded       |
| `L2_residual`     | weighted ROM residual > 3.0 (hot aisle weighted 1.0) | graded       |
| `stale_sensor`    | no sensor data for > 10 s                            | break-glass  |
| `stale_mpc`       | no MPC proposal for > 90 s                           | graded       |
| `invalid_command` | MPC proposal out of bounds or jumps too far          | graded       |

On a trip the relay ramps the plant to max safe cooling (severity-scaled), latches, and
**persists the latch to disk** — an OOM/restart comes back `TRIPPED`, not `ARMED`.

**Operator reset** (the only exit from a latched trip):

```bash
curl -X POST localhost:8091/reset      # or click "Operator Reset" in Panel 7
```

If the trip condition is still live, it re-trips immediately.

**Dead-man's switch:** if the relay dies or the network partitions, plant-sim reverts to
its baseline (full cooling) after 90 s — ramped at the VFD's slew limit, never a hard snap.
The safe state survives relay death.

## 7b. Coil health (the slow layer)

The safety relay catches **acute** faults in seconds (a CRAC trip spikes the L2 residual).
But slow **fouling** — a coil losing effectiveness over weeks — never spikes; it creeps. If
the model the relay references were allowed to adapt to that creep, the residual would stay
at zero and you'd go blind until the fans saturate and the room melts.

So the health monitor watches the **same frozen clean-coil reference** the relay uses, but
with a long-horizon detector: it self-baselines during a commissioning window, then runs a
CUSUM on the residual's drift and raises a **maintenance ALARM** (it never trips the relay or
moves the plant). The alarm fires at a small sustained drift (~0.2–0.4 °C) — long before the
residual would ever reach the relay's trip threshold.

It only accumulates in a reference regime (relay `ARMED`, plant quasi-steady, load in a band)
so a load shift doesn't look like degradation. Two consequences:

- **Recommission after a real change.** A coil cleaning (restores effectiveness) or a
  `SHADOW`↔`ACTIVE` switch (changes the operating point) invalidates the baseline. Re-zero it:

  ```bash
  curl -X POST localhost:8092/commission     # or "Recommission" in Panel 8
  ```

- **It samples only when the plant visits the reference band.** If your load never enters it,
  the monitor stays quiet — widen `HEALTH_BAND_LO/HI` if needed.

Degradation state is durable (survives restarts), like the relay latch.

## 8. Validation protocol

Recommended order for a fresh closed-loop bring-up (escalating trust):

1. **SHADOW bring-up** — `make up`, confirm all services healthy, dashboard LIVE, relay `ARMED`, L2 flat.
2. **Go ACTIVE (no fault)** — flip the three flags, confirm L2 stays flat while airflow throttles, RLS shows `frozen`.
3. **Interlocks one at a time** — inject each fault from section 5, confirm the matching trip, clear + reset between each.
   For `coil_fouling`, watch Panel 8 raise an ALARM (no relay trip) while airflow still has headroom.
4. **Durable latch** — trip, then `docker compose kill safety-relay && docker compose up -d safety-relay`; it must boot back `TRIPPED`.
5. **Dead-man** — `docker compose stop safety-relay`; after 90 s the plant ramps to baseline.

## 8b. High availability (optional)

The relay is the sole writer, which makes it a single point of failure for the optimization
layer (the dead-man keeps the *plant* safe regardless). For zero-gap failover, run a hot
standby. Both relays evaluate everything; only the lease holder writes.

```bash
RELAY_HA=true docker compose --profile ha up -d
```

Safety during failover does **not** depend on the two relays agreeing. Every command carries
a monotonic **fencing epoch**; plant-sim rejects any epoch lower than the highest it has seen.
So a paused/zombie ex-leader that resumes is fenced out at the actuator — the standby bumps
the epoch on takeover and the old writer's commands are ignored. (This is why we use a fencing
lease, not Raft: consensus split-brain would otherwise reintroduce the multi-writer hazard.)

Even a single relay carries the epoch (defaulted to boot time), so a rolling restart fences
its predecessor automatically. An operator reset on either relay broadcasts to both.

## 9c. Automated compliance (conformance gate)

`compliance/` holds an automated runner that injects every fault class, measures the fallback
latency, exercises the HA zombie-fencing end-to-end, and emits a JSON ledger + an Annex-IV-
structured PDF as technical-documentation evidence. It imports the production safety modules
directly and is fully deterministic, so it gates CI (`python compliance/runner.py --repo-root .`).
It is evidence, NOT a legal/conformity determination — see `compliance/README.md` for scope.

## 9. Operations

```bash
make logs                  # tail all services
make logs-safety-relay     # tail one service (logs-<name>)
make restart               # restart all
make down                  # stop (keeps data volumes)
make clean                 # stop + remove volumes (wipes DB + the relay latch)
make urls                  # print the service URLs
```

## 10. Configuration

Key environment variables (in `docker-compose.yml` per service):

- `ACTUATION_MODE` (plant-sim, rom-shadow, rls-rom): `SHADOW` | `ACTIVE`
- `MPC_DT_S` (mpc): control period, default 60 s
- `FLOW_MAX` (mpc): airflow cap, default 0.85 (CFD bypass threshold)
- `ACTUATION_DEADMAN_S` (plant-sim): dead-man timeout, default 90 s
- `VFD_SLEW_PER_S` (plant-sim): fan accel limit, default 0.10 /s
- `SIM_SPEED` (plant-sim): sim-seconds per wall-second (raise for faster campaigns)
- `HEALTH_BAND_LO` / `HEALTH_BAND_HI` (health-monitor): load-fraction sampling band (0.40 / 0.80)
- `HEALTH_CUSUM_K` / `HEALTH_CUSUM_H` (health-monitor): CUSUM slack / alarm threshold (0.25 / 8.0)
- `RELAY_HA` (safety-relay): `true` enables fencing-lease HA (also run `--profile ha`)
- `RELAY_LEASE_TTL` (safety-relay): lease TTL seconds (default 6; keep >> the 1 s eval loop)

The frontend reads `VITE_ACTUATION_MODE` (display badge), `VITE_PLANT_API`,
`VITE_RELAY_API`, and `VITE_WS_URL` (blank = same-host auto-detect).

## 11. Design notes & open items

See **GATE_B_NOTES.md** for the closed-loop architecture rationale, the actuation-aware
L2 fix, the closed-loop-identification-bias decision (why RLS freezes in ACTIVE), and the
open items (two-timescale clean-coil degradation monitor, relay HA via a fencing lease,
heartbeat/sequence numbers).

## Repository layout

```
backend/
  slurm-sim/     synthetic GPU workload
  plant-sim/     12-zone RC plant + fault API + actuation + dead-man
  rom-shadow/    static ROM -> L2 residual monitor (actuation-aware)
  rls-rom/       RLS-tuned ROM (freezes in ACTIVE)
  mpc/           advisory MPC (real-kW objective, tau gain schedule)
  safety-relay/  sole writer; latching trip state machine; fencing-lease HA (fence.py)
  health-monitor/ slow coil-degradation alarm (frozen-reference CUSUM)
  ingester/      NATS -> TimescaleDB
  bff/           WebSocket/REST gateway + JWT
frontend/        React dashboard (Vite)
infrastructure/db/migrations/   postgres init (auth tables)
docker-compose.yml
```

Note: `rom_model_rls.py` is intentionally duplicated in `mpc/` and `rls-rom/` so each
service's Docker image builds from its own directory only — this is deliberate isolation,
not redundancy. Keep the two copies in sync if you change the ROM topology.
