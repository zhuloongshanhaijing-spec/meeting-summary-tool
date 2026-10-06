#!/usr/bin/env python3
"""Wait for reconciled evidence, then render and validate a fresh report package."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(command: list[str], log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with exit {completed.returncode}; inspect {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--reconciled", required=True, type=Path)
    parser.add_argument("--reconcile-receipt", required=True, type=Path)
    parser.add_argument("--checkpoint-dir", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--stale-seconds", type=int, default=1200)
    parser.add_argument("--max-wait-seconds", type=int, default=10800)
    parser.add_argument(
        "--deterministic-final-report",
        action="store_true",
        help="Skip the optional whole-corpus final synthesis and validate the evidence-linked deterministic report.",
    )
    args = parser.parse_args()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.work_dir / "finish_receipt.json"
    log_path = args.work_dir / "finish.log"
    started = time.time()
    atomic_json(state_path, {"schema_version": 1, "status": "waiting_for_reconciliation", "content_included": False})

    while not args.reconcile_receipt.is_file():
        checkpoints = list(args.checkpoint_dir.glob("chunk-*.json"))
        newest = max((path.stat().st_mtime for path in checkpoints), default=started)
        now = time.time()
        if now - newest > args.stale_seconds:
            atomic_json(state_path, {
                "schema_version": 1,
                "status": "blocked",
                "reason": "reconciliation_checkpoint_stale",
                "checkpoint_count": len(checkpoints),
                "content_included": False,
            })
            return 2
        if now - started > args.max_wait_seconds:
            atomic_json(state_path, {
                "schema_version": 1,
                "status": "blocked",
                "reason": "reconciliation_wait_timeout",
                "checkpoint_count": len(checkpoints),
                "content_included": False,
            })
            return 2
        time.sleep(args.poll_seconds)

    reconciliation = json.loads(args.reconcile_receipt.read_text(encoding="utf-8"))
    if reconciliation.get("evidence_count", 0) <= 0 or reconciliation.get("disposition_count") != reconciliation.get("evidence_count"):
        atomic_json(state_path, {"schema_version": 1, "status": "blocked", "reason": "invalid_reconcile_receipt", "content_included": False})
        return 2

    atomic_json(state_path, {"schema_version": 1, "status": "rendering", "content_included": False})
    run([
        sys.executable, "-B", str(args.skill_dir / "scripts/build_package.py"),
        "--evidence", str(args.evidence), "--reconciled", str(args.reconciled),
        "--output-dir", str(args.package_dir),
    ], log_path)
    atomic_json(state_path, {"schema_version": 1, "status": "synthesizing_final_report", "content_included": False})
    final_report_command = [
        sys.executable, "-B", str(args.skill_dir / "scripts/run_ollama_final_report.py"),
        "--reconciled", str(args.reconciled), "--package-dir", str(args.package_dir),
        "--model", args.model,
    ]
    if args.deterministic_final_report:
        final_report_command.append("--deterministic-only")
    run(final_report_command, log_path)
    atomic_json(state_path, {"schema_version": 1, "status": "validating", "content_included": False})
    run([
        sys.executable, "-B", str(args.skill_dir / "scripts/validate_package.py"),
        "--manifest", str(args.manifest), "--reconciled", str(args.reconciled),
        "--package-dir", str(args.package_dir),
    ], log_path)
    validation = json.loads((args.package_dir / "validation_receipt.json").read_text(encoding="utf-8"))
    if validation.get("status") != "PASS":
        raise RuntimeError("final package validation did not pass")
    atomic_json(state_path, {
        "schema_version": 1,
        "status": "complete",
        "package_dir": str(args.package_dir),
        "required_artifacts": validation.get("checks", {}),
        "content_included": False,
    })
    print(state_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"report rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
