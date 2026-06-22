# compliance-runner

An automated, deterministic conformance gate for the ThermalFlow control/safety logic. It
injects each fault class, measures the fallback response, exercises the HA zombie-fencing
end-to-end, and emits a **JSON ledger** plus an **Annex-IV-structured PDF** as technical-
documentation evidence.

## Read this first (scope)

This runner produces *evidence*. It is **not** a legal determination and **not** a
certification. Specifically it does **not** assert that ThermalFlow is an "AI system" or a
"high-risk AI system" under Regulation (EU) 2024/1689, nor that Annex IV applies. Whether the
Act applies — and under which Annex III category (plausibly only via "safety components in the
management of critical infrastructure", which is deployment-specific) — is a determination for
the deployer's compliance/legal function. The full notice is embedded verbatim in every JSON
ledger (`scope_note`) and on the PDF cover. The authors are not lawyers; this is not legal
advice.

## What it tests (and what it does not)

The checks **import the production safety modules directly** (`relay.py`, `fence.py`,
`plant_sim.py`, `health_monitor.py`, `mpc_core.py`, `rom_model.py`) and drive them through
seeded, in-process scenarios. This covers the control/safety **logic** with full determinism
and reproducibility — the right properties for a compliance gate.

It deliberately does **not** cover the messaging/orchestration layer (NATS, JetStream KV,
containers). Reported latencies are **control-loop-cycle** figures under a modelled plant; they
exclude bus transport, scheduling, and physical-actuator response. Wall-clock latency on real
infrastructure must be measured by a separate **live-stack smoke test** (see *Extending* below)
before any safety claim is made.

## Checks

| ID | Property |
|----|----------|
| CHK-IL-L1-BG | single sensor > 38 C -> break-glass trip |
| CHK-IL-L1-VOTE | 2-of-3 trio > 32 C -> graded trip (noise must not) |
| CHK-IL-STALE-SENSOR | sensor comms loss > 10 s -> trip |
| CHK-IL-STALE-MPC | MPC stall > 90 s -> trip |
| CHK-IL-INVALID-CMD | out-of-bounds / over-slew proposal rejected |
| CHK-LATCH-DURABLE | latched trip survives restart, refuses silent re-arm |
| CHK-IL-L2-RESIDUAL | acute CRAC loss -> model-residual trip + latency |
| CHK-DEADMAN | lost controller -> autonomous revert to baseline |
| CHK-FENCE-ZOMBIE | failover: zombie ex-leader fenced at the actuator, no dual-write |
| CHK-FENCE-RESTART | restart re-acquisition: fresh sequence accepted |
| CHK-HEALTH-LEAD | slow fouling raises a maintenance alarm before any trip |
| CHK-HEALTH-LOADSHIFT | load shift is not mistaken for degradation |

Budgets (latency thresholds, alarm lead) are **safety-case parameters**, configurable in
`checks.py::DEFAULT_CONFIG` — they are not regulatory constants and should be ratified by the
project's safety case.

## Run

```bash
pip install -r compliance/requirements.txt
python compliance/runner.py --repo-root . --out-dir compliance/out
#   --no-pdf   JSON only      --quiet   no console summary
```

Exit code is non-zero if any check fails, so it gates CI directly. Outputs:
`compliance_ledger.json` (machine-readable source of truth) and
`annex_iv_technical_documentation.pdf` (human-readable binder page). Every run records a
SHA-256 of each safety source file, so the evidence is traceable to the exact code version.

CI workflow: `compliance/ci/github-actions.yml` (copy to `.github/workflows/`).

## Extending: the live-stack smoke test (TODO, not included)

The complement to this deterministic gate is a thin integration probe that runs against the
**assembled** stack and measures real wall-clock behaviour:

1. `docker compose up -d`; wait for health.
2. Go `ACTUATION_MODE=ACTIVE`; subscribe `plant.control.demo.setpoints` over NATS.
3. `POST :8090/fault` to inject each fault; timestamp the first TRIPPED command on the wire.
4. For HA, `--profile ha`, kill the leader pod, confirm the standby resumes and the old pod is
   fenced (no dual-write observed on the subject).

That probe is intentionally **not** in this package: it cannot be validated without a live
cluster, and a compliance artifact must not ship unvalidated code that implies coverage it
does not have. Build it against staging, not here.
