#!/usr/bin/env python3
"""Advance configured meeting stages until completion or bounded supervision."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--through", default="render")
    args = parser.parse_args()
    controller = Path(__file__).with_name("meeting_pipeline.py")
    state_path = args.run_dir / "state.json"
    if not state_path.is_file():
        raise SystemExit(f"run is not initialized: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    order = state.get("stage_order") or []
    if args.through not in order:
        raise SystemExit(f"unknown terminal stage: {args.through}")
    terminal_index = order.index(args.through)
    advanced = []
    for stage in order[: terminal_index + 1]:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        stage_state = state["stages"][stage]
        if stage_state.get("status") in {"passed", "skipped"}:
            continue
        completed = subprocess.run(
            [sys.executable, str(controller), "execute", "--run-dir", str(args.run_dir), "--stage", stage],
            stdin=subprocess.DEVNULL,
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        observed = state["stages"][stage].get("status")
        if completed.returncode != 0 or observed != "passed":
            packet = args.run_dir / "supervisor_packet.json"
            print(json.dumps({
                "status": "NEEDS_SUPERVISOR",
                "stage": stage,
                "observed": observed,
                "controller_exit_code": completed.returncode,
                "supervisor_packet": str(packet),
                "advanced_stages": advanced,
                "content_included": False,
            }, ensure_ascii=False))
            return 2
        advanced.append(stage)
    print(json.dumps({
        "status": "READY_FOR_ARCHIVE_PLAN" if args.through == "render" else "COMPLETE",
        "through": args.through,
        "advanced_stages": advanced,
        "content_included": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
