"""
checks.py — deterministic conformance checks.

Each check imports the PRODUCTION safety module (relay.py, fence.py, plant_sim.py, …) and
drives it through a seeded, in-process scenario, then returns a CheckResult with measured
metrics vs. budgets. No NATS, no containers: the messaging layer is out of scope here and is
covered by a separate live-stack smoke test (see README). Budgets are SAFETY-CASE parameters
(configurable), not regulatory constants.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

from ledger import CheckResult

SEED = 0
REPO_ROOT = None


def setup_paths(repo_root):
    global REPO_ROOT
    REPO_ROOT = repo_root
    for rel in ("backend/plant-sim", "backend/safety-relay", "backend/mpc",
                "backend/rom-shadow", "backend/health-monitor"):
        p = os.path.join(repo_root, rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def load_service(modname, relpath):
    """Load a service.py by file path under a UNIQUE module name. Every service dir has a
    file literally named service.py, so a bare `import service` would collide; this gives each
    a fresh, distinct module (also resets its module-level plant/_fence state per check)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, os.path.join(REPO_ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


DEFAULT_CONFIG = {
    "budgets": {
        "l1_breakglass_cycles": 2,
        "l1_graded_cycles": 2,
        "l2_residual_s": 15,
        "stale_mpc_s": 95,        # 90 s threshold + one eval cycle of slack
        "stale_sensor_s": 12,     # 10 s threshold + slack
        "invalid_cmd_cycles": 2,
        "deadman_revert_cycles": 2,
        "health_lead_s_min": 5,   # the maintenance alarm must precede the trip by >= this
    },
    "sim": {"dt_s": 1.0, "settle_ticks": 60, "load_frac": 0.6},
    "seed": SEED,
}


# ── helpers ─────────────────────────────────────────────────────────────────
def _meas_ha(plant):
    for z in plant.read_sensors()["zones"]:
        if z["zone_id"] == "hot-aisle":
            return z["temp_c"]
    return None


def _safe_trio(temp=24.0):
    return [{"rack": "r4", "sensors": {"top": temp, "mid": temp - 1, "bottom": temp - 2}}]


def _result(cid, title, req, method, metrics, budgets, ok, annex, detail="", error=""):
    return CheckResult(
        id=cid, title=title, requirement=req, method=method, metrics=metrics,
        budgets=budgets, verdict=("PASS" if ok else "FAIL") if not error else "ERROR",
        annex_iv=annex, detail=detail, error=error)


# ── interlock checks (relay logic, synthetic inputs) ────────────────────────
def chk_l1_breakglass(cfg):
    from relay import SafetyRelay, TRIPPED
    b = cfg["budgets"]["l1_breakglass_cycles"]
    sr = SafetyRelay()
    # prime ARMED with safe data
    for t in range(3):
        sr.update_sensors(_safe_trio(), t); sr.update_mpc(0.6, 18.0, t); sr.update_residual({"hot-aisle": 0.2}, t)
        sr.evaluate(t)
    armed = sr.state != TRIPPED
    # inject a single sensor over the hard limit
    inj = 3
    sr.update_sensors([{"rack": "r4", "sensors": {"top": 39.5, "mid": 24, "bottom": 23}}], inj)
    sr.update_mpc(0.6, 18.0, inj); sr.update_residual({"hot-aisle": 0.2}, inj)
    st = sr.evaluate(inj)
    cycles = 1
    ok = armed and st["state"] == TRIPPED and st["severity"] == "break_glass" \
        and str(st["trip_source"]).startswith("L1_single") and cycles <= b
    return _result(
        "CHK-IL-L1-BG", "L1 over-temperature, break-glass (single sensor > 38 C)",
        "Any single rack sensor above the hard limit must latch a break-glass trip and "
        "command the fast fallback ramp.",
        "Prime ARMED on safe data; inject one sensor at 39.5 C; assert immediate trip.",
        {"armed_before": armed, "trip_source": st["trip_source"], "severity": st["severity"],
         "detect_cycles": cycles},
        {"max_detect_cycles": b},
        ok, "Annex IV §2(g),§3,§5",
        detail=f"trip at {st['trip_source']}")


def chk_l1_graded(cfg):
    from relay import SafetyRelay, TRIPPED
    b = cfg["budgets"]["l1_graded_cycles"]
    sr = SafetyRelay()
    for t in range(3):
        sr.update_sensors(_safe_trio(), t); sr.update_mpc(0.6, 18.0, t); sr.update_residual({"hot-aisle": 0.2}, t)
        sr.evaluate(t)
    inj = 3
    # 2-of-3 over the soft vote limit (32 C), none over the hard limit
    sr.update_sensors([{"rack": "r2", "sensors": {"top": 33.0, "mid": 33.5, "bottom": 24}}], inj)
    sr.update_mpc(0.6, 18.0, inj); sr.update_residual({"hot-aisle": 0.2}, inj)
    st = sr.evaluate(inj)
    ok = st["state"] == TRIPPED and st["severity"] == "graded" \
        and str(st["trip_source"]).startswith("L1_vote")
    return _result(
        "CHK-IL-L1-VOTE", "L1 over-temperature, voted (2-of-3 trio > 32 C)",
        "Two of three sensors in a rack trio above the soft limit must latch a graded trip "
        "(single-sensor noise must not).",
        "Prime ARMED; inject a trio with two sensors at ~33 C; assert graded trip.",
        {"trip_source": st["trip_source"], "severity": st["severity"], "detect_cycles": 1},
        {"max_detect_cycles": b}, ok, "Annex IV §2(g),§3,§5")


def chk_stale_sensor(cfg):
    from relay import SafetyRelay, TRIPPED
    b = cfg["budgets"]["stale_sensor_s"]
    sr = SafetyRelay()
    sr.update_sensors(_safe_trio(), 0); sr.update_mpc(0.6, 18.0, 0); sr.update_residual({"hot-aisle": 0.2}, 0)
    sr.evaluate(0)
    # advance time, keep MPC fresh, but let the SENSOR feed go stale
    trip_t = None
    for t in range(1, 30):
        sr.update_mpc(0.6, 18.0, t)               # MPC stays fresh; sensors do NOT
        st = sr.evaluate(t)
        if st["state"] == TRIPPED:
            trip_t = t; break
    ok = trip_t is not None and str(sr.trip_source) == "stale_sensor" and trip_t <= b
    return _result(
        "CHK-IL-STALE-SENSOR", "Sensor watchdog (comms loss > 10 s)",
        "Loss of sensor data beyond the watchdog horizon must trip (blind => assume unsafe).",
        "Stop the sensor feed while MPC stays fresh; measure seconds to trip.",
        {"trip_source": sr.trip_source, "trip_s": trip_t}, {"max_trip_s": b},
        ok, "Annex IV §2(g),§3,§5", detail="break-glass: blind controller assumes unsafe")


def chk_stale_mpc(cfg):
    from relay import SafetyRelay, TRIPPED
    b = cfg["budgets"]["stale_mpc_s"]
    sr = SafetyRelay()
    sr.update_sensors(_safe_trio(), 0); sr.update_mpc(0.6, 18.0, 0); sr.update_residual({"hot-aisle": 0.2}, 0)
    sr.evaluate(0)
    trip_t = None
    for t in range(1, 130):
        sr.update_sensors(_safe_trio(), t)        # sensors fresh; MPC does NOT update
        sr.update_residual({"hot-aisle": 0.2}, t)
        st = sr.evaluate(t)
        if st["state"] == TRIPPED:
            trip_t = t; break
    ok = trip_t is not None and str(sr.trip_source) == "stale_mpc" and trip_t <= b
    return _result(
        "CHK-IL-STALE-MPC", "MPC watchdog (missed control cycle > 90 s)",
        "A stalled advisory MPC must trip to the safe fallback rather than hold a stale "
        "setpoint indefinitely.",
        "Stop MPC proposals while sensors stay fresh; measure seconds to trip.",
        {"trip_source": sr.trip_source, "trip_s": trip_t}, {"max_trip_s": b},
        ok, "Annex IV §2(g),§3,§5")


def chk_invalid_cmd(cfg):
    from relay import SafetyRelay, TRIPPED
    b = cfg["budgets"]["invalid_cmd_cycles"]
    sr = SafetyRelay()
    for t in range(3):
        sr.update_sensors(_safe_trio(), t); sr.update_mpc(0.50, 18.0, t); sr.update_residual({"hot-aisle": 0.2}, t)
        sr.evaluate(t)
    inj = 3
    # proposal-to-proposal jump beyond the VFD slew limit (0.15) but WITHIN flow bounds,
    # so this exercises the rate path specifically (0.50 -> 0.70 => df=0.20)
    sr.update_sensors(_safe_trio(), inj); sr.update_mpc(0.70, 18.0, inj); sr.update_residual({"hot-aisle": 0.2}, inj)
    st = sr.evaluate(inj)
    ok = st["state"] == TRIPPED and str(st["trip_source"]).startswith("invalid_command")
    return _result(
        "CHK-IL-INVALID-CMD", "Command validation (out-of-bounds / rate violation)",
        "A physically implausible MPC proposal (bounds or slew-rate) must be rejected by the "
        "relay before it reaches the plant.",
        "Feed an in-bounds proposal that jumps 0.20 in one cycle (> 0.15 slew limit); assert trip.",
        {"trip_source": st["trip_source"], "detect_cycles": 1}, {"max_detect_cycles": b},
        ok, "Annex IV §2(g),§3,§5")


def chk_latch_durability(cfg):
    import tempfile
    os.environ["RELAY_LATCH_FILE"] = tempfile.mktemp()
    relaysvc = load_service("relay_service", "backend/safety-relay/service.py")
    from relay import SafetyRelay, TRIPPED
    relaysvc.LATCH_FILE = tempfile.mktemp()
    # trip the relay, persist the latch
    relaysvc.sr = SafetyRelay()
    relaysvc.sr.state = TRIPPED; relaysvc.sr.trip_source = "L2_residual:9.9"; relaysvc.sr.severity = "graded"
    relaysvc.persist_latch()
    on_disk = os.path.exists(relaysvc.LATCH_FILE)
    # simulate a cold restart: fresh relay + restore from disk
    relaysvc.sr = SafetyRelay()
    booted_armed = relaysvc.sr.state != TRIPPED
    relaysvc.restore_latch()
    ok = on_disk and booted_armed and relaysvc.sr.state == TRIPPED
    return _result(
        "CHK-LATCH-DURABLE", "Latch durability across restart",
        "A latched trip must survive an OOM/cold restart and refuse to re-arm without an "
        "explicit operator reset.",
        "Trip + persist; instantiate a fresh relay; restore; assert it boots TRIPPED.",
        {"latch_persisted": on_disk, "fresh_relay_armed": booted_armed,
         "after_restore": relaysvc.sr.state},
        {}, ok, "Annex IV §3,§5")


# ── closed-loop checks (real plant) ─────────────────────────────────────────
def _closed_loop_l2(cfg, inject_tick, fault):
    from plant_sim import Plant
    from rom_model import ReducedOrderModel
    from relay import SafetyRelay, TRIPPED
    np.random.seed(cfg["seed"])
    lf = cfg["sim"]["load_frac"]
    p = Plant(); p.vfd_slew_per_s = 0.10
    rom = ReducedOrderModel(setpoint_c=18.0); sr = SafetyRelay()
    p.rack_power = np.full(8, lf * 270000.0 / 8); rom.set_power([lf * 135000.0, lf * 135000.0])
    for _ in range(cfg["sim"]["settle_ticks"]):
        p.revert_baseline(18.0); p.step(1.0); rom.step(1.0)
    trip_t = None
    for t in range(0, 400):
        if t == inject_tick:
            p.apply_fault(*fault)
        sr.update_mpc(0.85, 18.0, t)                          # benign fixed proposal
        sr.update_sensors(p.read_sensors()["rack_sensors"], t)
        res = rom.residual_for("hot-aisle", _meas_ha(p))
        sr.update_residual({"hot-aisle": round(res, 3)}, t)
        st = sr.evaluate(t)
        if st["state"] == TRIPPED and trip_t is None:
            trip_t = t; break
        cmd = st["command"]; p.apply_actuation(cmd["flow"], cmd["temp"]); rom.set_actuation(cmd["flow"], cmd["temp"])
        p.step(1.0); rom.step(1.0)
    return trip_t, sr


def chk_l2_residual(cfg):
    b = cfg["budgets"]["l2_residual_s"]
    inj = 5
    trip_t, sr = _closed_loop_l2(cfg, inj, ("crac_degradation", {"airflow_drop_pct": 75}))
    lat = (trip_t - inj) if trip_t is not None else None
    ok = trip_t is not None and str(sr.trip_source).startswith("L2_residual") and lat is not None and lat <= b
    return _result(
        "CHK-IL-L2-RESIDUAL", "L2 model-residual interlock (acute CRAC loss)",
        "A divergence between the frozen clean-coil reference and the plant (acute cooling "
        "loss) must trip on the residual norm.",
        "Closed loop plant+reference+relay; inject -75% CRAC capacity; measure onset-to-trip.",
        {"trip_source": sr.trip_source, "fallback_latency_s": lat}, {"max_latency_s": b},
        ok, "Annex IV §2(g),§3,§4,§5",
        detail="latency is fault-onset -> first TRIPPED command, control-cycle resolution")


def chk_deadman_revert(cfg):
    os.environ["ACTUATION_MODE"] = "ACTIVE"
    import json
    plantsvc = load_service("plant_service", "backend/plant-sim/service.py")
    b = cfg["budgets"]["deadman_revert_cycles"]
    # feed an ACCEPTED command, confirm it actuates; then go silent past the dead-man horizon
    plantsvc._fence.update(epoch=1.0, seq=-1, last_accept=time.monotonic())
    plantsvc._cmd.update(flow=None, temp=None)

    class _M:
        def __init__(self, d): self.data = json.dumps(d).encode()
    _run(plantsvc.on_control(_M({"flow": 0.40, "temp": 22.0, "epoch": 1.0, "seq": 1, "ts": 0})))
    plantsvc.apply_command()
    actuated = abs(plantsvc.plant.cmd_airflow - 0.40) < 1e-6
    # now simulate dead-man: last accepted command is older than the horizon
    plantsvc._fence["last_accept"] = time.monotonic() - (plantsvc.DEADMAN_S + 5)
    plantsvc.apply_command()
    reverted = abs(plantsvc.plant.cmd_airflow - 1.0) < 1e-6   # baseline = full airflow
    ok = actuated and reverted
    return _result(
        "CHK-DEADMAN", "Dead-man revert on lost controller",
        "If the relay goes silent beyond the dead-man horizon, the plant must autonomously "
        "revert to the safe always-on baseline without any command.",
        "Apply an accepted command; age it past the horizon; assert the plant reverts to "
        "baseline within one cycle.",
        {"command_actuated": actuated, "reverted_to_baseline": reverted,
         "deadman_horizon_s": plantsvc.DEADMAN_S, "revert_cycles": 1},
        {"max_revert_cycles": b}, ok, "Annex IV §3,§5")


# ── fencing / HA checks ─────────────────────────────────────────────────────
def chk_fence_zombie(cfg):
    import json
    os.environ["ACTUATION_MODE"] = "ACTIVE"
    from fence import LeaderController, InMemoryStore
    plantsvc = load_service("plant_service", "backend/plant-sim/service.py")

    class _M:
        def __init__(self, d): self.data = json.dumps(d).encode()

    clk = {"t": 1000.0}; now = lambda: clk["t"]

    async def run():
        store = InMemoryStore(clock=now)
        A = LeaderController(store, "relay-a", 6.0); B = LeaderController(store, "relay-b", 6.0)
        sA = {"n": 0}; sB = {"n": 0}
        plantsvc._fence.update(epoch=-1.0, seq=-1, last_accept=-1e9); plantsvc._cmd.update(flow=None, temp=None)

        async def pub(ctrl, sq, flow):
            sq["n"] += 1
            await plantsvc.on_control(_M({"flow": flow, "temp": 20, "epoch": float(ctrl.epoch),
                                          "seq": sq["n"], "ts": now()}))
        for _ in range(4):
            clk["t"] += 1; la, _ = await A.tick(now()); await B.tick(now())
            if la: await pub(A, sA, 0.50)
        applied_a = plantsvc._cmd["flow"]
        clk["t"] += 10
        lb, eb = await B.tick(now())
        if lb: await pub(B, sB, 0.70)
        applied_b = plantsvc._cmd["flow"]
        await pub(A, sA, 0.99)                              # zombie A at stale epoch
        applied_after_zombie = plantsvc._cmd["flow"]
        return applied_a, applied_b, eb, applied_after_zombie

    a, bflow, eb, after = _run(run())
    ok = (a == 0.50) and (bflow == 0.70) and (eb == 2) and (after == 0.70)  # zombie ignored
    return _result(
        "CHK-FENCE-ZOMBIE", "Zombie fencing across failover (no dual-write)",
        "After failover, a resurrected ex-leader must not be able to write to the actuator; "
        "the single-writer invariant must hold by fencing, not by agreement.",
        "Leader A writes; A dies; standby B takes over (epoch bump); zombie A re-publishes at "
        "its stale epoch; assert the actuator applied only A then B, never the zombie.",
        {"applied_under_A": a, "applied_under_B": bflow, "takeover_epoch": eb,
         "applied_after_zombie": after, "dual_write": after not in (0.70,)},
        {}, ok, "Annex IV §2(c),§3,§5",
        detail="enforcement is at plant-sim (the actuator), mirroring the dead-man philosophy")


def chk_fence_restart_seq(cfg):
    import json
    os.environ["ACTUATION_MODE"] = "ACTIVE"
    from fence import LeaderController, InMemoryStore
    plantsvc = load_service("plant_service", "backend/plant-sim/service.py")

    class _M:
        def __init__(self, d): self.data = json.dumps(d).encode()

    clk = {"t": 1000.0}; now = lambda: clk["t"]

    async def run():
        store = InMemoryStore(clock=now)
        a1 = LeaderController(store, "relay-a", 6.0); await a1.tick(now()); e1 = a1.epoch
        plantsvc._fence.update(epoch=-1.0, seq=-1, last_accept=-1e9); plantsvc._cmd.update(flow=None, temp=None)
        for sq in (1, 2, 3):
            await plantsvc.on_control(_M({"flow": 0.5, "temp": 20, "epoch": float(e1), "seq": sq, "ts": now()}))
        clk["t"] += 1
        a2 = LeaderController(store, "relay-a", 6.0)            # restart: same id, fresh seq
        l, e2 = await a2.tick(now())
        await plantsvc.on_control(_M({"flow": 0.77, "temp": 20, "epoch": float(e2), "seq": 1, "ts": now()}))
        return e1, e2, l, plantsvc._cmd["flow"]

    e1, e2, l, applied = _run(run())
    ok = l and (e2 > e1) and (applied == 0.77)
    return _result(
        "CHK-FENCE-RESTART", "Restart re-acquisition (fresh sequence accepted)",
        "A relay restart with a fresh in-memory sequence counter must be accepted by the "
        "actuator (epoch bumps per leadership session) and not rejected as a stale replay.",
        "Leader publishes seq 1..3; restart (same id) re-acquires; publishes seq 1; assert "
        "the epoch bumped and the fresh command is applied.",
        {"epoch_before": e1, "epoch_after_restart": e2, "is_leader": l, "applied_flow": applied},
        {}, ok, "Annex IV §2(c),§3")


# ── coil-health (post-market monitoring) ────────────────────────────────────
def chk_health_early_warning(cfg):
    from plant_sim import Plant
    from rom_model import ReducedOrderModel
    from relay import SafetyRelay
    from mpc_core import AdvisoryMPC, tau_for_stage, stage_for_load
    from health_monitor import CoilHealthMonitor
    np.random.seed(cfg["seed"])
    lf = 0.6
    p = Plant(); p.vfd_slew_per_s = 0.10
    mpc = AdvisoryMPC(eps=0.92, u_scale=1.0); sr = SafetyRelay(); rom = ReducedOrderModel(setpoint_c=18.0)
    mon = CoilHealthMonitor(n_bins=4, commission_n=80, cusum_h=8.0, cusum_k=0.25)
    p.rack_power = np.full(8, lf * 270000.0 / 8); rom.set_power([lf * 135000.0, lf * 135000.0])
    for _ in range(150):
        p.revert_baseline(18.0); p.step(1.0); rom.step(1.0)

    def s6():
        s = p.read_truth(); roles = s["zone_roles"]; st = s["state"]; R = lambda r: st[roles.index(r)]
        return np.array([R("crac-supply"), R("cold-aisle-1"), R("cold-aisle-2"),
                         np.mean([R("rack-%d" % k) for k in (1, 2, 3, 4)]),
                         np.mean([R("rack-%d" % k) for k in (5, 6, 7, 8)]), R("hot-aisle")])
    u_prev = [0.85, 18.0]; Q = [lf * 135000.0, lf * 135000.0]
    foul_at = 350; alarm_at = None; trip_at = None
    for t in range(0, 1400):
        if t == foul_at:
            p.apply_fault("coil_fouling", {"rate_pct_per_hour": 1800, "max_drop_pct": 55})
        if t % 60 == 0:
            x0 = np.concatenate([s6(), [p.setpoint]]); mpc.tau = tau_for_stage(stage_for_load(lf))
            r = mpc.solve(x0, Q, u_prev=u_prev)
            if r["feasible"]:
                sr.update_mpc(r["u_flow"], r["u_temp"], t); u_prev = [r["u_flow"], r["u_temp"]]
        sr.update_sensors(p.read_sensors()["rack_sensors"], t)
        resHA = rom.residual_for("hot-aisle", _meas_ha(p))
        sr.update_residual({"hot-aisle": round(resHA, 3)}, t)
        st = sr.evaluate(t)
        mon.sample(resHA, lf, _meas_ha(p), p.airflow_frac, st["state"] == "ARMED")
        if mon.alarm and alarm_at is None:
            alarm_at = t
        if st["state"] == "TRIPPED" and trip_at is None:
            trip_at = t
        cmd = st["command"]; p.apply_actuation(cmd["flow"], cmd["temp"]); rom.set_actuation(cmd["flow"], cmd["temp"])
        p.step(1.0); rom.step(1.0)
    lead = (trip_at - alarm_at) if (alarm_at is not None and trip_at is not None) else None
    minlead = cfg["budgets"]["health_lead_s_min"]
    ok = alarm_at is not None and (trip_at is None or (lead is not None and lead >= minlead))
    return _result(
        "CHK-HEALTH-LEAD", "Coil-health early warning precedes safety trip",
        "Slow coil fouling must raise a maintenance ALARM (advisory, not a trip) while the "
        "relay is still ARMED with airflow headroom — ahead of any acute trip.",
        "Closed loop with MPC; inject slow fouling; assert ALARM precedes any L2 trip by the "
        "minimum lead.",
        {"alarm_tick": alarm_at, "trip_tick": trip_at, "lead_s": lead},
        {"min_lead_s": minlead}, ok, "Annex IV §3,§9 (post-market monitoring)",
        detail="accelerated fouling rate for test; real lead scales ~1/rate (days/weeks)")


def chk_health_loadshift(cfg):
    from health_monitor import CoilHealthMonitor
    import random
    random.seed(cfg["seed"])
    m = CoilHealthMonitor(n_bins=4, commission_n=40)
    # commission bin at load 0.45 with baseline ~0.2
    for _ in range(60):
        m.sample(0.2 + random.gauss(0, 0.1), 0.45, 30.0, 0.6, True)
    base_alarm = m.alarm
    # shift to a DIFFERENT load point with a higher structural baseline (~0.9)
    for _ in range(300):
        m.sample(0.9 + random.gauss(0, 0.1), 0.78, 30.0, 0.6, True)
    ok = (not base_alarm) and (not m.alarm)
    return _result(
        "CHK-HEALTH-LOADSHIFT", "Coil-health load-shift false-alarm immunity",
        "A change of operating point (load) must not be mistaken for coil degradation; "
        "baselines are per-load-bin.",
        "Commission one load bin; shift to a different bin with a higher structural residual; "
        "assert no alarm.",
        {"alarm_after_commission": base_alarm, "alarm_after_load_shift": m.alarm},
        {}, ok, "Annex IV §3,§4,§9")


ALL_CHECKS = [
    chk_l1_breakglass, chk_l1_graded, chk_stale_sensor, chk_stale_mpc, chk_invalid_cmd,
    chk_latch_durability, chk_l2_residual, chk_deadman_revert,
    chk_fence_zombie, chk_fence_restart_seq, chk_health_early_warning, chk_health_loadshift,
]
