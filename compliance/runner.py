#!/usr/bin/env python3
"""
runner.py — ThermalFlow automated compliance runner (CI/CD entrypoint).

Runs the deterministic conformance checks, writes a JSON ledger + a PDF technical-
documentation rendering, prints a summary, and exits non-zero if any check fails — so it gates
a pipeline. Read compliance/README.md for scope and the (important) legal disclaimer.

  python runner.py [--repo-root DIR] [--out-dir DIR] [--no-pdf] [--quiet]
"""
import argparse
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import checks as checks_mod          # noqa: E402
from ledger import build_ledger      # noqa: E402


def run(repo_root, out_dir, make_pdf=True, quiet=False):
    checks_mod.setup_paths(repo_root)
    cfg = checks_mod.DEFAULT_CONFIG
    results = []
    for fn in checks_mod.ALL_CHECKS:
        try:
            results.append(fn(cfg))
        except Exception as e:  # a check that crashes is an ERROR (not a silent pass)
            from ledger import CheckResult
            results.append(CheckResult(
                id=fn.__name__, title=fn.__name__, requirement="", method="",
                metrics={}, budgets={}, verdict="ERROR", annex_iv="",
                error=f"{e}\n{traceback.format_exc()}"))

    ledger = build_ledger(results, repo_root, cfg)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "compliance_ledger.json")
    with open(json_path, "w") as fh:
        json.dump(ledger, fh, indent=2, default=str)

    pdf_path = None
    if make_pdf:
        try:
            from report import render_pdf
            pdf_path = os.path.join(out_dir, "annex_iv_technical_documentation.pdf")
            render_pdf(ledger, pdf_path)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] PDF rendering failed ({e}); JSON ledger still written", file=sys.stderr)
            pdf_path = None

    if not quiet:
        _print_summary(ledger, json_path, pdf_path)
    return ledger


def _print_summary(ledger, json_path, pdf_path):
    s = ledger["summary"]
    print("\nThermalFlow Conformance Ledger")
    print("=" * 72)
    for c in ledger["checks"]:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR "}[c["verdict"]]
        print(f"  [{mark}] {c['id']:<22} {c['title']}")
        if c["verdict"] != "PASS" and c.get("error"):
            print(f"         {c['error'].splitlines()[0]}")
    print("-" * 72)
    print(f"  {s['passed']}/{s['total']} passed  "
          f"(failed={s['failed']}, errored={s['errored']})  =>  VERDICT: {s['verdict']}")
    print(f"  code digest: {ledger['provenance']['code_digest']['combined_sha256'][:16]}…")
    print(f"  JSON: {json_path}")
    if pdf_path:
        print(f"  PDF : {pdf_path}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(description="ThermalFlow automated compliance runner")
    ap.add_argument("--repo-root", default=os.path.abspath(os.path.join(HERE, "..")),
                    help="repo root containing backend/ (default: parent of compliance/)")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "out"))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ledger = run(args.repo_root, args.out_dir, make_pdf=not args.no_pdf, quiet=args.quiet)
    sys.exit(0 if ledger["summary"]["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
