# ThermalFlow AI
Full-stack datacenter thermal-orchestration digital-twin testbed (Python backend, TypeScript frontend),
containerized with Docker Compose and versioned DB migrations for reproducible deployment.

It is a datacenter thermal-orchestration **testbed**. A model-predictive controller (MPC)
*proposes* cooling setpoints (fan airflow + chilled-water supply temperature) to minimise
plant energy while holding the hot aisle under limit; a **safety relay** in series decides
whether those proposals ever reach the plant. The whole thing runs as a co-simulation: a
12-zone resistor-capacitor (RC) plant, a 6-zone reduced-order model (ROM), online
identification, a safety/assurance layer, and a React dashboard — wired over NATS and
TimescaleDB.
 
**Defining principle: control authority is earned, not coded.** The optimizer can only ever
*propose*. A simple, fast, measurement-driven relay is the sole writer to the (simulated)
plant and can override the optimizer at any time. You run in `SHADOW` (the relay computes
everything but the plant ignores it "iron disconnected") until you trust it, then flip to
`ACTIVE` to close the loop.
 
> Status: working prototype / MVP, validated in deterministic simulation. It has **not** been
> wired to real hardware, and the "sole writer" property is a guarantee of the *simulation*
> topology — not a claim that a software relay is a certified safety-instrumented function.
 
---
 
## 1. Prerequisites
 
- **Docker** + **Docker Compose v2**
- **`make`** (optional, every target is a one-line `docker compose` command you can run directly)
- A web browser
No language toolchains are needed on the host; everything builds in containers. No GPU required.
A laptop-class machine is sufficient for the simulation.
 
---
 
## 2. Quick start
 
```bash
# from the repository root
make up
```
 
`make up` copies `.env.example` to `.env` (if absent) and runs `docker compose up -d --build`.
It brings up **12 services**. First boot takes a few minutes (the Python images compile `cvxpy`).
 
Without `make`:
 
```bash
cp .env.example .env        # then edit JWT_SECRET (see step 4)
docker compose up -d --build
```
 
Confirm everything is healthy:
 
```bash
make status                 # == docker compose ps
```
 
---
 
## 3. Service map
 
| Service          | Responsibility                                              | Port  |
|------------------|-------------------------------------------------------------|-------|
| `frontend`       | React dashboard (served by nginx)                           | 3000  |
| `bff`            | WebSocket/REST gateway, JWT auth, NATS -> browser fan-out    | 8080  |
| `nats`           | Message bus (monitoring UI on 8222) + JetStream KV (HA lease)| 4222  |
| `postgres`       | TimescaleDB (auth tables + `thermal_zones` hypertable)      | 5432  |
| `slurm-sim`      | Synthetic GPU workload generator (drives thermal load)      | —     |
| `plant-sim`      | 12-zone ground-truth RC plant + fault API + dead-man        | 8090  |
| `rom-shadow`     | Frozen open-loop ROM -> L2 residual monitor                  | —     |
| `rls-rom`        | RLS-tuned ROM (online identification; freezes in ACTIVE)    | —     |
| `mpc`            | Advisory MPC (proposes setpoints, never actuates)           | —     |
| `safety-relay`   | **Sole writer** to the plant; latching trips; reset API     | 8091  |
| `health-monitor` | Slow coil-degradation alarm (advisory only); commission API | 8092  |
| `ingester`       | Persists all NATS streams to TimescaleDB                    | —     |
 
(`safety-relay-standby`, port 8093, is opt-in — see §9.)
 
---
 
## 4. Open the dashboard
 
1. Go to **http://localhost:3000**.
2. The dashboard needs a JWT whose tenant is `demo`. Mint one from the `bff` container:
```bash
   docker compose exec bff node -e "console.log(require('jsonwebtoken').sign({tenant:'demo',sub:'operator'},process.env.JWT_SECRET,{algorithm:'HS256',expiresIn:'12h'}))"
```
 
3. Paste the printed token into the **Connect** bar. The status dot turns green (LIVE).
> Before any non-local use, set a real secret in `.env`: `JWT_SECRET=$(openssl rand -hex 32)`,
> then `make up` again.
 
---
 
## 5. Read the dashboard
 
Panels, left to right:
 
1. **Residual Decomposition** — truth vs measured vs static-ROM vs RLS-ROM (does online tuning close the model gap?).
2. **Spatial** — per-zone temperatures.
3. **Trip-wire** — the weighted L2 residual norm (hot aisle weighted heavily) vs a naive inlet-only check.
4. **Faults** — active faults + event log.
5. **MPC Advisory** — proposed vs baseline airflow/supply, and the predicted hot-aisle trajectory vs the limit.
6. **L1 Absolute Interlock** — per-rack top/mid/bottom sensors with the 2-of-3 (>32 °C) and any-single (>38 °C) thresholds.
7. **Safety Relay** — `ARMED` / `TRIPPED·LATCHED`, the sole-writer path, the trip source, commanded setpoints, data ages, **Operator Reset** button.
8. **Coil Health (slow)** — long-horizon drift (°C), CUSUM vs alarm threshold, `OK`/`COMMISSIONING`/`ALARM`, **Recommission** button.
The header shows live airflow %, sim time, and RLS state (`adapting` in SHADOW, `frozen` in ACTIVE).
 
---
 
## 6. Inject faults
 
Faults go to the plant-sim API (`POST :8090/fault`); clear with `POST :8090/fault/clear`.
 
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
 
---
 
## 7. SHADOW vs ACTIVE — closing the loop
 
By default everything runs in **SHADOW**: the relay computes and publishes commands, but
`plant-sim` ignores them and runs its baseline. Nothing the optimizer does can move the
(simulated) iron. This is the safe mode for first runs and for validating the dashboard,
NATS routing, and the relay state machine.
 
To **close the loop**, three services must agree, or you get a split-brain (the ROM tracking
commands the plant ignores -> false trips). In `docker-compose.yml`, change `ACTUATION_MODE`
on **`plant-sim`, `rom-shadow`, and `rls-rom`** from `SHADOW` to `ACTIVE`, then:
 
```bash
docker compose up -d plant-sim rom-shadow rls-rom
```
 
In ACTIVE: `plant-sim` applies relay commands (through a VFD slew limiter), `rom-shadow`'s L2
monitor tracks the actuated plant, and the RLS engine **freezes** (adapting in closed loop
would bias the parameter estimates).
 
To roll back: set the three flags to `SHADOW` and `docker compose up -d` them. The plant stays
safe regardless — see the dead-man in §8.
 
---
 
## 8. The safety relay
 
The relay is the only publisher to `plant.control.demo.setpoints`. It evaluates every second
(independent of the MPC's 60 s clock) and latches on a trip until an operator reset.
 
| Trip source       | Condition                                            | Severity     |
|-------------------|------------------------------------------------------|--------------|
| `L1_single`       | any single rack sensor > 38 °C                       | break-glass  |
| `L1_vote`         | 2-of-3 sensors on a rack > 32 °C                     | graded       |
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
 
**Dead-man's switch:** if the relay dies or the network partitions, `plant-sim` reverts to its
baseline (full cooling) after `ACTUATION_DEADMAN_S` (default 90 s) — ramped at the VFD slew
limit, never a hard snap. The safe state survives relay death.
 
### Coil health (the slow layer)
 
The relay catches **acute** faults in seconds (a CRAC trip spikes the L2 residual). Slow
**fouling** never spikes; it creeps. The health monitor watches the **same frozen clean-coil
reference** the relay uses, self-baselines during a commissioning window, then runs a CUSUM on
the residual's drift and raises a **maintenance ALARM** (it never trips the relay or moves the
plant) — long before the residual would reach the trip threshold.
 
It only accumulates in a reference regime (relay `ARMED`, plant quasi-steady, load in a band),
so a load shift doesn't look like degradation. Therefore:
 
- **Recommission after a real change.** A coil cleaning or a `SHADOW`<->`ACTIVE` switch
  invalidates the baseline. Re-zero it:
```bash
  curl -X POST localhost:8092/commission     # or "Recommission" in Panel 8
```
 
- **It samples only inside the reference band.** If your load never enters it, the monitor
  stays quiet — widen `HEALTH_BAND_LO`/`HEALTH_BAND_HI` if needed.
Degradation state is durable across restarts, like the relay latch.
 
---
 
## 9. High availability (optional)
 
The relay is the sole writer — a single point of failure for the optimization layer (the
dead-man keeps the *plant* safe regardless). For zero-gap failover, run a hot standby. Both
relays evaluate everything; only the lease holder writes.
 
```bash
RELAY_HA=true docker compose --profile ha up -d
```
 
Safety during failover does **not** depend on the two relays agreeing. Every command carries a
monotonic **fencing epoch**; `plant-sim` rejects any epoch lower than the highest it has seen,
so a paused/zombie ex-leader that resumes is fenced out at the actuator. An operator reset on
either relay broadcasts to both. (This is why a fencing lease is used, not Raft: consensus
split-brain would otherwise reintroduce the multi-writer hazard.) Falls back to single-relay if
JetStream is unavailable.
 
---
 
## 10. Conformance / evidence runner
 
`compliance/` holds an automated runner that drives every fault class through the **production
safety modules** (not reimplementations), exercises the HA zombie-fencing end-to-end, and emits
a JSON ledger plus an Annex-IV-structured PDF as technical documentation evidence. It is
deterministic and binds a SHA-256 digest of the safety sources into each ledger.
 
```bash
pip install -r compliance/requirements.txt
python compliance/runner.py --repo-root .
```
 
It exits non-zero if any check fails (suitable for CI). It is **evidence, not a legal/conformity
determination**,  see `compliance/README.md` for scope and the verified-vs-not caveats.
 
---
 
## 11. Operations (make targets)
 
```bash
make up                    # cp .env + docker compose up -d --build
make status                # docker compose ps
make logs                  # tail all services
make logs-safety-relay     # tail one service (logs-<name>)
make restart               # restart all
make down                  # stop (keeps data volumes)
make clean                 # stop + remove volumes (wipes the DB and the relay latch)
make urls                  # print the service URLs
```
 
---
 
## 12. Configuration
 
Set the secrets in `.env` (copied from `.env.example`):
 
| Variable                  | Purpose                                             |
|---------------------------|-----------------------------------------------------|
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | TimescaleDB credentials                 |
| `JWT_SECRET`              | HS256 signing secret (`openssl rand -hex 32`)       |
 
Per-service tunables live in `docker-compose.yml`:
 
| Variable                                | Service(s)                | Default | Meaning                                          |
|-----------------------------------------|---------------------------|---------|--------------------------------------------------|
| `ACTUATION_MODE`                        | plant-sim, rom-shadow, rls-rom | `SHADOW` | `SHADOW` (no actuation) \| `ACTIVE` (closed loop) |
| `MPC_DT_S`                              | mpc                       | `60`    | Control period (seconds)                         |
| `FLOW_MAX`                              | mpc                       | `0.85`  | Airflow cap (CFD bypass threshold)               |
| `ACTUATION_DEADMAN_S`                   | plant-sim                 | `90`    | Revert-to-baseline timeout with no accepted command |
| `VFD_SLEW_PER_S`                        | plant-sim                 | `0.10`  | Fan acceleration limit (per second)              |
| `SIM_SPEED`                             | plant-sim                 | `1.0`   | Sim-seconds per wall-second (raise to compress runs) |
| `HEALTH_BAND_LO` / `HEALTH_BAND_HI`     | health-monitor            | `0.40` / `0.80` | Load-fraction sampling band              |
| `HEALTH_CUSUM_K` / `HEALTH_CUSUM_H`     | health-monitor            | `0.25` / `8.0`  | CUSUM slack / alarm threshold            |
| `RELAY_HA`                              | safety-relay              | `false` | `true` enables fencing-lease HA (also `--profile ha`) |
| `RELAY_LEASE_TTL`                       | safety-relay              | `6`     | Lease TTL (s); keep `>>` the 1 s eval loop       |
 
Frontend (optional, blank = same-host auto-detect): `VITE_WS_URL`, `VITE_PLANT_API`,
`VITE_RELAY_API`, `VITE_ACTUATION_MODE` (display badge only).
 
---
 
## 13. Validation protocol
 
Recommended order for a fresh closed-loop bring-up (escalating trust):
 
1. **SHADOW bring-up** — `make up`; confirm all services healthy, dashboard LIVE, relay `ARMED`, L2 flat.
2. **Go ACTIVE (no fault)** — flip the three flags (§7); confirm L2 stays flat while airflow throttles, RLS shows `frozen`.
3. **Interlocks one at a time** — inject each fault from §6; confirm the matching trip; clear + reset between each. For `coil_fouling`, watch Panel 8 raise an ALARM (no trip) while airflow still has headroom.
4. **Durable latch** — trip, then `docker compose kill safety-relay && docker compose up -d safety-relay`; it must boot back `TRIPPED`.
5. **Dead-man** — `docker compose stop safety-relay`; after ~90 s the plant ramps to baseline.
---
 
## 14. Repository layout
 
```
backend/
  slurm-sim/       synthetic GPU workload
  plant-sim/       12-zone RC plant + fault API + actuation + dead-man
  rom-shadow/      frozen ROM -> L2 residual monitor (actuation-aware)
  rls-rom/         RLS-tuned ROM (freezes in ACTIVE)
  mpc/             advisory MPC (real-kW objective)
  safety-relay/    sole writer; latching trip state machine; fencing-lease HA (fence.py)
  health-monitor/  slow coil-degradation alarm (frozen-reference CUSUM)
  ingester/        NATS -> TimescaleDB
  bff/             WebSocket/REST gateway + JWT
frontend/          React dashboard (Vite)
compliance/        conformance runner + evidence ledger/report
infrastructure/db/ postgres init (auth tables)
docker-compose.yml
Makefile
GATE_B_NOTES.md    closed-loop architecture rationale + design decisions
```
 
> Note: `rom_model_rls.py` is intentionally duplicated in `mpc/` and `rls-rom/` so each
> service's Docker image builds from its own directory only deliberate isolation, not
> redundancy. Keep the two copies in sync if you change the ROM topology.
 
---
 
## 15. Design notes
 
See **GATE_B_NOTES.md** for the closed-loop architecture rationale, the actuation-aware L2 fix,
the closed-loop-identification-bias decision (why RLS freezes in ACTIVE), and the two-timescale
degradation / HA-fencing design. For the full engineering reference (interfaces, internals,
verification status), see the technical reference document.

## License

This project is proprietary. All rights reserved.

Unauthorized copying, modification, distribution, or use of this software is strictly prohibited without explicit permission from the author.
