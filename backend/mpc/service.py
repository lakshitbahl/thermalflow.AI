"""
service.py — Gate A advisory MPC service (entrypoint).

REACTIVE by design: solves + publishes only in response to a fresh rom.rls.<tenant>.pred
that advances the dt=60s controller clock (MPC_DT_S). If the rom.rls stream drops, this service
simply goes idle — it never republishes stale or zeroed setpoints. ZERO ACTUATION:
plant-sim is never touched; we publish what the MPC *would* command for the dashboard.

NATS in:
  rom.rls.<tenant>.pred   live 6-zone ROM state + frozen (eps, U_scale)
  slurm.nodes.<tenant>    disturbance (IT heat load)
NATS out:
  rom.mpc.<tenant>.advisory   baseline vs advisory u0 + predicted Np trajectory
"""

import asyncio
import json
import os

import numpy as np
import nats

from rom_model_rls import ROLE, power_from_nodes
from mpc_core import AdvisoryMPC, tau_for_stage, stage_for_load

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT = os.environ.get("TENANT", "demo")
MPC_DT = float(os.environ.get("MPC_DT_S", "60.0"))
T_LIMIT = float(os.environ.get("T_LIMIT_C", "30.0"))
TAU_WATER = float(os.environ.get("TAU_WATER_S", "150.0"))   # runtime-configurable, not hardcoded
FLOW_MAX = float(os.environ.get("FLOW_MAX", "0.85"))        # CFD bypass threshold (raised-floor jets bypass >85%)
BASELINE = (1.0, 18.0)  # what the plant actually executes (100% airflow, 18C)

mpc = AdvisoryMPC(eps=0.92, u_scale=1.0, dt=MPC_DT, T_limit=T_LIMIT,
                  tau_water=TAU_WATER, flow_bounds=(0.3, FLOW_MAX))
nc = None
_state = {"air": None, "eps": 0.92, "u_scale": 1.0, "Q": [0.0, 0.0], "last_solve_ts": None}


def log(**kv):
    print(json.dumps({"svc": "mpc", **kv}), flush=True)


async def on_power(msg):
    try:
        p = json.loads(msg.data)
        if "nodes" in p:
            q = power_from_nodes(p["nodes"])
            if q is not None:
                _state["Q"] = q
    except Exception as e:  # noqa: BLE001
        log(event="power_parse_error", error=str(e))


async def on_rls(msg):
    """Cadence driver: solve on the MPC_DT (60s) clock, publish advisory (= relay proposal)."""
    try:
        d = json.loads(msg.data)
        ts = float(d.get("ts", 0.0))
        st = d.get("state", {})
        _state["air"] = [float(st[r]) for r in ROLE]
        if d.get("params"):
            _state["eps"] = float(d["params"]["eps"])
            _state["u_scale"] = float(d["params"]["u_scale"])

        last = _state["last_solve_ts"]
        if last is not None and (ts - last) < MPC_DT:
            return  # hold the controller clock
        _state["last_solve_ts"] = ts

        # frozen params drive the linearization (QP structure is unchanged)
        mpc.eps, mpc.u_scale = _state["eps"], _state["u_scale"]
        # tau_water gain schedule: BMS pump-stage (here a sim stand-in from load).
        # Only the linearization uses tau; the QP structure is untouched.
        load_frac = max(0.0, min(1.0, float(np.sum(_state["Q"])) / 270000.0))
        stage = stage_for_load(load_frac)
        mpc.tau = tau_for_stage(stage)
        x0 = np.array(_state["air"] + [BASELINE[1]])  # supply state = delivered baseline
        r = mpc.solve(x0, _state["Q"], u_prev=list(BASELINE))

        payload = {
            "ts": ts, "T_limit": T_LIMIT,
            "baseline": {"flow": BASELINE[0], "temp": BASELINE[1]},
            "params": {"eps": round(mpc.eps, 4), "u_scale": round(mpc.u_scale, 4), "pump_stage": stage, "tau_water": mpc.tau},
            "feasible": r["feasible"], "status": r.get("status"),
            "solve_ms": r.get("solve_ms"),
        }
        if r["feasible"]:
            payload.update({
                "advisory": {"flow": r["u_flow"], "temp": r["u_temp"]},
                "ha_pred": r["ha_pred"], "sup_pred": r["sup_pred"],
                "flow_plan": r["flow_plan"], "temp_plan": r["temp_plan"],
                "slack_max": round(r["slack_max"], 3),
            })
        else:
            # never publish stale/zeroed: advise the safe baseline on solver failure
            payload.update({"advisory": {"flow": BASELINE[0], "temp": BASELINE[1]},
                            "ha_pred": [], "fallback": True})
            log(event="solve_infeasible", status=r.get("status"))

        if nc is not None:
            await nc.publish(f"rom.mpc.{TENANT}.advisory", json.dumps(payload).encode())
    except Exception as e:  # noqa: BLE001
        log(event="rls_handler_error", error=str(e))


async def main():
    global nc
    for attempt in range(1, 16):
        try:
            nc = await nats.connect(NATS_URL)
            log(event="nats_connected", url=NATS_URL)
            break
        except Exception as e:  # noqa: BLE001
            log(event="nats_retry", attempt=attempt, error=str(e))
            await asyncio.sleep(3)
    if nc is None:
        raise SystemExit("mpc: cannot reach NATS after 15 attempts")

    await nc.subscribe(f"slurm.nodes.{TENANT}", cb=on_power)
    await nc.subscribe(f"rom.rls.{TENANT}.pred", cb=on_rls)
    log(event="started", dt_s=MPC_DT, T_limit=T_LIMIT, Np=mpc.Np,
        note="Gate A advisory; reactive to rom.rls; zero actuation")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
