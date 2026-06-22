"""
health_monitor.py — slow coil-degradation detector (pure logic; unit-testable).

Two-timescale safety: the relay's L2 trip is the fast layer (acute faults, seconds); this is
the slow layer — it watches the SAME frozen clean-coil residual (rom-shadow, never adapts) for
a small, persistent, upward DRIFT (fouling, weeks) and raises a maintenance ALARM. It never
trips the relay or touches the actuator.

PER-LOAD-BIN BASELINING: the structural baseline residual depends on operating point, so a
single zeroed detector false-alarms on a load shift. We therefore bin the reference load band
and keep an INDEPENDENT baseline + CUSUM per bin. A sample only updates its own bin, so moving
to a different load point can't masquerade as drift. Fouling is broadband (degrades every load
point), so it lights up bins as they're visited; we alarm once any committed bin confirms a
sustained drift. A bin that's never visited simply stays uncommissioned and silent.

Still gated on the reference regime (relay ARMED, quasi-steady). A SHADOW<->ACTIVE switch
changes the operating point for ALL bins -> recommission.
"""
from __future__ import annotations
from dataclasses import dataclass, field

OK, COMMISSIONING, ALARM = "OK", "COMMISSIONING", "ALARM"


def _new_bin():
    return {"n": 0, "sum": 0.0, "r0": 0.0, "commissioned": False,
            "cusum": 0.0, "ewma": 0.0, "alarm": False}


@dataclass
class CoilHealthMonitor:
    band_lo: float = 0.40
    band_hi: float = 0.80
    n_bins: int = 4                # independent baselines across the band
    steady_eps: float = 0.5        # max |ΔHA| between samples to count as steady (°C)
    commission_n: int = 120        # qualifying samples per BIN to fix that bin's baseline
    cusum_k: float = 0.25          # CUSUM slack (°C): ignore drift below this (noise)
    cusum_h: float = 8.0           # CUSUM decision threshold (°C·samples) -> bin alarm
    ewma_alpha: float = 0.02
    urgent_ewma: float = 1.5
    headroom_airflow: float = 0.82

    bins: list = field(default_factory=list)
    last_ha: float = field(default=None)  # type: ignore

    def __post_init__(self):
        if not self.bins:
            self.bins = [_new_bin() for _ in range(self.n_bins)]

    def _bin_index(self, load_frac):
        if not (self.band_lo <= load_frac <= self.band_hi):
            return None
        frac = (load_frac - self.band_lo) / max(1e-9, self.band_hi - self.band_lo)
        return min(self.n_bins - 1, int(frac * self.n_bins))

    def sample(self, residual_ha, load_frac, ha, airflow_frac, armed) -> dict:
        steady = self.last_ha is not None and abs(ha - self.last_ha) <= self.steady_eps
        self.last_ha = ha
        bi = self._bin_index(load_frac)
        qualify = bool(armed) and steady and bi is not None

        if qualify:
            b = self.bins[bi]
            if not b["commissioned"]:
                b["sum"] += residual_ha
                b["n"] += 1
                if b["n"] >= self.commission_n:
                    b["r0"] = b["sum"] / b["n"]
                    b["commissioned"] = True
            else:
                dev = residual_ha - b["r0"]
                b["ewma"] = (1 - self.ewma_alpha) * b["ewma"] + self.ewma_alpha * dev
                b["cusum"] = max(0.0, b["cusum"] + dev - self.cusum_k)
                if b["cusum"] > self.cusum_h:
                    b["alarm"] = True

        return self.status(qualify, airflow_frac, bi)

    # ── aggregate views over bins ───────────────────────────────────────────
    @property
    def alarm(self):
        return any(b["alarm"] for b in self.bins)

    @property
    def commissioned(self):
        return any(b["commissioned"] for b in self.bins)

    def _worst_drift(self):
        comm = [b for b in self.bins if b["commissioned"]]
        return max((b["ewma"] for b in comm), default=0.0)

    def _worst_cusum(self):
        comm = [b for b in self.bins if b["commissioned"]]
        return max((b["cusum"] for b in comm), default=0.0)

    def severity(self, airflow_frac):
        if not self.alarm:
            return "none"
        if self._worst_drift() >= self.urgent_ewma or airflow_frac >= self.headroom_airflow:
            return "urgent"
        return "advisory"

    def state(self):
        if self.alarm:
            return ALARM
        return OK if self.commissioned else COMMISSIONING

    def recommission(self):
        self.bins = [_new_bin() for _ in range(self.n_bins)]

    def status(self, qualify=False, airflow_frac=0.0, active_bin=None):
        n_comm = sum(1 for b in self.bins if b["commissioned"])
        progress = sum(min(1.0, b["n"] / self.commission_n) for b in self.bins) / self.n_bins
        return {
            "state": self.state(),
            "alarm": self.alarm,
            "severity": self.severity(airflow_frac),
            "drift_c": round(self._worst_drift(), 3),
            "cusum": round(self._worst_cusum(), 3),
            "cusum_h": self.cusum_h,
            "commissioned": self.commissioned,
            "commission_progress": round(progress, 3) if not self.commissioned else 1.0,
            "bins_committed": n_comm,
            "n_bins": self.n_bins,
            "baseline_c": round(next((b["r0"] for b in self.bins if b["commissioned"]), 0.0), 3) if self.commissioned else None,
            "sampling": qualify,
            "active_bin": active_bin,
        }

    def to_dict(self):
        return {"bins": self.bins}

    def load_dict(self, d):
        if "bins" in d and isinstance(d["bins"], list) and len(d["bins"]) == self.n_bins:
            self.bins = d["bins"]
