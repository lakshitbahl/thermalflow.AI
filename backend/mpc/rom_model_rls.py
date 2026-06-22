"""
rom_model_rls.py — 6-zone ROM with online RLS identification of (eps, U_scale).

Derived from rom-shadow's rom_model.py (same topology — keep them in sync). The
difference: eps and a global conductance scale U_scale are INSTANCE parameters,
tuned online by gated recursive least squares.

Identifies only 2 parameters (strict identifiability):
  eps      CRAC coil effectiveness  (drives the 0.30 C static baseline error)
  U_scale  global multiplier on all conductances (airflow/coupling strength)

Gated RLS — adapt only when BOTH:
  PE gate:   lambda_min(EMA[Phi^T Phi]) > DELTA_PHI   (excitation present -> no windup)
  dead-zone: ||e_k|| > DELTA_NOISE                    (signal above sensor noise)
plus a covariance-trace backstop and hard parameter clamps.
"""

import numpy as np

CP = 1005.0
N = 6
CRAC, CA0, CA1, RM0, RM1, HA = range(6)
ROLE = ["crac-supply", "cold-aisle-0", "cold-aisle-1", "rack-mass-0", "rack-mass-1", "hot-aisle"]

W_GPU_FULL = 650.0
W_GPU_IDLE = 90.0
M_TOTAL = 20.0
AMBIENT_INIT = 22.0
SETPOINT_DEFAULT = 18.0
C_ROM = np.array([9000.0, 28000.0, 28000.0, 7000.0, 7000.0, 40000.0])

# ── RLS / dead-zone configuration ───────────────────────────────────────────
EPS0, USCALE0 = 0.90, 1.0
EPS_BOUNDS = (0.85, 0.97)
USCALE_BOUNDS = (0.70, 1.30)
LAMBDA = 0.995                 # forgetting factor
P0 = np.diag([1e-2, 1e-2])     # initial covariance
P_TRACE_MAX = 10.0             # backstop: rescale if trace(P) exceeds this
DELTA_NOISE = 0.40             # ~2.7 * sensor sigma (0.15 C); below this = noise
DELTA_PHI = 1e-4               # min eigenvalue of EMA[Phi^T Phi] to allow adaptation
PHI_EMA_ALPHA = 0.1            # EMA smoothing for the excitation measure
H_FD = 1e-3                    # finite-difference step for the Jacobian


def power_from_nodes(nodes):
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


def sensor_to_state(sensor_id):
    if sensor_id.endswith("-inlet"):
        k = int(sensor_id.split("-")[1]); return CA0 if k <= 4 else CA1
    if sensor_id.startswith("rack-"):
        k = int(sensor_id.split("-")[1]); return RM0 if k <= 4 else RM1
    return {"cold-aisle-1": CA0, "cold-aisle-2": CA1, "hot-aisle": HA, "crac-supply": CRAC}.get(sensor_id)


def _system(eps, u_scale, Q, setpoint):
    """Assemble (A, f) for C dx/dt = A x + f, given parameters."""
    g_rm = (M_TOTAL / 2) * CP * u_scale
    g_ca = (M_TOTAL / 2) * CP * u_scale
    g_crac = M_TOTAL * CP * u_scale
    A = np.zeros((N, N)); f = np.zeros(N)
    A[RM0, RM0] -= g_rm; A[RM0, CA0] += g_rm; f[RM0] += Q[0]
    A[RM1, RM1] -= g_rm; A[RM1, CA1] += g_rm; f[RM1] += Q[1]
    A[CA0, CA0] -= g_ca; A[CA0, CRAC] += g_ca
    A[CA1, CA1] -= g_ca; A[CA1, CRAC] += g_ca
    A[HA, HA] -= 2 * g_rm; A[HA, RM0] += g_rm; A[HA, RM1] += g_rm
    A[CRAC, CRAC] -= g_crac; A[CRAC, HA] += g_crac * (1 - eps)
    f[CRAC] += g_crac * eps * setpoint
    return A, f


def _step_from(x_prev, dt, Q, setpoint, eps, u_scale):
    """Pure backward-Euler one-step prediction (used for the real step + Jacobian)."""
    A, f = _system(eps, u_scale, Q, setpoint)
    M = np.diag(C_ROM / dt) - A
    return np.linalg.solve(M, (C_ROM / dt) * x_prev + f)


class RlsRom:
    def __init__(self, setpoint_c=SETPOINT_DEFAULT):
        self.x = np.full(N, AMBIENT_INIT, dtype=float)
        self.setpoint = setpoint_c
        self.Q = np.zeros(2)
        self.theta = np.array([EPS0, USCALE0])     # [eps, u_scale]
        self.P = P0.copy()
        self.phi_ema = np.zeros((2, 2))
        self.frozen = True
        self._prev_x = self.x.copy()
        self._last_dt = 1.0

    def set_power(self, q):
        if q is not None:
            self.Q = np.array(q, dtype=float)

    def step(self, dt):
        self._prev_x = self.x.copy()
        self._last_dt = dt
        self.x = _step_from(self.x, dt, self.Q, self.setpoint, self.theta[0], self.theta[1])

    def _jacobian(self):
        """Phi = d(x_pred)/d(theta), finite-differenced through the last step (N x 2)."""
        base = self.x
        cols = []
        for j in range(2):
            tp = self.theta.copy(); tp[j] += H_FD
            xp = _step_from(self._prev_x, self._last_dt, self.Q, self.setpoint, tp[0], tp[1])
            cols.append((xp - base) / H_FD)
        return np.column_stack(cols)  # N x 2

    def update(self, y, mask):
        """Gated RLS update. y: N-vector measurements; mask: N bools (valid).

        Returns dict with adaptation diagnostics.
        """
        Phi_full = self._jacobian()                 # N x 2
        idx = np.where(mask)[0]
        if idx.size == 0:
            self.frozen = True
            return self._diag(0.0, 0.0, gated=True)

        Phi = Phi_full[idx]                          # m x 2
        e = (y[idx] - self.x[idx])                   # m  (innovation)

        # excitation measure: smallest eigenvalue of EMA[Phi^T Phi]
        self.phi_ema = (1 - PHI_EMA_ALPHA) * self.phi_ema + PHI_EMA_ALPHA * (Phi.T @ Phi)
        eig_min = float(np.linalg.eigvalsh(self.phi_ema)[0])
        e_norm = float(np.linalg.norm(e))

        adapt = (eig_min > DELTA_PHI) and (e_norm > DELTA_NOISE)
        self.frozen = not adapt
        if adapt:
            # RLS (vector measurement): K = P Phi^T (lambda I + Phi P Phi^T)^-1
            S = LAMBDA * np.eye(idx.size) + Phi @ self.P @ Phi.T
            K = self.P @ Phi.T @ np.linalg.inv(S)        # 2 x m
            self.theta = self.theta + K @ e
            self.P = (self.P - K @ Phi @ self.P) / LAMBDA
            # symmetrize + trace backstop
            self.P = 0.5 * (self.P + self.P.T)
            tr = np.trace(self.P)
            if tr > P_TRACE_MAX:
                self.P *= (P_TRACE_MAX / tr)
            # hard parameter projection (physical bounds)
            self.theta[0] = float(np.clip(self.theta[0], *EPS_BOUNDS))
            self.theta[1] = float(np.clip(self.theta[1], *USCALE_BOUNDS))

        return self._diag(eig_min, e_norm, gated=not adapt)

    def _diag(self, eig_min, e_norm, gated):
        return {"eps": float(self.theta[0]), "u_scale": float(self.theta[1]),
                "frozen": bool(gated), "eig_min": eig_min, "e_norm": e_norm,
                "trace_P": float(np.trace(self.P))}

    # observation helpers (mirror rom-shadow)
    def predict_state(self):
        return {ROLE[i]: round(float(self.x[i]), 4) for i in range(N)}

    def predict_rack_inlets(self):
        return [float(self.x[CA0])] * 4 + [float(self.x[CA1])] * 4

    def residual_for(self, sensor_id, measured_value):
        s = sensor_to_state(sensor_id)
        return None if s is None else float(measured_value) - float(self.x[s])
