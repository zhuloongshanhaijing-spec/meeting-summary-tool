#!/usr/bin/env python3
"""Run Qwen3-ASR on local audio and write machine-readable candidates.

The script deliberately keeps model logs separate from the JSON result so an
orchestrator can determine success without parsing (or displaying) transcript
content.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import sys
import time
from pathlib import Path


SUPPORTED = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--context", default="")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def audio_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(p for p in source.iterdir() if p.suffix.lower() in SUPPORTED)
    raise FileNotFoundError(source)


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.output_dir / "qwen3_asr_candidates.json"
    receipt_path = args.output_dir / "qwen3_asr_receipt.json"
    log_path = args.output_dir / "qwen3_asr.log"
    started = time.time()
    files = audio_files(args.input)
    if not files:
        raise RuntimeError(f"No supported audio found under {args.input}")

    existing_payload = {}
    if args.resume and result_path.is_file():
        existing_payload = json.loads(result_path.read_text(encoding="utf-8"))
        if existing_payload.get("model") != args.model:
            raise RuntimeError("resume model does not match existing candidate file")
    rows_by_audio = {
        item["audio"]: item for item in existing_payload.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
        and (item["text"].strip() or item.get("status") == "empty")
    }
    pending = [path for path in files if str(path.resolve()) not in rows_by_audio]

    if not pending:
        device = existing_payload.get("device", args.device)
        rows = [rows_by_audio[str(path.resolve())] for path in files]
        receipt = {
            "status": "complete", "engine": "Qwen3-ASR", "model": args.model, "device": device,
            "host": platform.machine(), "input_count": len(files), "output_count": len(rows),
            "nonempty_count": len(rows), "reused_count": len(rows), "elapsed_seconds": 0.0,
            "result": str(result_path.resolve()), "log": str(log_path.resolve()), "python": sys.version.split()[0],
        }
        atomic_json(receipt_path, receipt)
        print(receipt_path)
        return 0

    # Import and model chatter can be substantial; keep it in a local log.
    with log_path.open("a" if args.resume else "w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        import torch
        from qwen_asr import Qwen3ASRModel

        if args.device == "auto":
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        else:
            device = args.device
        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("MPS requested but unavailable in this process")

        dtype = torch.float16 if device == "mps" else torch.float32
        print(f"loading model={args.model} device={device} dtype={dtype}", flush=True)
        model = Qwen3ASRModel.from_pretrained(
            args.model,
            device_map=device,
            dtype=dtype,
            max_inference_batch_size=1,
            max_new_tokens=args.max_new_tokens,
        )
        for path in pending:
            item_started = time.time()
            outputs = model.transcribe(
                audio=str(path),
                language=args.language,
                context=args.context,
                return_time_stamps=False,
            )
            output = outputs[0]
            row = {
                "audio": str(path.resolve()),
                "language": getattr(output, "language", None),
                "text": getattr(output, "text", ""),
                "elapsed_seconds": round(time.time() - item_started, 3),
            }
            row["status"] = "complete" if row["text"].strip() else "empty"
            rows_by_audio[row["audio"]] = row
            rows = [rows_by_audio[str(item.resolve())] for item in files if str(item.resolve()) in rows_by_audio]
            payload = {
                "schema_version": 1, "engine": "Qwen3-ASR", "model": args.model, "device": device,
                "language_requested": args.language, "status": "in_progress", "items": rows,
            }
            atomic_json(result_path, payload)
            print(f"completed {path.name} chars={len(row['text'])}", flush=True)

    rows = [rows_by_audio[str(path.resolve())] for path in files]
    payload = {
        "schema_version": 1, "engine": "Qwen3-ASR", "model": args.model, "device": device,
        "language_requested": args.language, "status": "complete", "items": rows,
    }
    atomic_json(result_path, payload)
    receipt = {
        "status": "complete",
        "engine": "Qwen3-ASR",
        "model": args.model,
        "device": device,
        "host": platform.machine(),
        "input_count": len(files),
        "output_count": len(rows),
        "nonempty_count": sum(bool(row["text"].strip()) for row in rows),
        "reused_count": len(files) - len(pending),
        "elapsed_seconds": round(time.time() - started, 3),
        "result": str(result_path.resolve()),
        "log": str(log_path.resolve()),
        "python": sys.version.split()[0],
    }
    atomic_json(receipt_path, receipt)
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
