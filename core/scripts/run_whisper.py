#!/usr/bin/env python3
"""Run whisper.cpp quietly and expose only metadata to the controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


DEFAULT_ROOT = Path(os.environ["MST_WHISPER_ROOT"]) if os.environ.get("MST_WHISPER_ROOT") else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--whisper-cli", type=Path, default=DEFAULT_ROOT / "build/bin/whisper-cli")
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_ROOT / "models/ggml-large-v3-turbo-q5_0.bin",
    )
    parser.add_argument("--vad-model", type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    for path, label in ((args.whisper_cli, "whisper-cli"), (args.model, "model")):
        if not path.is_file():
            raise SystemExit(f"{label} not found: {path}")
    if args.vad_model and not args.vad_model.is_file():
        raise SystemExit(f"VAD model not found: {args.vad_model}")
    inputs = sorted(args.input_dir.glob("*.wav"))
    if not inputs:
        raise SystemExit(f"no WAV files found: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logs = args.output_dir / "logs"
    logs.mkdir(exist_ok=True)
    records = []
    for source in inputs:
        prefix = args.output_dir / source.stem
        output_path = prefix.with_suffix(".json")
        command = [
            str(args.whisper_cli),
            "-m",
            str(args.model),
            "-f",
            str(source),
            "-l",
            args.language,
            "-oj",
            "-of",
            str(prefix),
            "-np",
            "-sns",
        ]
        if args.cpu_only:
            command.insert(1, "-ng")
        if args.vad_model:
            command += ["--vad", "-vm", str(args.vad_model)]
        log_path = logs / f"{source.stem}.log"
        reused = False
        payload = None
        if args.resume and output_path.is_file():
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                reused = isinstance(payload.get("transcription"), list)
            except (OSError, json.JSONDecodeError):
                payload = None
        if not reused:
            environment = os.environ.copy()
            with log_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    env=environment,
                )
            if completed.returncode != 0 or not output_path.is_file():
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise SystemExit(f"Whisper failed for {source}; log tail:\n{tail}")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        segments = payload.get("transcription")
        if not isinstance(segments, list):
            raise SystemExit(f"Whisper transcription is not a segment list for {source}")
        invalid_offsets = 0
        for segment in segments:
            offsets = segment.get("offsets") or {}
            if offsets.get("from", 0) < 0 or offsets.get("to", 0) < offsets.get("from", 0):
                invalid_offsets += 1
        if invalid_offsets:
            raise SystemExit(f"Whisper produced {invalid_offsets} invalid timestamp segments")
        records.append(
            {
                "input": str(source),
                "output": str(output_path),
                "log": str(log_path),
                "language": (payload.get("result") or {}).get("language"),
                "segment_count": len(segments),
                "invalid_timestamp_count": invalid_offsets,
                "reused": reused,
            }
        )
    receipt = {
        "schema_version": 1,
        "adapter": "whisper.cpp",
        "binary": str(args.whisper_cli),
        "model": str(args.model),
        "model_sha256": sha256(args.model),
        "vad_model": str(args.vad_model) if args.vad_model else None,
        "cpu_only": args.cpu_only,
        "reused_count": sum(record["reused"] for record in records),
        "records": records,
    }
    receipt_path = args.output_dir / "whisper_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
