#!/usr/bin/env python3
"""Resume only valid UTF-8 Whisper outputs; quarantine corrupt checkpoints."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def valid_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data.get("transcription"), list) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--continuation-receipt", required=True, type=Path)
    parser.add_argument("--whisper-cli", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    sources = sorted(args.input_dir.glob("*.wav"))
    if not sources:
        raise SystemExit("no escalation WAV files")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logs, quarantine = args.output_dir / "logs", args.output_dir / "invalid_utf8_quarantine"
    logs.mkdir(exist_ok=True); quarantine.mkdir(exist_ok=True)
    records, repaired, unavailable = [], [], []
    for index, source in enumerate(sources, start=1):
        prefix = args.output_dir / source.stem
        output = prefix.with_suffix(".json")
        payload = valid_json(output) if output.exists() else None
        reused = payload is not None
        if output.exists() and payload is None:
            target = quarantine / output.name
            if target.exists(): target = quarantine / f"{output.stem}.{index}.json"
            shutil.move(str(output), str(target))
            repaired.append(output.name)
        if not reused:
            env = os.environ.copy(); env.update({"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
            command = [str(args.whisper_cli), "-m", str(args.model), "-f", str(source), "-l", args.language, "-oj", "-of", str(prefix), "-np", "-sns"]
            with (logs / f"{source.stem}.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env, check=False)
            payload = valid_json(output)
            if result.returncode or payload is None:
                if output.exists():
                    shutil.move(str(output), str(quarantine / f"{output.stem}.rerun-invalid.json"))
                unavailable.append(source.name)
                continue
        records.append({"input": str(source), "output": str(output), "log": str(logs / f"{source.stem}.log"), "segment_count": len(payload["transcription"]), "reused": reused})
        if index % 10 == 0 or index == len(sources):
            atomic_json(args.continuation_receipt, {"schema_version": 1, "status": "running_whisper_utf8_repair", "escalated_route_count": len(sources), "whisper_completed_count": index, "invalid_utf8_quarantined_count": len(repaired), "unavailable_whisper_route_count": len(unavailable), "content_included": False})
    atomic_json(args.output_dir / "whisper_receipt.json", {"schema_version": 1, "status": "complete_with_unavailable_routes", "records": records, "unavailable_routes": unavailable, "reused_count": sum(r["reused"] for r in records), "utf8_repaired_count": len(repaired), "content_included": False})
    atomic_json(args.continuation_receipt, {"schema_version": 1, "status": "secondary_asr_complete", "escalated_route_count": len(sources), "paraformer_record_count": len(sources), "whisper_record_count": len(records), "unavailable_whisper_route_count": len(unavailable), "invalid_utf8_quarantined_count": len(repaired), "content_included": False})


if __name__ == "__main__":
    main()
