"""
service.py — rom-shadow I/O layer (entrypoint).

Runs the 6-zone OPEN-LOOP ROM and publishes its prediction + residual.

NATS in:
  sensor.thermal.<tenant>   noisy measurements (used ONLY to compute residual)
  slurm.nodes.<tenant>      disturbance input (aggregated to 2 rack-mass powers)
                            (POWER_SUBJECT; auto-detects a `nodes` list or a
                             2-element `power` field, for the future measured
                             slurm.power.* / Prometheus stream)
NATS out (tenant in 3rd token so the BFF's *.*.<tenant>.> fan-out forwards them):
  rom.static.<tenant>.pred       6-zone state + per-rack inlet prediction
  rom.static.<tenant>.residual   measured - mapped-prediction, per sensor

BLIND TO TRUTH: this service must never subscribe to plant.truth.<tenant>.
The ROM advances one step per arriving sensor message, using the dt implied by
the message timestamps (so it is correct under any plant SIM_SPEED). Sensors are
used to compute the residual, never to correct the state — open-loop by design.
"""

import asyncio
import json
import os

import nats

from rom_model import ReducedOrderModel, power_from_nodes

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT = os.environ.get("TENANT", "demo")
SENSOR_SUBJECT = os.environ.get("SENSOR_SUBJECT", f"sensor.thermal.{TENANT}")
POWER_SUBJECT = os.environ.get("POWER_SUBJECT", f"slurm.nodes.{TENANT}")
SETPOINT = float(os.environ.get("CRAC_SETPOINT_C", "18.0"))
ACTUATION_MODE = os.environ.get("ACTUATION_MODE", "SHADOW").upper()  # ACTIVE -> follow relay cmd
DT_MIN, DT_MAX = 0.1, 10.0  # clamp the inter-message dt against gaps/reorders

rom = ReducedOrderModel(setpoint_c=SETPOINT)
nc = None
_last_ts = None


def log(**kv):
    print(json.dumps({"svc": "rom-shadow", **kv}), flush=True)


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


async def on_control(msg):
    # actuation awareness: the L2 twin tracks the ACTUATED plant. But it must mirror
    # what plant-sim ACTUALLY does: in SHADOW the plant ignores commands and runs the
    # baseline, so the ROM must stay at baseline too (else it diverges and false-trips).
    # Only follow the command when the loop is genuinely closed (ACTIVE).
    if ACTUATION_MODE != "ACTIVE":
        return
    try:
        d = json.loads(msg.data)
        rom.set_actuation(d.get("flow"), d.get("temp"))
    except Exception as e:  # noqa: BLE001
        log(event="control_parse_error", error=str(e))


async def on_sensor(msg):
    global _last_ts
    try:
        s = json.loads(msg.data)
        ts = float(s.get("ts", 0.0))

        # advance ROM by the sim-time elapsed since the last reading (open-loop)
        if _last_ts is not None:
            dt = min(max(ts - _last_ts, DT_MIN), DT_MAX)
            rom.step(dt)
        _last_ts = ts

        # residual = measured - mapped prediction; skip invalid (dropout) sensors
        residuals, dropped = {}, []
        for item in s.get("rack_inlet_c", []) + s.get("zones", []):
            sid = item.get("sensor_id") or item.get("zone_id")
            if not item.get("valid", True):
                dropped.append(sid)
                continue
            r = rom.residual_for(sid, item["temp_c"])
            if r is not None:
                residuals[sid] = round(r, 3)

        if nc is not None:
            # Tenant in the 3rd token so the BFF's *.*.<tenant>.> forward matches.
            await nc.publish(f"rom.static.{TENANT}.pred", json.dumps({
                "ts": ts,
                "state": rom.predict_state(),
                "rack_inlet_pred_c": [round(x, 3) for x in rom.predict_rack_inlets()],
            }).encode())
            await nc.publish(f"rom.static.{TENANT}.residual", json.dumps({
                "ts": ts,
                "residuals": residuals,
                "dropped": dropped,
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
        raise SystemExit("rom-shadow: cannot reach NATS after 15 attempts")

    await nc.subscribe(POWER_SUBJECT, cb=on_power)
    await nc.subscribe(SENSOR_SUBJECT, cb=on_sensor)
    await nc.subscribe(f"plant.control.{TENANT}.setpoints", cb=on_control)
    log(event="started", sensor=SENSOR_SUBJECT, power=POWER_SUBJECT,
        setpoint_c=SETPOINT, note="actuation-aware (frozen params); blind to plant.truth")

    # idle forever; all work is in the subscription callbacks
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
