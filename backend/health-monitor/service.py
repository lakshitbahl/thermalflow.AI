"""
service.py — coil health monitor (slow degradation layer).

Consumes the FROZEN clean-coil residual (rom.static.<tenant>.residual), gates on the
reference regime (relay ARMED, quasi-steady, load in band), self-baselines, and raises a
maintenance ALARM on sustained upward drift. Publishes health.coil.<tenant>.status. Never
touches the relay or the actuator — this is advisory/maintenance only.

State is durable: weeks of accumulated CUSUM must survive a pod restart (same reasoning as
the relay latch). /commission re-zeros the baseline after a coil cleaning or a mode change.
"""
import asyncio
import json
import os
import time

import nats
from aiohttp import web

from health_monitor import CoilHealthMonitor

NATS_URL   = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT     = os.environ.get("TENANT", "demo")
HTTP_PORT  = int(os.environ.get("HEALTH_HTTP_PORT", "8092"))
STATE_FILE = os.environ.get("HEALTH_STATE_FILE", "/data/coil_health.json")
PERSIST_EVERY = int(os.environ.get("HEALTH_PERSIST_EVERY", "60"))

nc = None
mon = CoilHealthMonitor(
    band_lo=float(os.environ.get("HEALTH_BAND_LO", "0.40")),
    band_hi=float(os.environ.get("HEALTH_BAND_HI", "0.80")),
    commission_n=int(os.environ.get("HEALTH_COMMISSION_N", "120")),
    cusum_k=float(os.environ.get("HEALTH_CUSUM_K", "0.25")),
    cusum_h=float(os.environ.get("HEALTH_CUSUM_H", "8.0")),
)

# cached inputs (the residual stream is the sample trigger; the rest is context)
_ctx = {"ha": None, "load": None, "airflow": None, "armed": False}
_since_persist = 0


def log(**kv):
    print(json.dumps({"svc": "health-monitor", **kv}), flush=True)


def persist():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(mon.to_dict(), fh)
        os.replace(tmp, STATE_FILE)
    except Exception as e:  # noqa: BLE001
        log(event="persist_error", error=str(e))


def restore():
    try:
        if os.path.exists(STATE_FILE):
            mon.load_dict(json.load(open(STATE_FILE)))
            log(event="state_restored", commissioned=mon.commissioned,
                alarm=mon.alarm, cusum=round(mon.cusum, 2))
    except Exception as e:  # noqa: BLE001
        log(event="restore_error", error=str(e))


# ── NATS intake (context cached; residual drives the sample) ────────────────
async def on_sensor(msg):
    try:
        d = json.loads(msg.data)
        for z in d.get("zones", []):
            if z.get("zone_id") == "hot-aisle":
                _ctx["ha"] = z.get("temp_c")
        if "crac_airflow_pct" in d:
            _ctx["airflow"] = d["crac_airflow_pct"] / 100.0
    except Exception as e:  # noqa: BLE001
        log(event="sensor_parse_error", error=str(e))


async def on_slurm(msg):
    try:
        nodes = json.loads(msg.data).get("nodes", [])
        tot = sum(n.get("gpus_total", 0) for n in nodes)
        alloc = sum(n.get("gpus_alloc", 0) for n in nodes)
        if tot > 0:
            _ctx["load"] = alloc / tot
    except Exception as e:  # noqa: BLE001
        log(event="slurm_parse_error", error=str(e))


async def on_relay(msg):
    try:
        _ctx["armed"] = json.loads(msg.data).get("state") == "ARMED"
    except Exception as e:  # noqa: BLE001
        log(event="relay_parse_error", error=str(e))


async def on_residual(msg):
    global _since_persist
    try:
        r = json.loads(msg.data).get("residuals", {}).get("hot-aisle")
        if r is None or _ctx["ha"] is None or _ctx["load"] is None:
            return  # need full context before sampling
        was_alarm, was_comm = mon.alarm, mon.commissioned
        st = mon.sample(float(r), _ctx["load"], _ctx["ha"],
                        _ctx["airflow"] or 0.0, _ctx["armed"])
        if nc is not None:
            await nc.publish(f"health.coil.{TENANT}.status", json.dumps(st).encode())
        # persist on meaningful transitions + periodically
        _since_persist += 1
        if mon.alarm != was_alarm or mon.commissioned != was_comm or _since_persist >= PERSIST_EVERY:
            persist(); _since_persist = 0
            if mon.alarm and not was_alarm:
                log(event="coil_alarm", drift_c=st["drift_c"], severity=st["severity"])
            if mon.commissioned and not was_comm:
                log(event="commissioned", baseline_c=st["baseline_c"])
    except Exception as e:  # noqa: BLE001
        log(event="residual_parse_error", error=str(e))


# ── operator HTTP ───────────────────────────────────────────────────────────
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def h_health(_):
    return web.json_response({"ok": True})


async def h_status(_):
    return web.json_response(mon.status(False, _ctx["airflow"] or 0.0))


async def h_commission(_):
    mon.recommission()
    persist()
    log(event="recommissioned")
    return web.json_response({"recommissioned": True, "status": mon.status()})


def build_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([
        web.get("/health", h_health),
        web.get("/status", h_status),
        web.post("/commission", h_commission),
        web.options("/{tail:.*}", lambda r: web.Response(status=204)),
    ])
    return app


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
        raise SystemExit("health-monitor: cannot reach NATS after 15 attempts")

    restore()
    await nc.subscribe(f"rom.static.{TENANT}.residual", cb=on_residual)
    await nc.subscribe(f"sensor.thermal.{TENANT}", cb=on_sensor)
    await nc.subscribe(f"slurm.nodes.{TENANT}", cb=on_slurm)
    await nc.subscribe(f"safety.relay.{TENANT}.status", cb=on_relay)

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log(event="health_started", port=HTTP_PORT, band=[mon.band_lo, mon.band_hi])
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
