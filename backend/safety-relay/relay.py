"""
relay.py — Safety-relay core logic (pure, no I/O; unit-testable).

ROLE: the relay is the SOLE WRITER to plant.control.<tenant>.setpoints. It sits in
SERIES between the MPC (a proposer) and plant-sim (the actuator). The MPC never writes
to the plant — it only publishes proposals. The relay either forwards a validated
proposal (ARMED) or substitutes a severity-scaled fallback ramp (TRIPPED). Because the
relay is the only publisher on the actuator subject in *every* state, there is no
parallel writer to race — the single-writer invariant is enforced by topology, not locks.

LATCHING: a trip does NOT auto-clear. Once TRIPPED it stays tripped (holding the fallback)
until an explicit operator reset(). This prevents authority chatter — handing control
back to the MPC the instant a sensor dips back under threshold would oscillate the plant.

The relay carries NO plant model. Every trip is a direct measurement test, verifiable by
inspection. (SIS principle: the safety function is simpler than, faster than, and
independent of the control function.)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# ── safe fallback target = max cooling within the CFD airflow cap ────────────
FALLBACK_FLOW = 0.85
FALLBACK_TEMP = 18.0

# ── trip thresholds (measurement-domain; match Gate A L1/L2) ────────────────
L1_VOTE_C     = 32.0   # 2-of-3 trio over this -> rack overheat (voted)
L1_SINGLE_C   = 38.0   # any single sensor over this -> break-glass (no vote)
L2_THETA      = 3.0    # weighted residual norm trip
L2_W_HOTAISLE = 1.0    # residual weight: hot-aisle carries the CRAC-fault signature
L2_W_INLET    = 0.3    # residual weight: rack inlets (less sensitive to CRAC loss)

# ── command validation bounds (reject malformed optimizer output) ───────────
CMD_FLOW_MIN, CMD_FLOW_MAX = 0.30, 0.85
CMD_TEMP_MIN, CMD_TEMP_MAX = 18.0, 27.0
CMD_FLOW_RATE_MAX = 0.15   # > MPC's 0.10/cycle VFD limit; trips on a bad jump

# ── watchdog timeouts (wall-clock seconds) ──────────────────────────────────
MPC_STALE_S    = 90.0   # MPC proposes every 60s; >90s = a missed cycle -> fall back
SENSOR_STALE_S = 10.0   # lost sensor comms -> cannot confirm safe -> trip

# ── fallback ramp rates (per evaluate() tick, nominally 1s) ─────────────────
RAMP_FLOW_GENTLE = 0.02   # graded fallback rate (per SECOND): protect plenum pressure
RAMP_TEMP_GENTLE = 0.10   # per SECOND
SEVERITY_GAIN    = 6.0    # break-glass multiplier on the ramp rate

ARMED, TRIPPED = "ARMED", "TRIPPED"


@dataclass
class SafetyRelay:
    # held command (what the relay is currently publishing); init to the safe state
    cmd_flow: float = FALLBACK_FLOW
    cmd_temp: float = FALLBACK_TEMP
    state: str = ARMED
    trip_source: Optional[str] = None
    severity: str = "none"            # none | graded | break_glass

    # latest inputs + their wall-clock receipt times
    mpc_flow: Optional[float] = None
    mpc_temp: Optional[float] = None
    prev_mpc_flow: Optional[float] = None   # last forwarded proposal (for rate check)
    mpc_ts: float = -1e9
    rack_sensors: list = field(default_factory=list)
    sensor_ts: float = -1e9
    residuals: dict = field(default_factory=dict)
    last_eval: float = -1.0           # wall time of previous evaluate() (for dt scaling)

    # ── input intake (called from NATS handlers) ────────────────────────────
    def update_mpc(self, flow: float, temp: float, now: float):
        self.mpc_flow, self.mpc_temp, self.mpc_ts = float(flow), float(temp), now

    def update_sensors(self, rack_sensors: list, now: float):
        self.rack_sensors, self.sensor_ts = rack_sensors, now

    def update_residual(self, residuals: dict, now: float):
        self.residuals = residuals or {}

    # ── trip tests (each returns (source, severity) or None) ────────────────
    def _check_l1(self):
        worst = None
        for rk in self.rack_sensors:
            trio = rk.get("sensors", {})
            vals = [v for v in trio.values() if v is not None]
            # break-glass: any single sensor over the hard limit
            for pos, v in trio.items():
                if v is not None and v > L1_SINGLE_C:
                    return (f"L1_single:{rk.get('rack')}/{pos}={v:.1f}", "break_glass")
            # voted: 2-of-3 over the soft limit
            if sum(1 for v in vals if v > L1_VOTE_C) >= 2:
                worst = (f"L1_vote:{rk.get('rack')}", "graded")
        return worst

    def _check_l2(self):
        worst_w = 0.0
        for sid, r in self.residuals.items():
            w = L2_W_HOTAISLE if "hot" in str(sid).lower() else L2_W_INLET
            worst_w = max(worst_w, w * abs(float(r)))
        if worst_w > L2_THETA:
            return (f"L2_residual:{worst_w:.2f}", "graded")
        return None

    def _check_watchdogs(self, now: float):
        if now - self.sensor_ts > SENSOR_STALE_S:
            return ("stale_sensor", "break_glass")   # blind -> assume unsafe
        if now - self.mpc_ts > MPC_STALE_S:
            return ("stale_mpc", "graded")            # missed control cycle
        return None

    def _check_command(self):
        if self.mpc_flow is None:
            return None
        f, t = self.mpc_flow, self.mpc_temp
        if not (CMD_FLOW_MIN <= f <= CMD_FLOW_MAX) or not (CMD_TEMP_MIN <= t <= CMD_TEMP_MAX):
            return (f"invalid_command:bounds f={f:.2f} t={t:.1f}", "graded")
        # rate test is MPC proposal-to-proposal (not vs the fallback-biased held cmd)
        if self.prev_mpc_flow is not None and abs(f - self.prev_mpc_flow) > CMD_FLOW_RATE_MAX:
            return (f"invalid_command:rate df={f - self.prev_mpc_flow:+.2f}", "graded")
        return None

    # ── the evaluation tick (cadence-robust via measured wall dt) ───────────
    def evaluate(self, now: float) -> dict:
        # measured elapsed time, not an assumed 1s tick: if the event loop is
        # starved, break-glass must cover the lost ground proportionally.
        dt = 0.0 if self.last_eval < 0 else max(0.0, min(now - self.last_eval, 5.0))
        self.last_eval = now

        if self.state == ARMED:
            # order: watchdogs (blind), then L1 (absolute), L2 (model), command (validation)
            trip = (self._check_watchdogs(now) or self._check_l1()
                    or self._check_l2() or self._check_command())
            if trip:
                self.state, (self.trip_source, self.severity) = TRIPPED, trip
            else:
                # forward the validated proposal (held between MPC cycles)
                if self.mpc_flow is not None:
                    self.cmd_flow, self.cmd_temp = self.mpc_flow, self.mpc_temp
                    self.prev_mpc_flow = self.mpc_flow

        if self.state == TRIPPED:
            self._ramp_fallback(dt)

        return self.status(now)

    def _ramp_fallback(self, dt: float):
        gain = SEVERITY_GAIN if self.severity == "break_glass" else 1.0
        df = RAMP_FLOW_GENTLE * gain * dt   # per-second rates x measured elapsed time
        dT = RAMP_TEMP_GENTLE * gain * dt
        self.cmd_flow = min(FALLBACK_FLOW, self.cmd_flow + df)
        self.cmd_temp = max(FALLBACK_TEMP, self.cmd_temp - dT)

    # ── operator reset (manual re-arm; the ONLY exit from TRIPPED) ───────────
    def reset(self, now: float) -> dict:
        # if a trip condition is still live, evaluate() will immediately re-trip.
        self.state, self.trip_source, self.severity = ARMED, None, "none"
        self.prev_mpc_flow = self.mpc_flow   # accept current proposal as the new baseline
        return self.evaluate(now)

    def status(self, now: float) -> dict:
        return {
            "state": self.state,
            "latched": self.state == TRIPPED,
            "trip_source": self.trip_source,
            "severity": self.severity,
            "command": {"flow": round(self.cmd_flow, 4), "temp": round(self.cmd_temp, 3)},
            "sole_writer": True,
            "age": {"mpc": round(now - self.mpc_ts, 1) if self.mpc_ts > -1e8 else None,
                    "sensor": round(now - self.sensor_ts, 1) if self.sensor_ts > -1e8 else None},
        }
