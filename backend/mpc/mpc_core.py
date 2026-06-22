"""
mpc_core.py — Gate A v2 advisory MPC (pure compute, no I/O; unit-testable).

v2 corrections (from the facility red-team):
  + Waterside state: T_supply_actual lags the command (tau_water=150s), slew-capped
    (chiller can't drop the loop faster than ~0.5 C/min). The air sees the DELIVERED
    supply, not the command. This makes the dominant time constant minutes, not seconds.
  + Total-plant objective: fan(u_flow) + chiller(warm supply cheaper) + low-DeltaT
    penalty  w_dT * pos(DT_target - (T_HA - T_supply))^2  (anti low-DeltaT-syndrome).
  + VFD ramp limit on airflow + 60s move-block (2 ticks) — protects bearings/plenum.
  + dt=30s: the air modes (tau<=3.7s) are quasi-static each tick; the real dynamics
    is the waterside. N_p=15 ticks = 7.5 min ~= 3*tau_water (same eigenvalue logic,
    now applied to the correct — waterside — time constant).

Soft state constraints only (hard limits live in the L1/L2 interlocks).
"""

import time
import numpy as np
import cvxpy as cp

from rom_model_rls import _step_from, HA  # HA = hot-aisle air-state index (5)

N_AIR = 6
SUP = 6          # waterside supply-temp state index in the augmented state
N = 7


class AdvisoryMPC:
    def __init__(self, eps, u_scale, dt=60.0, Np=8, Ncb=4, blk=1,
                 tau_water=150.0, slew_c_per_min=0.5,
                 T_limit=30.0, dT_target=12.0,
                 flow_bounds=(0.3, 0.85), temp_bounds=(18.0, 27.0),
                 du_flow_max=0.10, p_fan_max=7.5, cop0=6.0, beta_cop=0.03,
                 w_dt=1.0, rho=2000.0, lam=0.5):   # w_dt kW-scaled (real-kW objective)
        self.eps, self.u_scale, self.dt, self.tau = eps, u_scale, dt, tau_water
        self.Np, self.Ncb, self.blk = Np, Ncb, blk
        self.T_limit, self.dT_target = T_limit, dT_target
        self.flow_min, self.flow_max = flow_bounds
        self.temp_min, self.temp_max = temp_bounds
        self.du_flow_max = du_flow_max
        self.p_fan_max, self.cop0, self.beta = p_fan_max, cop0, beta_cop
        # chiller slew -> max command excursion s.t. the lag step stays within slew:
        #   d_supply = (dt/tau)*(u_cmd - supply); |d_supply| <= slew_per_tick
        self.slew_per_tick = slew_c_per_min * (dt / 60.0)
        self.cmd_excursion_max = self.slew_per_tick / (dt / tau_water)
        self._build(w_dt, rho, lam)

    def _step(self, x, u_flow, u_temp_cmd, Q):
        x_air = _step_from(x[:N_AIR], self.dt, Q, x[SUP], self.eps, self.u_scale * u_flow)
        a = self.dt / self.tau
        sup = (1 - a) * x[SUP] + a * u_temp_cmd
        return np.concatenate([x_air, [sup]])

    def _linearize(self, x0, u_op, Q):
        g0 = self._step(x0, u_op[0], u_op[1], Q)
        Ad = np.zeros((N, N)); hx = 1e-3
        for i in range(N):
            xp = x0.copy(); xp[i] += hx
            Ad[:, i] = (self._step(xp, u_op[0], u_op[1], Q) - g0) / hx
        Bd = np.zeros((N, 2)); hf, ht = 1e-3, 1e-2
        Bd[:, 0] = (self._step(x0, u_op[0] + hf, u_op[1], Q) - g0) / hf
        Bd[:, 1] = (self._step(x0, u_op[0], u_op[1] + ht, Q) - g0) / ht
        dd = g0 - Ad @ x0 - Bd @ np.asarray(u_op)
        return Ad, Bd, dd

    def _bidx(self, k):
        return min(k // self.blk, self.Ncb - 1)

    def _build(self, w_dt, rho, lam):
        Np = self.Np
        self.x = cp.Variable((Np + 1, N))
        self.ub = cp.Variable((self.Ncb, 2))     # per-block [flow, temp_cmd]
        self.eta = cp.Variable(Np, nonneg=True)
        self.pAd = cp.Parameter((N, N)); self.pBd = cp.Parameter((N, 2)); self.pdd = cp.Parameter(N)
        self.px0 = cp.Parameter(N); self.puprev = cp.Parameter(2)
        self.pQ = cp.Parameter(nonneg=True)       # total IT load (kW) — weights chiller lift

        cons = [self.x[0] == self.px0]
        obj = 0
        for k in range(Np):
            ub = self.ub[self._bidx(k)]
            uf, ut = ub[0], ub[1]
            cons += [self.x[k + 1] == self.pAd @ self.x[k] + self.pBd @ ub + self.pdd]
            cons += [self.x[k + 1][HA] <= self.T_limit + self.eta[k]]
            cons += [cp.abs(ut - self.x[k][SUP]) <= self.cmd_excursion_max]
            cons += [uf >= self.flow_min, uf <= self.flow_max, ut >= self.temp_min, ut <= self.temp_max]
            dT = self.x[k + 1][HA] - self.x[k + 1][SUP]
            # ── real-kW total-plant power ────────────────────────────
            # fan: affinity law P = P_fan_max * u_flow^3 (Clarabel power cone)
            fan_kw = self.p_fan_max * cp.power(uf, 3)
            # chiller: P = Q/COP, COP = cop0*(1 + beta*(ut-18)); linearized about 18C:
            #   P ~ (Q/cop0)*(1 - beta*(ut-18))  -> reward warm supply, load-weighted.
            # constant (Q/cop0) dropped (no effect on argmin); keep the ut-dependent part.
            chiller_kw = -(self.pQ / self.cop0) * self.beta * (ut - 18.0)
            obj += (fan_kw + chiller_kw
                    + w_dt * cp.square(cp.pos(self.dT_target - dT))
                    + rho * cp.square(self.eta[k]))
        for b in range(self.Ncb):
            prev = self.puprev if b == 0 else self.ub[b - 1]
            cons += [cp.abs(self.ub[b][0] - prev[0]) <= self.du_flow_max]  # VFD ramp
            obj += lam * cp.sum_squares(self.ub[b] - prev)
        self.prob = cp.Problem(cp.Minimize(obj), cons)

    def solve(self, x0, Q, u_prev):
        x0 = np.asarray(x0, float); u_prev = np.asarray(u_prev, float)
        Ad, Bd, dd = self._linearize(x0, u_prev, Q)
        self.pAd.value, self.pBd.value, self.pdd.value = Ad, Bd, dd
        self.px0.value, self.puprev.value = x0, u_prev
        self.pQ.value = max(float(np.sum(Q)) / 1000.0, 0.0)   # total IT load in kW
        t0 = time.perf_counter()
        # Clarabel (interior-point) over OSQP (ADMM): the low-DeltaT penalty is
        # piecewise-quadratic, and ADMM thrashes near its kink under low load
        # (-> user_limit). Interior-point is robust there. Small QP, still ms-scale.
        try:
            self.prob.solve(solver=cp.CLARABEL)
        except cp.error.SolverError:
            return {"feasible": False, "status": "solver_error"}
        ms = (time.perf_counter() - t0) * 1e3
        if self.ub.value is None or self.prob.status not in ("optimal", "optimal_inaccurate"):
            return {"feasible": False, "status": self.prob.status, "solve_ms": round(ms, 2)}
        u0 = self.ub.value[0]
        ha = self.x.value[1:, HA]; sup = self.x.value[1:, SUP]
        flow_plan = [round(float(self.ub.value[self._bidx(k)][0]), 3) for k in range(self.Np)]
        temp_plan = [round(float(self.ub.value[self._bidx(k)][1]), 2) for k in range(self.Np)]
        return {
            "feasible": True, "status": self.prob.status, "solve_ms": round(ms, 2),
            "u_flow": float(np.clip(u0[0], self.flow_min, self.flow_max)),
            "u_temp": float(np.clip(u0[1], self.temp_min, self.temp_max)),
            "ha_pred": [round(float(v), 2) for v in ha],
            "sup_pred": [round(float(v), 2) for v in sup],
            "flow_plan": flow_plan, "temp_plan": temp_plan,
            "dT_end": float(ha[-1] - sup[-1]),
            "slack_max": float(np.max(self.eta.value)),
        }


def tau_for_stage(stage: int) -> float:
    """BMS pump-stage -> water-side time constant (gain schedule)."""
    return {1: 240.0, 2: 150.0, 3: 90.0}.get(int(stage), 150.0)


def stage_for_load(load_frac: float) -> int:
    """Sim stand-in for the BMS pump-stage integer (pumps stage up with load)."""
    return 1 if load_frac < 0.35 else (3 if load_frac > 0.75 else 2)
