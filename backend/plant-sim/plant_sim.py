"""
plant_sim.py — Phase 0 GROUND-TRUTH thermal plant simulator.

This stands in for the real datacenter. It is deliberately NOT the model under
test: the rom-shadow service must validate against an *independent* generating
process whose true state we know, otherwise the residual is a leakage artifact
(validating the ROM against data produced by the model it replaces proves
nothing). See FIXES / Phase 0 design notes.

Topology — single-aisle containment pod, 12 well-mixed air zones:
    index  role
    0..7   rack zones (IT load centers; heat injected here, exhaust -> hot aisle)
    8,9    cold aisle plenums  (CA1 feeds racks 0-3, CA2 feeds racks 4-7)
    10     hot aisle return plenum (mixes all 8 rack exhausts)
    11     CRAC supply (the cooling actuator)

Physics: advection-dominated energy balance. Air mass flow carries enthalpy
CRAC -> cold aisles -> racks -> hot aisle -> CRAC. Linear within a fault regime,
so we integrate with backward-Euler (one 12x12 solve), unconditionally stable.

Sensors (what rom-shadow sees, over NATS subject `sensor.thermal.<tenant>`):
    - 8 rack-inlet sensors: TRUE value = the feeding cold-aisle temp (the
      safety-relevant ASHRAE inlet). Each rack has its own front-door sensor, so
      drift/dropout can hit ONE rack while its 3 cold-aisle neighbours stay honest.
    - 12 zone sensors: one per state.
Ground truth (NOT ROM-visible; subject `plant.truth.<tenant>`): noise-free,
sensor-fault-free state + the raw 12-vector + active faults. The ROM is
forbidden by convention from subscribing to this; the dashboard/eval uses it for
the `rom_truth_c` series and the residual decomposition.

Faults split cleanly into two domains — this IS the validation matrix:
    SENSOR-domain (corrupt the reading, not the truth):
      sensor_noise   baseline Gaussian jitter (always on)
      sensor_drift   slow creeping bias on one rack-inlet sensor
      sensor_dropout NaN / flatline on one sensor
        -> should show pred-vs-measured divergence with NO pred-vs-truth divergence
           (ROM is right, the sensor lies)
    PLANT-domain (corrupt the truth, ROM must track):
      recirculation  transient hot-aisle bleed into a cold aisle
      crac_degradation step drop in airflow (partial fan failure)
        -> should show pred-vs-truth divergence (a real thermal event)
"""

import numpy as np

CP = 1005.0      # J/(kg.K) air specific heat
RHO = 1.2        # kg/m^3 air density

N_RACKS = 8
N_ZONES = 12
RACK = list(range(0, 8))
CA = [8, 9]          # cold aisles
HA = 10              # hot aisle
CRAC = 11            # crac supply
CA_OF_RACK = {0: 8, 1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 9}

ZONE_ROLE = (
    [f"rack-{i+1}" for i in range(8)]
    + ["cold-aisle-1", "cold-aisle-2", "hot-aisle", "crac-supply"]
)

# Volumes (m^3) -> lumped air capacitance C = rho*cp*V (J/K). Small zones = fast.
VOLUME = np.array(
    [1.5] * 8          # racks
    + [25.0, 25.0]     # cold aisles
    + [35.0]           # hot aisle
    + [8.0]            # crac
)
CAP = RHO * CP * VOLUME  # J/K

# Cooling
CRAC_EFFECTIVENESS = 0.92   # coil effectiveness eps; supply = setpoint + (1-eps)*(T_HA-setpoint)
M_RACK_BASE = 2.5           # kg/s per rack at full airflow (m_total = 8*2.5 = 20 kg/s)

# IT power model (matches twin-model.js GPU watts)
W_GPU_FULL = 650.0
W_GPU_IDLE = 90.0

AMBIENT_INIT = 22.0
SETPOINT_DEFAULT = 18.0


class Plant:
    """Pure numerical core. No I/O — importable and unit-testable."""

    def __init__(self, setpoint_c: float = SETPOINT_DEFAULT, seed: int = 0):
        self.T = np.full(N_ZONES, AMBIENT_INIT, dtype=float)
        self.setpoint = setpoint_c
        self.rng = np.random.default_rng(seed)

        # rack IT power (W); default to idle until slurm telemetry arrives
        self.rack_power = np.full(N_RACKS, 8 * W_GPU_IDLE * 6, dtype=float)

        # plant-domain fault state
        self.airflow_frac = 1.0          # EFFECTIVE flow = commanded x fault cap
        self.fault_airflow_cap = 1.0     # crac_degradation: <1 caps deliverable flow
        self.cmd_airflow = 1.0           # actuation command (1.0 = baseline/full)
        self.vfd_slew_per_s = 0.10       # VFD accel limit: max Δairflow_frac per second
        self.coil_eff = 1.0              # coil effectiveness (slow fouling drives this DOWN)
        self.foul_rate_per_s = 0.0       # coil_fouling ramp rate; >0 = fouling in progress
        self.coil_eff_floor = 0.40       # fouling asymptote
        self.recirc_kgps = np.zeros(2)   # recirculation leak HA->CA per cold aisle
        self.recirc_until = [None, None] # sim-time auto-clear, or None=persistent/off

        # sensor-domain fault state (keyed by sensor id)
        self.sensor_noise_sigma = 0.15   # baseline Gaussian °C
        self.drift = {}                  # sid -> {"rate": c_per_min, "acc": c}
        self.dropout = {}                # sid -> "nan" | "flatline"
        self.spike = {}                  # (rack_idx, pos) -> bias_c (single-sensor fault)
        self._flatline_cache = {}

        self.sim_time = 0.0

    # ---- inputs -------------------------------------------------------------
    def set_rack_power_from_nodes(self, nodes):
        """Bin an incoming slurm.nodes list into 8 racks (contiguous chunks)."""
        if not nodes:
            return
        nodes = sorted(nodes, key=lambda n: n.get("node_name", ""))
        powers = []
        for n in nodes:
            tot = float(n.get("gpus_total", 0) or 0)
            alloc = float(n.get("gpus_alloc", 0) or 0)
            powers.append(alloc * W_GPU_FULL + max(tot - alloc, 0) * W_GPU_IDLE)
        # split into 8 contiguous racks
        chunks = np.array_split(np.array(powers, dtype=float), N_RACKS)
        for i, c in enumerate(chunks):
            self.rack_power[i] = float(c.sum()) if c.size else 8 * W_GPU_IDLE * 6

    # ---- dynamics -----------------------------------------------------------
    def _build_system(self):
        """Assemble C/dt-independent A (W/K) and forcing f (W) for: C dT/dt = A T + f."""
        m_rack = M_RACK_BASE * self.airflow_frac
        g_rack = m_rack * CP                 # rack <- cold aisle, rack -> hot aisle
        g_ca = 4 * m_rack * CP               # cold aisle <- crac supply (4 racks each)
        g_crac = 8 * m_rack * CP             # crac driven by hot-aisle return (m_total)
        eps = CRAC_EFFECTIVENESS

        A = np.zeros((N_ZONES, N_ZONES))
        f = np.zeros(N_ZONES)

        # racks: g_rack*(T_ca - T_i) + Q_i
        for i in RACK:
            ca = CA_OF_RACK[i]
            A[i, i] -= g_rack
            A[i, ca] += g_rack
            f[i] += self.rack_power[i]
            # exhaust to hot aisle handled on HA row (mass conservation)

        # cold aisles: g_ca*(T_crac - T_ca) + recirc*(T_HA - T_ca)
        for k, ca in enumerate(CA):
            A[ca, ca] -= g_ca
            A[ca, CRAC] += g_ca
            g_re = self.recirc_kgps[k] * CP
            if g_re > 0:
                A[ca, ca] -= g_re
                A[ca, HA] += g_re

        # hot aisle: sum_i g_rack*(T_i - T_HA)
        for i in RACK:
            A[HA, HA] -= g_rack
            A[HA, i] += g_rack

        # crac supply: g_crac*( ((1-eps)T_HA + eps*setpoint) - T_crac )
        A[CRAC, CRAC] -= g_crac
        A[CRAC, HA] += g_crac * (1 - eps)
        f[CRAC] += g_crac * eps * self.setpoint

        return A, f

    def step(self, dt: float):
        # slow coil fouling: effectiveness creeps down toward the floor (acute faults
        # are a STEP on fault_airflow_cap; fouling is a RAMP on coil_eff). The clean-coil
        # reference (rom-shadow) is blind to BOTH, so fouling shows as a slow residual drift.
        if self.foul_rate_per_s > 0.0:
            self.coil_eff = max(self.coil_eff_floor, self.coil_eff - self.foul_rate_per_s * dt)
        # effective deliverable flow = command x fault capacity x coil effectiveness.
        target_airflow = max(0.05, self.cmd_airflow * self.fault_airflow_cap * self.coil_eff)
        # VFD physical slew: the drive ramps at its accel limit — a commanded STEP
        # (incl. dead-man snap to baseline) never produces a mechanical step. This
        # protects the hardware regardless of which upstream service is talking.
        max_step = self.vfd_slew_per_s * dt
        delta = target_airflow - self.airflow_frac
        self.airflow_frac = self.airflow_frac + max(-max_step, min(max_step, delta))
        # auto-clear expired recirculation transients
        for k in range(2):
            if self.recirc_until[k] is not None and self.sim_time >= self.recirc_until[k]:
                self.recirc_kgps[k] = 0.0
                self.recirc_until[k] = None

        A, f = self._build_system()
        # backward Euler: (C/dt - A) T_{k+1} = (C/dt) T_k + f
        M = np.diag(CAP / dt) - A
        rhs = (CAP / dt) * self.T + f
        self.T = np.linalg.solve(M, rhs)
        self.sim_time += dt

        # advance sensor drift accumulators
        for sid, d in self.drift.items():
            d["acc"] += d["rate"] * (dt / 60.0)

    # ---- observation --------------------------------------------------------
    def rack_inlet_true(self):
        """True rack-inlet temp = feeding cold-aisle temp (8-vector)."""
        return np.array([self.T[CA_OF_RACK[i]] for i in RACK])

    def _apply_sensor_faults(self, sid, true_val):
        """Return (value, valid). Sensor-domain faults corrupt this, not self.T."""
        if sid in self.dropout:
            mode = self.dropout[sid]
            if mode == "nan":
                return float("nan"), False
            # flatline: hold the last good reading
            return self._flatline_cache.get(sid, true_val), False
        val = true_val + self.rng.normal(0.0, self.sensor_noise_sigma)
        if sid in self.drift:
            val += self.drift[sid]["acc"]
        self._flatline_cache[sid] = val
        return val, True

    def read_sensors(self):
        """Noisy, fault-injected readings the ROM is allowed to see."""
        inlet_true = self.rack_inlet_true()
        rack_inlet = []
        for i in RACK:
            v, valid = self._apply_sensor_faults(f"rack-{i+1}-inlet", inlet_true[i])
            rack_inlet.append({"sensor_id": f"rack-{i+1}-inlet", "temp_c": v, "valid": valid})
        zones = []
        for z in range(N_ZONES):
            v, valid = self._apply_sensor_faults(f"zone-{ZONE_ROLE[z]}", self.T[z])
            zones.append({"zone_id": ZONE_ROLE[z], "temp_c": v, "valid": valid})
        # 3 redundant inlet sensors per rack (top/mid/bottom) for L1 2-of-3 voting.
        # Buoyancy: top hottest. Independent noise + optional single-sensor spike.
        BUOY = {"top": 1.5, "mid": 0.0, "bottom": -1.0}
        rack_sensors = []
        for i in RACK:
            trio = {}
            for pos, off in BUOY.items():
                v = inlet_true[i] + off + self.rng.normal(0.0, self.sensor_noise_sigma)
                v += self.spike.get((i, pos), 0.0)
                trio[pos] = round(float(v), 3)
            rack_sensors.append({"rack": f"rack-{i+1}", "sensors": trio})
        return {
            "ts": self.sim_time,
            "rack_inlet_c": rack_inlet,
            "zones": zones,
            "rack_sensors": rack_sensors,
            "crac_airflow_pct": round(100.0 * self.airflow_frac, 1),
            "crac_supply_c": zones[CRAC]["temp_c"],
        }

    def read_truth(self):
        """Noise-free, fault-free ground truth. ROM MUST NOT subscribe to this."""
        inlet = self.rack_inlet_true()
        return {
            "ts": self.sim_time,
            "state": [round(float(t), 4) for t in self.T],
            "zone_roles": ZONE_ROLE,
            "rack_inlet_c": [round(float(t), 4) for t in inlet],
            "crac_airflow_pct": round(100.0 * self.airflow_frac, 1),
            "active_faults": self.describe_faults(),
        }

    # ---- fault API ----------------------------------------------------------
    def apply_fault(self, ftype, params):
        p = params or {}
        if ftype == "sensor_noise":
            self.sensor_noise_sigma = float(p.get("sigma_c", 0.15))
        elif ftype == "sensor_drift":
            sid = p["sensor_id"]
            self.drift[sid] = {"rate": float(p.get("rate_c_per_min", 0.5)), "acc": 0.0}
        elif ftype == "sensor_dropout":
            self.dropout[p["sensor_id"]] = p.get("mode", "nan")
        elif ftype == "sensor_spike":
            # spike ONE sensor on ONE rack (rack 1-8, position top/mid/bottom)
            self.spike[(int(p["rack"]) - 1, p.get("position", "top"))] = float(p.get("delta_c", 8.0))
        elif ftype == "recirculation":
            k = int(p.get("cold_aisle", 0))  # 0 or 1
            self.recirc_kgps[k] = float(p.get("intensity_kgps", 1.5))
            dur = p.get("duration_s")
            self.recirc_until[k] = (self.sim_time + float(dur)) if dur else None
        elif ftype == "crac_degradation":
            self.fault_airflow_cap = max(0.05, 1.0 - float(p.get("airflow_drop_pct", 40)) / 100.0)
        elif ftype == "coil_fouling":
            # slow effectiveness ramp. rate in %/hour of sim-time -> per-second decrement.
            self.foul_rate_per_s = float(p.get("rate_pct_per_hour", 5.0)) / 100.0 / 3600.0
            self.coil_eff_floor = max(0.05, 1.0 - float(p.get("max_drop_pct", 60)) / 100.0)
        else:
            raise ValueError(f"unknown fault type: {ftype}")
        return self.describe_faults()

    def clear_faults(self):
        self.fault_airflow_cap = 1.0
        self.foul_rate_per_s = 0.0
        self.coil_eff = 1.0
        self.recirc_kgps = np.zeros(2)
        self.recirc_until = [None, None]

    def apply_actuation(self, flow_cmd: float, setpoint_c: float):
        """Relay command intake (only called when actuation is ACTIVE and fresh).
        flow_cmd in [0.3, 0.85]; effective flow is throttled by fault capacity in step()."""
        self.cmd_airflow = float(flow_cmd)
        self.setpoint = float(setpoint_c)

    def revert_baseline(self, setpoint_c: float):
        """Dead-man / shadow: run the always-on baseline (full flow, default setpoint)."""
        self.cmd_airflow = 1.0
        self.setpoint = float(setpoint_c)
        self.drift.clear()
        self.dropout.clear()
        self.spike.clear()
        self.sensor_noise_sigma = 0.15
        return self.describe_faults()

    def describe_faults(self):
        return {
            "sensor_noise_sigma_c": self.sensor_noise_sigma,
            "drift": {k: round(v["acc"], 3) for k, v in self.drift.items()},
            "dropout": dict(self.dropout),
            "recirculation_kgps": [float(x) for x in self.recirc_kgps],
            "crac_airflow_pct": round(100.0 * self.airflow_frac, 1),
        }
