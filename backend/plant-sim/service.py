"""
service.py — plant-sim I/O layer (entrypoint).

Wires the pure Plant core to:
  - NATS in:  slurm.nodes.<tenant>      (workload power, ZOH between updates)
  - NATS out: sensor.thermal.<tenant>   (ROM-visible noisy/faulted readings)
              plant.truth.<tenant>      (ground truth; ROM MUST NOT subscribe)
  - HTTP:     fault-injection API on :8090

Four decoupled clocks (see Phase 0 notes):
  PLANT_DT_S       integration step (backward-Euler, stable)        default 1.0
  SENSOR_PUBLISH_S sensor/truth publish cadence                     default 1.0
  SIM_SPEED        sim-seconds per wall-second (time-warp campaigns) default 1.0
  (slurm workload cadence is owned by slurm-sim)
"""

import asyncio
import json
import os
import time

import nats
from aiohttp import web

from plant_sim import Plant

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT = os.environ.get("TENANT", "demo")
DT = float(os.environ.get("PLANT_DT_S", "1.0"))
PUBLISH_S = float(os.environ.get("SENSOR_PUBLISH_S", "1.0"))
SIM_SPEED = float(os.environ.get("SIM_SPEED", "1.0"))
SETPOINT = float(os.environ.get("CRAC_SETPOINT_C", "18.0"))
# Gate B actuation: SHADOW (default) receives + logs relay commands but does NOT apply
# them (iron disconnected); ACTIVE applies them. Dead-man reverts to baseline if the
# relay's command stream goes silent past DEADMAN_S — survives relay death / partition.
ACTUATION_MODE = os.environ.get("ACTUATION_MODE", "SHADOW").upper()
DEADMAN_S = float(os.environ.get("ACTUATION_DEADMAN_S", "90.0"))
VFD_SLEW_PER_S = float(os.environ.get("VFD_SLEW_PER_S", "0.10"))  # fan accel limit
HTTP_PORT = int(os.environ.get("PLANT_HTTP_PORT", "8090"))

plant = Plant(setpoint_c=SETPOINT)
plant.vfd_slew_per_s = VFD_SLEW_PER_S
nc = None  # set on connect


def log(**kv):
    print(json.dumps({"svc": "plant-sim", **kv}), flush=True)


# ── HTTP fault API ───────────────────────────────────────────────────────────
async def h_health(_):
    return web.json_response({"status": "ok", "sim_time": plant.sim_time})


async def h_faults(_):
    return web.json_response(plant.describe_faults())


async def h_state(_):
    return web.json_response(plant.read_truth())


async def h_fault(request):
    try:
        body = await request.json()
        ftype = body.get("type")
        active = plant.apply_fault(ftype, body)
        log(event="fault_injected", type=ftype, params={k: v for k, v in body.items() if k != "type"})
        return web.json_response({"applied": ftype, "active": active})
    except (KeyError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)


async def h_clear(_):
    active = plant.clear_faults()
    log(event="faults_cleared")
    return web.json_response({"active": active})


@web.middleware
async def cors_mw(request, handler):
    # Dev fault API is hit cross-origin from the dashboard (localhost:3000 ->
    # :8090). Permissive CORS is fine for a local-only simulation control plane.
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def build_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([
        web.get("/health", h_health),
        web.get("/faults", h_faults),
        web.get("/state", h_state),
        web.post("/fault", h_fault),
        web.post("/fault/clear", h_clear),
        web.options("/{tail:.*}", lambda r: web.Response(status=204)),
    ])
    return app


# ── NATS workload intake ─────────────────────────────────────────────────────
async def on_slurm(msg):
    try:
        payload = json.loads(msg.data)
        plant.set_rack_power_from_nodes(payload.get("nodes", []))
    except Exception as e:  # noqa: BLE001 - never let a bad message kill the loop
        log(event="slurm_parse_error", error=str(e))


# ── relay command intake (Gate B) ────────────────────────────────────────────
# plant.control.<tenant>.setpoints — the relay is the writer, but plant-sim is the
# AUTHORITY that enforces single-writer by TOPOLOGY: it tracks the highest fencing epoch
# seen and REJECTS any command carrying a lower epoch (a fenced zombie / stale leader).
# Within an epoch, seq must advance (rejects replays / stuck commands). Liveness for the
# dead-man is the last ACCEPTED command — a rejected zombie can't keep the dead-man fed.
_cmd = {"flow": None, "temp": None}
_fence = {"epoch": -1.0, "seq": -1, "last_accept": -1e9}


async def on_control(msg):
    try:
        d = json.loads(msg.data)
        epoch = float(d.get("epoch", 0)); seq = int(d.get("seq", 0))
        if epoch < _fence["epoch"]:
            log(event="cmd_rejected", reason="fenced", epoch=epoch, current=_fence["epoch"])
            return
        if epoch > _fence["epoch"]:
            log(event="fence_takeover", old=_fence["epoch"], new=epoch)
            _fence["epoch"] = epoch; _fence["seq"] = -1      # new leader -> reset seq baseline
        if seq <= _fence["seq"]:
            log(event="cmd_rejected", reason="stale_seq", seq=seq, last=_fence["seq"])
            return
        _fence["seq"] = seq
        _fence["last_accept"] = time.monotonic()
        _cmd["flow"], _cmd["temp"] = float(d["flow"]), float(d["temp"])
    except Exception as e:  # noqa: BLE001
        log(event="control_parse_error", error=str(e))


def apply_command():
    """SHADOW logs but does not actuate; ACTIVE applies a fresh ACCEPTED command; a stale
    command (no accepted command past DEADMAN_S) or SHADOW reverts to the safe baseline."""
    have = _cmd["flow"] is not None
    fresh = have and (time.monotonic() - _fence["last_accept"]) <= DEADMAN_S
    if ACTUATION_MODE == "ACTIVE" and fresh:
        plant.apply_actuation(_cmd["flow"], _cmd["temp"])
    else:
        plant.revert_baseline(SETPOINT)


# ── Simulation loop ──────────────────────────────────────────────────────────
async def sim_loop():
    steps_per_publish = max(1, round(PUBLISH_S / DT))
    n = 0
    log(event="sim_started", dt_s=DT, publish_s=PUBLISH_S, sim_speed=SIM_SPEED, tenant=TENANT)
    while True:
        apply_command()
        plant.step(DT)
        n += 1
        if n % steps_per_publish == 0 and nc is not None:
            try:
                await nc.publish(f"sensor.thermal.{TENANT}", json.dumps(plant.read_sensors()).encode())
                await nc.publish(f"plant.truth.{TENANT}", json.dumps(plant.read_truth()).encode())
            except Exception as e:  # noqa: BLE001
                log(event="publish_error", error=str(e))
        # time-warp: advance DT of sim-time per (DT / SIM_SPEED) of wall-time
        await asyncio.sleep(DT / SIM_SPEED)


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
        raise SystemExit("plant-sim: cannot reach NATS after 15 attempts")

    await nc.subscribe(f"slurm.nodes.{TENANT}", cb=on_slurm)
    await nc.subscribe(f"plant.control.{TENANT}.setpoints", cb=on_control)
    log(event="actuation_mode", mode=ACTUATION_MODE, deadman_s=DEADMAN_S)

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log(event="http_started", port=HTTP_PORT)

    await sim_loop()


if __name__ == "__main__":
    asyncio.run(main())
