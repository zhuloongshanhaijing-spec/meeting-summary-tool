#!/usr/bin/env python3
"""Run the bounded local sample pipeline without content-level supervision."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    benchmark = root.parent / "lectures-benchmark-20260911"
    run = root / "autonomous-run-20260913-v2"
    run.mkdir(parents=True, exist_ok=True)
    qwen_python = Path(os.environ["MST_QWEN_PYTHON"]) if os.environ.get("MST_QWEN_PYTHON") else None
    qwen_script = Path(__file__).resolve().parents[2] / "core" / "scripts" / "run_qwen3_asr.py"
    assert qwen_python, "set MST_QWEN_PYTHON before using this legacy driver"
    stages = [
        ("asr_primary", [str(qwen_python), "-B", str(qwen_script), "--input", str(benchmark / "sample-flat"), "--output-dir", str(run / "qwen3-asr"), "--model", "Qwen/Qwen3-ASR-1.7B", "--language", "Chinese", "--device", "mps", "--resume"]),
        ("literal", [sys.executable, str(root / "scripts/build_sample_literal.py"), "--qwen", str(run / "qwen3-asr/qwen3_asr_candidates.json"), "--output", str(run / "literal_records.jsonl"), "--receipt", str(run / "literal_receipt.json"), "--start", "60", "--end", "105"]),
        ("note_evidence", [sys.executable, str(root / "scripts/build_note_evidence.py"), "--manifest", str(benchmark / "manifest.json"), "--output", str(run / "note_evidence.jsonl"), "--receipt", str(run / "note_receipt.json")]),
        ("note_retrieval", [sys.executable, str(root / "scripts/retrieve_note_links.py"), "--notes", str(run / "note_evidence.jsonl"), "--records", str(run / "literal_records.jsonl"), "--output", str(run / "note_candidates.jsonl"), "--receipt", str(run / "note_candidates_receipt.json"), "--minimum-score", "0.20", "--document-minimum-score", "0.02"]),
        ("note_relation", [sys.executable, str(root / "scripts/classify_note_links.py"), "--notes", str(run / "note_evidence.jsonl"), "--records", str(run / "literal_records.jsonl"), "--relations", str(run / "note_candidates.jsonl"), "--output", str(run / "note_relations.jsonl"), "--receipt", str(run / "note_relation_receipt.json"), "--model", "qwen3:8b"]),
        ("render", [sys.executable, str(root / "scripts/render_note_test.py"), "--records", str(run / "literal_records.jsonl"), "--notes", str(run / "note_evidence.jsonl"), "--relations", str(run / "note_relations.jsonl"), "--output-dir", str(run / "output"), "--receipt", str(run / "output/render_receipt.json")]),
    ]
    receipt = {"status": "running", "mode": "autonomous_light_sample", "content_level_supervision": False, "stages": []}
    receipt_path = run / "autonomous_receipt.json"
    for name, command in stages:
        started = time.time()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        row = {"stage": name, "exit_code": result.returncode, "elapsed_seconds": round(time.time() - started, 3), "log_tail": result.stdout[-1000:]}
        receipt["stages"].append(row)
        if result.returncode:
            receipt["status"] = "BLOCKED"
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result.returncode
    receipt["status"] = "COMPLETE"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
