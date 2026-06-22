"""
service.py — rls-rom I/O layer (entrypoint).

Runs the gated-RLS 6-zone ROM and publishes its prediction + residual + the
identified parameters, for side-by-side A/B against the static rom-shadow.

NATS in:
  sensor.thermal.<tenant>        measurements (residual + RLS measurement vector)
  slurm.nodes.<tenant>           disturbance input
NATS out (model token = 'rls'; tenant 3rd token so the BFF forwards it):
  rom.rls.<tenant>.pred          6-zone state + per-rack inlet + identified params
  rom.rls.<tenant>.residual      measured - mapped prediction, per sensor

BLIND TO TRUTH: never subscribes to plant.truth.<tenant>. Open-loop prediction;
the RLS uses measurements to identify (eps, U_scale) under the dead-zone gate,
not to correct the state directly.
"""

import asyncio
import json
import os

import numpy as np
import nats

from rom_model_rls import RlsRom, power_from_nodes

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT = os.environ.get("TENANT", "demo")
SENSOR_SUBJECT = os.environ.get("SENSOR_SUBJECT", f"sensor.thermal.{TENANT}")
POWER_SUBJECT = os.environ.get("POWER_SUBJECT", f"slurm.nodes.{TENANT}")
SETPOINT = float(os.environ.get("CRAC_SETPOINT_C", "18.0"))
ACTUATION_MODE = os.environ.get("ACTUATION_MODE", "SHADOW").upper()  # ACTIVE -> freeze RLS
DT_MIN, DT_MAX = 0.1, 10.0

rom = RlsRom(setpoint_c=SETPOINT)
nc = None
_last_ts = None


def log(**kv):
    print(json.dumps({"svc": "rls-rom", **kv}), flush=True)


def measurement_vector(sensor):
    """Build the 6-zone (y, mask) the RLS regresses against, from sensor msg."""
    z = {q["zone_id"]: q for q in sensor.get("zones", [])}

    def get(zid):
        q = z.get(zid)
        return (q["temp_c"], True) if (q and q.get("valid", True)) else (np.nan, False)

    def rackmean(ks):
        vals = [z[f"rack-{k}"]["temp_c"] for k in ks if z.get(f"rack-{k}", {}).get("valid")]
        return (float(np.mean(vals)), True) if vals else (np.nan, False)

    pairs = [get("crac-supply"), get("cold-aisle-1"), get("cold-aisle-2"),
             rackmean([1, 2, 3, 4]), rackmean([5, 6, 7, 8]), get("hot-aisle")]
    y = np.array([p[0] for p in pairs])
    mask = np.array([p[1] for p in pairs])
    return y, mask


async def on_power(msg):
    try:
        p = json.loads(msg.data)
        if isinstance(p.get("power"), (list, tuple)) and len(p["power"]) == 2:
            rom.set_power(p["power"])
        elif "nodes" in p:
            q = power_from_nodes(p["nodes"])
            if q is not None:
                rom.set_power(q)
    except Exception as e:  # noqa: BLE001
        log(event="power_parse_error", error=str(e))


async def on_sensor(msg):
    global _last_ts
    try:
        s = json.loads(msg.data)
        ts = float(s.get("ts", 0.0))
        if _last_ts is not None:
            rom.step(min(max(ts - _last_ts, DT_MIN), DT_MAX))
        _last_ts = ts

        # gated RLS identification step — FROZEN in closed loop (ACTIVE) to avoid
        # closed-loop identification bias (u = MPC(x) correlates regressor with error).
        y, mask = measurement_vector(s)
        diag = rom.hold() if ACTUATION_MODE == "ACTIVE" else rom.update(y, mask)

        residuals, dropped = {}, []
        for item in s.get("rack_inlet_c", []) + s.get("zones", []):
            sid = item.get("sensor_id") or item.get("zone_id")
            if not item.get("valid", True):
                dropped.append(sid); continue
            r = rom.residual_for(sid, item["temp_c"])
            if r is not None:
                residuals[sid] = round(r, 3)

        if nc is not None:
            await nc.publish(f"rom.rls.{TENANT}.pred", json.dumps({
                "ts": ts, "state": rom.predict_state(),
                "rack_inlet_pred_c": [round(x, 3) for x in rom.predict_rack_inlets()],
                "params": {"eps": round(diag["eps"], 4), "u_scale": round(diag["u_scale"], 4)},
                "frozen": diag["frozen"], "trace_P": round(diag["trace_P"], 5),
            }).encode())
            await nc.publish(f"rom.rls.{TENANT}.residual", json.dumps({
                "ts": ts, "residuals": residuals, "dropped": dropped,
            }).encode())
    except Exception as e:  # noqa: BLE001
        log(event="sensor_handler_error", error=str(e))


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
        raise SystemExit("rls-rom: cannot reach NATS after 15 attempts")

    await nc.subscribe(POWER_SUBJECT, cb=on_power)
    await nc.subscribe(SENSOR_SUBJECT, cb=on_sensor)
    log(event="started", sensor=SENSOR_SUBJECT, power=POWER_SUBJECT,
        note="gated RLS (eps,U_scale); blind to plant.truth")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
