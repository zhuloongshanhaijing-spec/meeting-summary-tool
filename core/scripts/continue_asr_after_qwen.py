#!/usr/bin/env python3
"""Wait for primary ASR, then run bounded independent escalation routes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(command: list[str], log_path: Path, environment: dict | None = None) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=environment, check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with exit {completed.returncode}; inspect {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", required=True, type=Path)
    parser.add_argument("--qwen-dir", required=True, type=Path)
    parser.add_argument("--window-receipt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--funasr-python", required=True, type=Path)
    parser.add_argument("--funasr-model-cache", required=True, type=Path)
    parser.add_argument("--whisper-cli", required=True, type=Path)
    parser.add_argument("--whisper-model", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--stale-seconds", type=int, default=7200)
    args = parser.parse_args()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.work_dir / "continuation_receipt.json"
    log_path = args.work_dir / "continuation.log"
    qwen_candidates = args.qwen_dir / "qwen3_asr_candidates.json"
    state = {"schema_version": 1, "status": "waiting_for_qwen", "content_included": False}
    atomic_json(state_path, state)

    while True:
        if qwen_candidates.is_file():
            try:
                payload = json.loads(qwen_candidates.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if payload.get("status") == "complete":
                break
            if time.time() - qwen_candidates.stat().st_mtime > args.stale_seconds:
                state.update(status="blocked", reason="qwen_candidate_file_stale")
                atomic_json(state_path, state)
                return 2
        time.sleep(args.poll_seconds)

    escalation_dir = args.work_dir / "escalation"
    state.update(status="selecting_escalations")
    atomic_json(state_path, state)
    run([
        sys.executable, str(args.skill_dir / "scripts/select_asr_escalations.py"),
        "--qwen", str(qwen_candidates), "--window-receipt", str(args.window_receipt),
        "--output-dir", str(escalation_dir),
    ], log_path)
    escalation = json.loads((escalation_dir / "escalation_receipt.json").read_text(encoding="utf-8"))
    state.update(
        status="running_paraformer",
        escalated_window_count=escalation["escalated_window_count"],
        escalated_route_count=escalation["escalated_route_count"],
    )
    atomic_json(state_path, state)

    paraformer_dir = args.work_dir / "paraformer"
    run([
        str(args.funasr_python), str(args.skill_dir / "scripts/run_funasr.py"),
        "--input-dir", str(escalation_dir / "flat"), "--output-dir", str(paraformer_dir),
        "--model", "paraformer-zh", "--device", "cpu", "--chunk-seconds", "30",
        "--model-cache", str(args.funasr_model_cache), "--resume",
    ], log_path)
    paraformer = json.loads((paraformer_dir / "funasr_receipt.json").read_text(encoding="utf-8"))
    if len(paraformer.get("records", [])) != escalation["escalated_route_count"]:
        raise RuntimeError("Paraformer receipt count does not match escalation receipt")

    state.update(status="running_whisper")
    atomic_json(state_path, state)
    whisper_dir = args.work_dir / "whisper"
    run([
        sys.executable, str(args.skill_dir / "scripts/run_whisper.py"),
        "--input-dir", str(escalation_dir / "flat"), "--output-dir", str(whisper_dir),
        "--whisper-cli", str(args.whisper_cli), "--model", str(args.whisper_model),
        "--language", "zh", "--resume",
    ], log_path)
    whisper = json.loads((whisper_dir / "whisper_receipt.json").read_text(encoding="utf-8"))
    if len(whisper.get("records", [])) != escalation["escalated_route_count"]:
        raise RuntimeError("Whisper receipt count does not match escalation receipt")

    state.update(
        status="secondary_asr_complete",
        paraformer_record_count=len(paraformer["records"]),
        whisper_record_count=len(whisper["records"]),
    )
    atomic_json(state_path, state)
    print(state_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"continuation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
