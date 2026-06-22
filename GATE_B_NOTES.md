# Gate B — ACTIVE-Ready (closed-loop unblocked)

## What this build does
Removes the hard blocker that made ACTIVE actuation impossible (L2 false-tripping on
legitimate MPC throttling) and hardens the four failure modes found in red-team review.

### 1. Actuation-aware L2 ROM  (`rom-shadow/rom_model.py`, `service.py`)
- `set_actuation(u_flow, u_temp)`: u_flow scales every conductance exactly as plant-sim
  scales `airflow_frac`; u_temp is the CRAC supply forcing. Frozen params.
- rom-shadow subscribes `plant.control.<tenant>.setpoints` (relay = sole writer).
- **Mode-gated**: follows the command only in ACTIVE. In SHADOW the plant ignores
  commands and runs baseline, so the ROM stays baseline too — otherwise it would diverge
  from the plant and false-trip in shadow.
- **Deliberate deviation from spec**: NO separate tau_water lag in the monitor. plant-sim
  has no waterside lag STATE — its supply lags the setpoint only through the CRAC-zone
  thermal dynamics, which the ROM already reproduces identically. A tau_water lag in the
  monitor alone would desync it from the plant and reintroduce a smaller false residual.
  If we later add a waterside state to plant-sim, the monitor mirrors it then — not before.

### 2. RLS freeze in closed loop  (`rls-rom/rom_model_rls.py` `hold()`, `service.py`)
- `if ACTUATION_MODE == ACTIVE: rom.hold()` — reports current params, does NOT adapt.
- Rationale: u = MPC(x) correlates the regressor with the equation error (closed-loop
  identification bias). Adapting in closed loop biases eps/U_scale regardless of gating.
  Re-enabling adaptation later requires external excitation, not move-block gating.

### 3. Plant-side VFD slew  (`plant-sim/plant_sim.py`)
- `airflow_frac` ramps toward target at `vfd_slew_per_s` (0.10/s). A commanded step —
  including the dead-man snap to baseline — can no longer produce a mechanical step.
  Protects the hardware regardless of which upstream service is talking.

### 4. Durable safety latch  (`safety-relay/service.py`)
- LATCHED state + trip_source persisted (atomic write) to `/data/relay_latch.json`
  BEFORE the fallback command hits the wire. On boot, `restore_latch()` refuses ARMED
  if a trip was latched — survives OOM/cold restart. `/reset` clears it (operator ack).
- Volume `relay-state:/data` in compose.

### 5. Cadence-robust fallback  (`safety-relay/relay.py`)
- Fallback ramp uses measured wall dt (`monotonic() - last_eval`), not an assumed 1s tick.
  A starved event loop scales break-glass up proportionally to cover lost ground.

## Verified in-sandbox (ACTIVE closed-loop co-sim)
- **L2 flat under aggressive throttling**: MPC drove airflow 1.0 -> ~0.73, L2(HA)
  residual stayed in [-0.30, 0.00] °C — no false trip. (Pre-fix this throttling
  false-tripped at ~3 °C immediately.)
- **L2 fires on the real fault**: CRAC -75% -> residual 0.84 -> 3.58 -> TRIP at +6 s,
  latches, climbs to 28 °C as the fault deepens.
- Relay logic: 10/10 regression (trips, fallback convergence, latch).
- Durable latch: trip persists, cold boot restores TRIPPED, reset clears, reboot ARMED.
- VFD slew: dead-man snap 0.40->1.0 ramps at 0.10/s (no step).
- All Python compiles; compose valid (11 services + relay-state volume).

## NOT verified here (confirm on your machine)
- Live NATS/TimescaleDB/container runtime; cvxpy image build; live `/reset` over HTTP.

## How to actually close the loop
Set `ACTUATION_MODE: ACTIVE` on **plant-sim, rom-shadow, and rls-rom together** (they must
agree). plant-sim then applies relay commands (through VFD slew + dead-man); rom-shadow's
L2 twin tracks them; rls-rom freezes. SHADOW (default) remains the safe first run.

## Two-timescale safety (BUILT)

The relay's L2 trip is the **fast** layer (acute faults, seconds). The **slow** layer is the
`health-monitor` service: it consumes the same frozen clean-coil residual (`rom.static`),
gates on a reference regime (relay ARMED, quasi-steady, load in band), self-baselines during
a commissioning window, and runs EWMA (readable drift °C) + one-sided CUSUM (change detector)
to raise a maintenance ALARM on sustained drift. It never trips the relay or actuates.

- Durable state (`/data/coil_health.json`), `POST :8092/commission` to re-baseline.
- `coil_fouling` ramp fault added to plant-sim (distinct from the acute `crac_degradation`
  step) drives `coil_eff` down slowly; the frozen reference is blind to it -> residual drifts.
- Verified (co-sim): commissions, ignores noise / out-of-band / transient / disarmed samples,
  ALARMs on sustained drift while the relay is still ARMED with airflow headroom, then halts
  accumulation once tripped. 11/11 detector unit tests.

Honest caveats:
- **Single baseline** assumes the in-band structural residual is ~constant; per-load-bin
  baselining is the production extension.
- **Operating-point coupling**: a SHADOW<->ACTIVE switch changes the regime and invalidates
  the baseline -> recommission.
- The co-sim used an accelerated fouling rate (1800 %/hr) for speed; real lead time scales by
  ~1/rate (days/weeks at realistic fouling).

## Relay HA + command-path hardening (BUILT)

**Fencing epoch + sequence number (item 2 — fully verified).** Every command carries
`{epoch, seq, ts}`. plant-sim is the AUTHORITY: it tracks the highest epoch seen and rejects
any lower one (a fenced zombie), requires `seq` to advance within an epoch (rejects replays /
stuck commands), and bases the dead-man on the last ACCEPTED command (a rejected zombie can't
keep it alive). Single relay defaults its epoch to boot wall-time, so a rolling-restart
predecessor is already fenced — no lease required. Verified: 6/6 enforcement tests.

**Leader-election fencing lease (item 1 — logic verified; live store not).** Two relays share
a lease (`fence.py`, store-agnostic over optimistic-CAS KV). Only the holder publishes; the
standby evaluates hot and takes over on expiry, BUMPING the epoch (fence). Safety does NOT
depend on the relays agreeing — a zombie that still believes it leads is fenced at plant-sim.
Verified (in-memory store): single-leader, takeover, epoch monotonicity, no-split-brain race,
eventual convergence, and an end-to-end failover through the real plant-sim fence (takeover +
zombie rejection, no dual-write reaches the actuator). Reset broadcasts to both relays.

- Opt-in: `RELAY_HA=true docker compose --profile ha up -d` (default = single relay, the
  tested path). JetStream KV bucket `thermalflow_relay`; falls back to single-relay if JS is
  unavailable rather than bricking the safety function.
- Hardening (both verified in-memory): (a) the epoch bumps per process-leadership-session, so
  a relay restart with a fresh in-memory seq counter is accepted by plant-sim instead of being
  rejected as stale_seq; (b) the trip latch lives in the shared lease store, so a relay that
  was DOWN during a trip and later becomes leader adopts the latch on promotion rather than
  silently re-arming — preserving the manual-reset-only invariant across the pair.
- **NOT verified here**: the live NATS JetStream KV wiring (`NatsKvStore`) and a real two-pod
  failover — needs a running cluster. The election LOGIC, the fence ENFORCEMENT, the restart
  epoch-bump, and the shared-latch adopt/clear are all verified in isolation; only the KV glue
  is untested.
- Caveat: lease expiry uses wall time, so keep TTL >> heartbeat (default 6s / 1s) given
  cross-host clock skew; the JetStream server-side bucket TTL is the single-clock backstop.

**Per-load-bin degradation baselining (item 3 — fully verified).** The coil monitor now keeps
an independent baseline + CUSUM per load bin across the band, so a load shift to a different
operating point can't masquerade as drift. Fouling is broadband, so it lights bins as they're
visited; alarm on any committed bin. Verified: 10/10 incl. load-shift false-alarm immunity.

## Nothing open
All Gate B open items are addressed. Remaining future work is operational, not architectural:
exercise the HA pair on a live cluster (KV glue + real failover), and tune the coil-monitor
bin/CUSUM constants against real plant data.
