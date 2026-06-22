"""
fence.py — leader election with a fencing lease (relay HA).

The single-writer invariant must survive failover. Two relays run; both do the full safety
evaluation, but only the LEASE HOLDER publishes to plant.control. Correctness does NOT come
from the two relays agreeing — a partition or GC pause could make both think they lead. It
comes from a monotonic FENCING EPOCH carried on every command and enforced at the actuator
(plant-sim rejects any epoch lower than the highest it has seen). So a zombie ex-leader that
resumes is fenced out by topology, exactly like the dead-man doesn't trust the relay to be
alive. This file owns the epoch; plant-sim owns the enforcement.

Store-agnostic over an optimistic-CAS key/value (matches NATS JetStream KV revision
semantics). InMemoryStore is for tests; NatsKvStore is for production.

LIMITATION: lease expiry uses wall time, so cross-host clock skew must be << the TTL — keep
TTL >> heartbeat (default 3x). NATS KV server-side TTL (single clock) is the more robust prod
backstop and is set on the bucket.
"""
from __future__ import annotations
import time
from typing import Optional, Tuple

LEASE_KEY = "relay.lease"
LATCH_KEY = "relay.latch"


# ── stores ──────────────────────────────────────────────────────────────────
class InMemoryStore:
    """Single-process CAS store with an injectable clock (tests). revision is monotonic."""
    def __init__(self, clock=None):
        self._d = {}          # key -> (value, revision, expires_at|None)
        self._rev = 0
        self._clock = clock or time.time

    def _expired(self, entry):
        return entry[2] is not None and entry[2] <= self._clock()

    async def get(self, key):
        e = self._d.get(key)
        if e is None or self._expired(e):
            return None
        return (e[0], e[1])

    async def create(self, key, value, ttl=None):
        e = self._d.get(key)
        if e is not None and not self._expired(e):
            return None
        self._rev += 1
        self._d[key] = (value, self._rev, (self._clock() + ttl) if ttl else None)
        return self._rev

    async def update(self, key, value, last_rev, ttl=None):
        e = self._d.get(key)
        if e is None or self._expired(e) or e[1] != last_rev:
            return None
        self._rev += 1
        self._d[key] = (value, self._rev, (self._clock() + ttl) if ttl else None)
        return self._rev

    async def put(self, key, value, ttl=None):
        """unconditional write (used for the shared latch)."""
        self._rev += 1
        self._d[key] = (value, self._rev, (self._clock() + ttl) if ttl else None)
        return self._rev

    async def delete(self, key):
        self._d.pop(key, None)
        return True


class NatsKvStore:
    """Production store over NATS JetStream KV. NOT exercised in unit tests (needs live JS).

    Maps: get->kv.get, create/update->kv.create/kv.update(revision) for optimistic CAS,
    put->kv.put, delete->kv.delete. Bucket TTL gives a server-clock lease backstop.
    """
    def __init__(self, kv):
        self._kv = kv

    async def get(self, key):
        try:
            e = await self._kv.get(key)
            import json
            return (json.loads(e.value), e.revision)
        except Exception:
            return None

    async def create(self, key, value, ttl=None):
        import json
        try:
            rev = await self._kv.create(key, json.dumps(value).encode())
            return rev
        except Exception:
            return None

    async def update(self, key, value, last_rev, ttl=None):
        import json
        try:
            return await self._kv.update(key, json.dumps(value).encode(), last=last_rev)
        except Exception:
            return None

    async def put(self, key, value, ttl=None):
        import json
        return await self._kv.put(key, json.dumps(value).encode())

    async def delete(self, key):
        try:
            await self._kv.delete(key)
            return True
        except Exception:
            return False


# ── leader controller (sync; used with InMemoryStore in tests) ──────────────
class LeaderController:
    def __init__(self, store, holder_id: str, ttl: float = 6.0):
        self.store = store
        self.id = holder_id
        self.ttl = ttl
        self.is_leader = False
        self.epoch = -1            # the epoch we currently hold (when leader)
        self._held = False         # has THIS process instance confirmed the hold?

    def _set(self, is_leader, epoch):
        self.is_leader = is_leader
        self._held = is_leader     # a restart starts with _held=False -> forces an epoch bump
        self.epoch = epoch
        return is_leader, epoch

    async def tick(self, now: float) -> Tuple[bool, int]:
        cur = await self.store.get(LEASE_KEY)
        if cur is None:
            rev = await self.store.create(LEASE_KEY,
                                    {"holder": self.id, "epoch": 1, "expires": now + self.ttl})
            return self._set(rev is not None, 1 if rev is not None else self.epoch)

        val, rev = cur
        mine = val["holder"] == self.id

        # genuine ongoing hold by THIS instance -> renew, keep epoch (seq continues in-process)
        if mine and self._held:
            newrev = await self.store.update(LEASE_KEY,
                                       {"holder": self.id, "epoch": val["epoch"], "expires": now + self.ttl},
                                       rev)
            return self._set(newrev is not None, val["epoch"])

        # acquire when the lease is free (expired) OR is our OWN stale lease from a past life
        # (a restart: same id in the store, but _held is False). Either way BUMP the epoch so a
        # fresh in-memory seq counter is accepted by plant-sim and any predecessor is fenced.
        if val["expires"] <= now or mine:
            new_epoch = val["epoch"] + 1
            newrev = await self.store.update(LEASE_KEY,
                                       {"holder": self.id, "epoch": new_epoch, "expires": now + self.ttl},
                                       rev)
            if newrev is not None:
                return self._set(True, new_epoch)
            return self._set(False, val["epoch"])         # lost the race; retry next tick

        return self._set(False, val["epoch"])              # held by a live peer -> stand by

    async def release(self):
        cur = await self.store.get(LEASE_KEY)
        if cur and cur[0].get("holder") == self.id:
            await self.store.delete(LEASE_KEY)
        self.is_leader = False
        self._held = False
