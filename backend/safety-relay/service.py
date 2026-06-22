"""
service.py — Safety-relay service (entrypoint).

The relay is the SOLE PUBLISHER to plant.control.<tenant>.setpoints. It sits in series:

   MPC ──proposal──▶ rom.mpc.<tenant>.advisory
                          │
   sensor.thermal ──────▶ │ (L1, staleness)
   rom.static.residual ─▶ │ (L2)
                          ▼
                   ┌──────────────┐
                   │ safety-relay │── plant.control.<tenant>.setpoints ─▶ plant-sim
                   │ (sole writer)│── safety.relay.<tenant>.status ─────▶ dashboard
                   └──────────────┘

Evaluation runs on a fast 1s wall-clock loop, independent of the MPC's 60s control
clock — a sensor breach must trip within ~1s, not wait for the next MPC cycle.
Operator reset is an HTTP POST /reset (the only exit from a latched trip).
"""
import asyncio
import json
import os
import time

import nats
from aiohttp import web

from relay import SafetyRelay, TRIPPED, ARMED
from fence import LeaderController, NatsKvStore, LATCH_KEY

NATS_URL  = os.environ.get("NATS_URL", "nats://nats:4222")
TENANT    = os.environ.get("TENANT", "demo")
EVAL_S    = float(os.environ.get("RELAY_EVAL_S", "1.0"))
HTTP_PORT = int(os.environ.get("RELAY_HTTP_PORT", "8091"))
LATCH_FILE = os.environ.get("RELAY_LATCH_FILE", "/data/relay_latch.json")
# HA (opt-in): two relays share a fencing lease; only the holder publishes. Default OFF =
# single relay with a boot-time epoch (already fences a rolling-restart predecessor).
HA_ENABLED = os.environ.get("RELAY_HA", "false").lower() == "true"
RELAY_ID   = os.environ.get("RELAY_ID", os.environ.get("HOSTNAME", "relay-" + str(os.getpid())))
LEASE_TTL  = float(os.environ.get("RELAY_LEASE_TTL", "6.0"))
KV_BUCKET  = os.environ.get("RELAY_KV_BUCKET", "thermalflow_relay")

ctrl = None        # LeaderController when HA is enabled
_is_leader = True  # single-relay default: always the writer

nc = None
sr = SafetyRelay()

# Fencing token: defaults to boot wall-time so a freshly restarted instance always
# outranks a zombie predecessor (monotonic across restarts). The lease controller
# raises this to a coordinated epoch when HA is enabled.
EPOCH = time.time()
_seq = 0


def log(**kv):
    print(json.dumps({"svc": "safety-relay", **kv}), flush=True)


# ── durable latch: a TRIPPED state must survive an OOM/cold restart ──────────
# Without this, a crash after a trip reboots ARMED and silently hands authority
# back to the MPC with no operator ack — defeating the manual-reset latch.
def persist_latch():
    try:
        os.makedirs(os.path.dirname(LATCH_FILE), exist_ok=True)
        tmp = LATCH_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"state": sr.state, "trip_source": sr.trip_source,
                       "severity": sr.severity}, fh)
        os.replace(tmp, LATCH_FILE)   # atomic: never a half-written latch
    except Exception as e:  # noqa: BLE001
        log(event="latch_persist_error", error=str(e))


def clear_latch():
    try:
        if os.path.exists(LATCH_FILE):
            os.remove(LATCH_FILE)
    except Exception as e:  # noqa: BLE001
        log(event="latch_clear_error", error=str(e))


def restore_latch():
    """On boot: if a latched trip was persisted, refuse ARMED until operator reset."""
    try:
        if not os.path.exists(LATCH_FILE):
            return
        d = json.load(open(LATCH_FILE))
        if d.get("state") == TRIPPED:
            sr.state = TRIPPED
            sr.trip_source = d.get("trip_source") or "restored_latch"
            sr.severity = d.get("severity") or "graded"
            log(event="latch_restored", trip_source=sr.trip_source, severity=sr.severity)
    except Exception as e:  # noqa: BLE001
        log(event="latch_restore_error", error=str(e))


# ── shared latch (HA): the latch lives in the lease store so a relay that was DOWN during
# a trip and later becomes leader still honours the manual-reset-only invariant. A no-op
# without HA (ctrl is None) — the local durable file covers the single-relay case.
async def persist_latch_shared():
    if ctrl is None:
        return
    try:
        await ctrl.store.put(LATCH_KEY, {"state": sr.state, "trip_source": sr.trip_source,
                                         "severity": sr.severity})
    except Exception as e:  # noqa: BLE001
        log(event="shared_latch_persist_error", error=str(e))


async def clear_latch_shared():
    if ctrl is None:
        return
    try:
        await ctrl.store.delete(LATCH_KEY)
    except Exception as e:  # noqa: BLE001
        log(event="shared_latch_clear_error", error=str(e))


async def adopt_shared_latch():
    """Called when this relay becomes leader: inherit a latch raised while it was a standby
    (or down). Closes the 'came up clean after a trip and re-armed without ack' hole."""
    if ctrl is None:
        return
    try:
        r = await ctrl.store.get(LATCH_KEY)
        if r:
            val, _ = r
            if val.get("state") == TRIPPED and sr.state != TRIPPED:
                sr.state = TRIPPED
                sr.trip_source = val.get("trip_source") or "shared_latch"
                sr.severity = val.get("severity") or "graded"
                persist_latch()   # mirror locally
                log(event="shared_latch_adopted", trip_source=sr.trip_source)
    except Exception as e:  # noqa: BLE001
        log(event="shared_latch_adopt_error", error=str(e))


# wall clock — the relay reacts in real time, not sim-warped time
def now() -> float:
    return time.monotonic()


# ── NATS intake (data only; never a command source) ─────────────────────────
async def on_mpc(msg):
    try:
        d = json.loads(msg.data)
        adv = d.get("advisory") or {}
        if "flow" in adv and "temp" in adv:
            sr.update_mpc(adv["flow"], adv["temp"], now())
    except Exception as e:  # noqa: BLE001
        log(event="mpc_parse_error", error=str(e))


async def on_sensor(msg):
    try:
        d = json.loads(msg.data)
        sr.update_sensors(d.get("rack_sensors", []), now())
    except Exception as e:  # noqa: BLE001
        log(event="sensor_parse_error", error=str(e))


async def on_residual(msg):
    try:
        d = json.loads(msg.data)
        sr.update_residual(d.get("residuals", {}), now())
    except Exception as e:  # noqa: BLE001
        log(event="residual_parse_error", error=str(e))


# ── the fast safety loop: evaluate, then publish (sole writer) ──────────────
async def eval_loop():
    log(event="relay_started", eval_s=EVAL_S, tenant=TENANT, ha=HA_ENABLED, id=RELAY_ID)
    global _seq, EPOCH, _is_leader
    last_state = sr.state
    was_leader = _is_leader
    while True:
        t = now()
        # HA: renew/acquire the fencing lease. On store error keep the last role+epoch —
        # the fence at plant-sim makes a stale writer harmless (its epoch gets out-ranked).
        if ctrl is not None:
            try:
                _is_leader, epoch = await ctrl.tick(t)
                EPOCH = float(epoch)
            except Exception as e:  # noqa: BLE001
                log(event="lease_error", error=str(e))
        if _is_leader and not was_leader:        # just promoted -> inherit any shared latch
            await adopt_shared_latch()
        was_leader = _is_leader
        st = sr.evaluate(t)
        # commit the latch (local + shared) BEFORE the fallback command hits the wire
        if st["state"] == TRIPPED and last_state != TRIPPED:
            persist_latch()
            await persist_latch_shared()
        # ONLY the lease holder writes to the actuator subject (single-writer invariant)
        if nc is not None and _is_leader:
            try:
                _seq += 1
                await nc.publish(
                    f"plant.control.{TENANT}.setpoints",
                    json.dumps({"flow": st["command"]["flow"],
                                "temp": st["command"]["temp"],
                                "src": RELAY_ID, "state": st["state"],
                                "epoch": EPOCH, "seq": _seq, "ts": t}).encode())
                await nc.publish(
                    f"safety.relay.{TENANT}.status", json.dumps({**st, "leader": RELAY_ID}).encode())
            except Exception as e:  # noqa: BLE001
                log(event="publish_error", error=str(e))
        if st["state"] != last_state:   # edge-log transitions only
            log(event="state_change", state=st["state"], leader=_is_leader,
                trip_source=st["trip_source"], severity=st["severity"])
            last_state = st["state"]
        await asyncio.sleep(EVAL_S)


# ── operator HTTP: /reset is the only exit from a latched trip ──────────────
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
    return web.json_response(sr.status(now()))


async def on_reset_broadcast(msg):
    # an operator reset on EITHER relay clears the latch on BOTH (HA pair).
    st = sr.reset(now())
    clear_latch()
    await clear_latch_shared()
    log(event="reset_applied", via="broadcast", state=st["state"])


async def h_reset(_):
    st = sr.reset(now())
    clear_latch()   # operator ack -> drop the durable latch
    await clear_latch_shared()
    if nc is not None:   # propagate to the standby (HA)
        try:
            await nc.publish(f"safety.relay.{TENANT}.reset", b"{}")
        except Exception:  # noqa: BLE001
            pass
    log(event="operator_reset", resulting_state=st["state"], trip_source=st["trip_source"])
    return web.json_response({"reset": True, "status": st})


def build_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([
        web.get("/health", h_health),
        web.get("/status", h_status),
        web.post("/reset", h_reset),
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
        raise SystemExit("safety-relay: cannot reach NATS after 15 attempts")

    restore_latch()   # refuse ARMED on boot if a trip was latched before the crash

    # HA: build the fencing-lease controller over JetStream KV. If JS is unavailable,
    # fall back to single-relay (boot-epoch) rather than bricking the safety function.
    global ctrl
    if HA_ENABLED:
        try:
            js = nc.jetstream()
            try:
                kv = await js.key_value(KV_BUCKET)
            except Exception:
                kv = await js.create_key_value(bucket=KV_BUCKET)
            ctrl = LeaderController(NatsKvStore(kv), RELAY_ID, ttl=LEASE_TTL)
            log(event="ha_enabled", id=RELAY_ID, bucket=KV_BUCKET, ttl=LEASE_TTL)
        except Exception as e:  # noqa: BLE001
            log(event="ha_unavailable_fallback_single", error=str(e))
            ctrl = None

    await nc.subscribe(f"safety.relay.{TENANT}.reset", cb=on_reset_broadcast)

    await nc.subscribe(f"rom.mpc.{TENANT}.advisory", cb=on_mpc)
    await nc.subscribe(f"sensor.thermal.{TENANT}", cb=on_sensor)
    await nc.subscribe(f"rom.static.{TENANT}.residual", cb=on_residual)

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log(event="http_started", port=HTTP_PORT)

    await eval_loop()


if __name__ == "__main__":
    asyncio.run(main())
