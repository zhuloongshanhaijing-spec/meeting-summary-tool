#!/usr/bin/env python3
"""Run the local audio-first lecture pipeline and its separate note layer."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path


def atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    root = workspace / "note-layer"
    run = workspace / "runs/lectures-full-autonomous-20260913"
    output = workspace / "outputs/lectures-full-autonomous-20260913"
    source = Path(os.environ["MST_NOTE_SOURCE"]) if os.environ.get("MST_NOTE_SOURCE") else None
    assert source, "set MST_NOTE_SOURCE (path to your raw lecture materials) before using this legacy driver"
    core = workspace / "core"
    receipt_path = run / "autonomous_receipt.json"
    receipt = {"status": "RUNNING", "mode": "local_audio_first_note_corroboration", "content_level_supervision": False, "source_mutated": False, "stages": []}
    atomic(receipt_path, receipt)
    stages = [
        ("audio_pipeline", [sys.executable, "-B", str(core / "scripts/adaptive_pipeline_v3.py"), "--run-dir", str(run), "--config", str(run / "audio-pipeline.local.json"), "--source-root", str(source), "--poll-seconds", "15"]),
        ("note_evidence", [sys.executable, str(root / "scripts/build_note_evidence.py"), "--manifest", str(run / "manifest.json"), "--output", str(run / "notes/note_evidence.jsonl"), "--receipt", str(run / "notes/note_receipt.json")]),
        ("note_retrieval", [sys.executable, str(root / "scripts/retrieve_note_links.py"), "--notes", str(run / "notes/note_evidence.jsonl"), "--records", str(run / "literal/literal_records.jsonl"), "--output", str(run / "notes/note_candidates.jsonl"), "--receipt", str(run / "notes/note_candidates_receipt.json"), "--minimum-score", "0.20", "--document-minimum-score", "0.02"]),
        ("note_relation", [sys.executable, str(root / "scripts/classify_note_links.py"), "--notes", str(run / "notes/note_evidence.jsonl"), "--records", str(run / "literal/literal_records.jsonl"), "--relations", str(run / "notes/note_candidates.jsonl"), "--output", str(run / "notes/note_relations.jsonl"), "--receipt", str(run / "notes/note_relation_receipt.json"), "--model", "qwen3:8b"]),
        ("note_safety", [sys.executable, str(root / "scripts/sanitize_note_relations.py"), "--input", str(run / "notes/note_relations.jsonl"), "--output", str(run / "notes/note_relations_safe.jsonl"), "--receipt", str(run / "notes/note_relation_safety_receipt.json")]),
        ("render", [sys.executable, str(root / "scripts/render_note_test.py"), "--records", str(run / "literal/literal_records.jsonl"), "--notes", str(run / "notes/note_evidence.jsonl"), "--relations", str(run / "notes/note_relations_safe.jsonl"), "--output-dir", str(output), "--receipt", str(output / "render_receipt.json")]),
    ]
    for name, command in stages:
        started = time.time()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        receipt["stages"].append({"stage": name, "exit_code": result.returncode, "elapsed_seconds": round(time.time() - started, 3), "log_tail": result.stdout[-2000:]})
        if result.returncode:
            receipt["status"] = "BLOCKED"
            atomic(receipt_path, receipt)
            return result.returncode
        atomic(receipt_path, receipt)
    receipt["status"] = "COMPLETE_WITH_UNCERTAINTY"
    atomic(receipt_path, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
