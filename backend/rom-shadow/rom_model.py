"""
rom_model.py — 6-zone reduced-order thermal model (Phase 0, OPEN-LOOP).

The model under test. It is intentionally a COARSE, INDEPENDENTLY-PARAMETERIZED
view of the 12-zone plant — not the plant's own constants re-aggregated (that
would make the structural error artificially ~0, a leakage trap). The parameters
below are engineering estimates an operator would pick from nameplate data; the
residual against the plant measures what that coarseness actually costs.

Open-loop by design: state is driven ONLY by known inputs (rack power + CRAC
setpoint). Measurements are used downstream to compute the residual, never to
correct the state. No output-injection gain L — that is a later decision.

Reduced zones:
    0 CRAC supply
    1 Cold Aisle 0   (plant CA1)
    2 Cold Aisle 1   (plant CA2)
    3 Rack Mass 0    (plant racks 1-4, aggregated)
    4 Rack Mass 1    (plant racks 5-8, aggregated)
    5 Hot Aisle      (plant hot aisle)

Discretized with backward-Euler at dt (default 1 s): the model is a stable LTI
system, so the implicit step is unconditionally stable and exact-linear.
"""

import numpy as np

CP = 1005.0
N = 6
CRAC, CA0, CA1, RM0, RM1, HA = range(6)
ROLE = ["crac-supply", "cold-aisle-0", "cold-aisle-1", "rack-mass-0", "rack-mass-1", "hot-aisle"]

# IT power model (same W/GPU as plant + twin — the disturbance is a known input).
W_GPU_FULL = 650.0
W_GPU_IDLE = 90.0

# ── Independent engineering estimates (deliberately NOT the plant's exact values)
EPS_ROM = 0.90          # plant uses 0.92 -> small steady-state offset
M_TOTAL = 20.0          # kg/s total airflow estimate
SETPOINT_DEFAULT = 18.0
AMBIENT_INIT = 22.0
# Lumped capacitances (J/K), rounded estimates:
C_ROM = np.array([9000.0, 28000.0, 28000.0, 7000.0, 7000.0, 40000.0])


def power_from_nodes(nodes):
    """Aggregate a slurm.nodes list into [Q_rackmass0, Q_rackmass1] watts.

    Mirrors plant-sim binning: split sorted nodes into 8 racks, then group
    racks 0-3 -> mass0, 4-7 -> mass1, so both consume the identical disturbance.
    """
    if not nodes:
        return None
    nodes = sorted(nodes, key=lambda n: n.get("node_name", ""))
    powers = []
    for n in nodes:
        tot = float(n.get("gpus_total", 0) or 0)
        alloc = float(n.get("gpus_alloc", 0) or 0)
        powers.append(alloc * W_GPU_FULL + max(tot - alloc, 0) * W_GPU_IDLE)
    racks = [float(c.sum()) for c in np.array_split(np.array(powers, dtype=float), 8)]
    return [sum(racks[0:4]), sum(racks[4:8])]


# Sensor-id -> reduced state index (the C matrix, as a lookup).
# Must match the IDs plant-sim actually publishes: rack inlets are
# "rack-{k}-inlet"; zone sensors are the bare role ("rack-{k}", "cold-aisle-1",
# "cold-aisle-2", "hot-aisle", "crac-supply").
def sensor_to_state(sensor_id):
    if sensor_id.endswith("-inlet"):                       # rack-k-inlet
        k = int(sensor_id.split("-")[1])
        return CA0 if k <= 4 else CA1
    if sensor_id.startswith("rack-"):                      # zone sensor rack-k
        k = int(sensor_id.split("-")[1])
        return RM0 if k <= 4 else RM1
    return {
        "cold-aisle-1": CA0, "cold-aisle-2": CA1,
        "hot-aisle": HA, "crac-supply": CRAC,
    }.get(sensor_id)


class ReducedOrderModel:
    def __init__(self, setpoint_c=SETPOINT_DEFAULT):
        self.x = np.full(N, AMBIENT_INIT, dtype=float)
        self.setpoint = setpoint_c
        self.Q = np.zeros(2)  # [rack mass 0, rack mass 1] power (W)
        # ── actuation awareness (L2 must track the ACTUATED plant, not a baseline) ──
        # u_flow scales every conductance exactly as plant-sim scales airflow_frac;
        # u_temp is the CRAC supply setpoint forcing. Defaults = always-on baseline.
        # NOTE: no separate tau_water lag here ON PURPOSE — the plant has no waterside
        # lag STATE; its supply lags the setpoint only through the CRAC-zone thermal
        # dynamics, which this ROM reproduces identically. Adding a tau_water lag to the
        # monitor alone would desync it from the plant and reintroduce a (smaller) version
        # of the actuation-blind false residual we are fixing.
        self.u_flow = 1.0
        self.u_temp = setpoint_c

    def set_power(self, q_masses):
        if q_masses is not None:
            self.Q = np.array(q_masses, dtype=float)

    def set_actuation(self, u_flow, u_temp):
        """Live relay command (delivered setpoints). Frozen-parameter, airflow-aware."""
        if u_flow is not None:
            self.u_flow = max(0.05, float(u_flow))
        if u_temp is not None:
            self.u_temp = float(u_temp)

    def _system(self):
        uf = self.u_flow
        g_rm = (M_TOTAL * uf / 2) * CP   # each rack mass carries half the (scaled) airflow
        g_ca = (M_TOTAL * uf / 2) * CP   # each cold aisle fed by half the (scaled) airflow
        g_crac = M_TOTAL * uf * CP
        A = np.zeros((N, N))
        f = np.zeros(N)
        # rack masses heated by IT, cooled by their cold aisle
        A[RM0, RM0] -= g_rm; A[RM0, CA0] += g_rm; f[RM0] += self.Q[0]
        A[RM1, RM1] -= g_rm; A[RM1, CA1] += g_rm; f[RM1] += self.Q[1]
        # cold aisles fed by crac supply
        A[CA0, CA0] -= g_ca; A[CA0, CRAC] += g_ca
        A[CA1, CA1] -= g_ca; A[CA1, CRAC] += g_ca
        # hot aisle mixes both rack-mass exhausts
        A[HA, HA] -= 2 * g_rm; A[HA, RM0] += g_rm; A[HA, RM1] += g_rm
        # crac: supply -> (1-eps)*T_HA + eps*u_temp  (commanded supply, not fixed baseline)
        A[CRAC, CRAC] -= g_crac
        A[CRAC, HA] += g_crac * (1 - EPS_ROM)
        f[CRAC] += g_crac * EPS_ROM * self.u_temp
        return A, f

    def step(self, dt):
        A, f = self._system()
        M = np.diag(C_ROM / dt) - A
        rhs = (C_ROM / dt) * self.x + f
        self.x = np.linalg.solve(M, rhs)

    def predict_state(self):
        return {ROLE[i]: round(float(self.x[i]), 4) for i in range(N)}

    def predict_rack_inlets(self):
        """Predicted per-rack inlet = feeding cold-aisle state (8-vector)."""
        return [float(self.x[CA0])] * 4 + [float(self.x[CA1])] * 4

    def residual_for(self, sensor_id, measured_value):
        """measured - predicted(mapped). None if unmappable."""
        s = sensor_to_state(sensor_id)
        if s is None:
            return None
        return float(measured_value) - float(self.x[s])
