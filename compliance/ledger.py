"""
ledger.py — data model + provenance for the conformance ledger.

The ledger is automatically generated EVIDENCE. Its value depends on being traceable to the
exact code under test, so every run records a SHA-256 of each safety-critical source file plus
a combined digest. Re-running on changed safety code yields a different digest — the evidence
cannot be silently detached from the artifact it certifies.
"""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

RUNNER_VERSION = "1.0.0"
LEDGER_SCHEMA = "thermalflow.compliance.v1"

# Safety-critical sources whose behaviour the checks exercise. The digest binds the ledger
# to these exact files.
SAFETY_SOURCES = [
    "backend/safety-relay/relay.py",
    "backend/safety-relay/fence.py",
    "backend/safety-relay/service.py",
    "backend/plant-sim/plant_sim.py",
    "backend/plant-sim/service.py",
    "backend/health-monitor/health_monitor.py",
    "backend/mpc/mpc_core.py",
    "backend/rom-shadow/rom_model.py",
]

# Read carefully. This text is reproduced verbatim in the JSON and the PDF.
SCOPE_NOTE = (
    "This ledger is automatically generated conformance EVIDENCE for the ThermalFlow "
    "control and safety logic. It is produced by deterministic, seeded, in-process "
    "simulation that imports and exercises the production safety modules directly (the "
    "files hashed under provenance.code_digest). It is offered as a CONTRIBUTION to a "
    "deployer's technical documentation and is structured to the headings of EU AI Act "
    "Annex IV (Regulation (EU) 2024/1689) for convenience.\n\n"
    "It is NOT, and must not be represented as: (a) a legal determination that ThermalFlow "
    "is an 'AI system' or a 'high-risk AI system' under the Act; (b) a statement that Annex "
    "IV or any other obligation applies; (c) a conformity assessment, audit, or "
    "certification. Whether the Act applies, and under which Annex III category (e.g. safety "
    "components in the management of critical infrastructure), is a determination for the "
    "deployer's compliance/legal function. The authors are not lawyers and this is not legal "
    "advice.\n\n"
    "Latencies reported here are CONTROL-LOOP-CYCLE figures under a modelled plant. They do "
    "NOT include message-bus transport, container scheduling, or physical-actuator response. "
    "Wall-clock latency on real infrastructure MUST be measured separately via a live-stack "
    "smoke test before any safety claim is made. The simulation covers control/safety LOGIC; "
    "it does not cover the messaging or orchestration layer. No warranty is given."
)


@dataclass
class CheckResult:
    id: str
    title: str
    requirement: str          # the safety property under test
    method: str               # how it is tested
    metrics: dict             # measured values
    budgets: dict             # thresholds applied (safety-case parameters, not regulatory)
    verdict: str              # "PASS" | "FAIL" | "ERROR"
    annex_iv: str             # which Annex IV heading this evidence maps to
    detail: str = ""
    error: str = ""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def code_digest(repo_root):
    files, combined = {}, hashlib.sha256()
    for rel in SAFETY_SOURCES:
        p = os.path.join(repo_root, rel)
        if os.path.exists(p):
            d = _sha256_file(p)
            files[rel] = d
            combined.update(rel.encode())
            combined.update(d.encode())
        else:
            files[rel] = None
    return {"combined_sha256": combined.hexdigest(), "files": files}


def git_commit(repo_root):
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None


def build_ledger(results, repo_root, config):
    passed = sum(1 for r in results if r.verdict == "PASS")
    errored = sum(1 for r in results if r.verdict == "ERROR")
    total = len(results)
    verdict = "PASS" if (passed == total and total > 0) else "FAIL"
    return {
        "artifact": "ThermalFlow Safety Conformance Ledger",
        "schema": LEDGER_SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total, "passed": passed,
            "failed": total - passed - errored, "errored": errored,
            "verdict": verdict,
        },
        "provenance": {
            "git_commit": git_commit(repo_root),
            "code_digest": code_digest(repo_root),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "runner_version": RUNNER_VERSION,
        },
        "config": config,
        "checks": [asdict(r) for r in results],
        "scope_note": SCOPE_NOTE,
    }
