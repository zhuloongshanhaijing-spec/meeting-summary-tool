#!/usr/bin/env python3
"""Create deterministic overlapping ASR windows for every acoustic route."""

from __future__ import annotations

import argparse
import json
import subprocess
import wave
from pathlib import Path

from prepare_audio import find_ffmpeg


ROUTES = ("original", "normalized", "impulse_noise_reduced")


def valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as reader:
            return reader.getnchannels() == 1 and reader.getframerate() == 16000 and reader.getnframes() > 0
    except (OSError, wave.Error):
        return False


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--prepared-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--window-seconds", type=float, default=20.0)
    parser.add_argument("--overlap-seconds", type=float, default=5.0)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.window_seconds <= 0 or args.overlap_seconds < 0 or args.overlap_seconds >= args.window_seconds:
        raise SystemExit("require 0 <= overlap-seconds < window-seconds")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    audio_items = [item for item in manifest["files"] if item.get("kind") == "audio"]
    ffmpeg = find_ffmpeg(args.ffmpeg)
    flat = args.output_dir / "flat"
    flat.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output_dir / "window_receipt.json"
    records = []
    stride = args.window_seconds - args.overlap_seconds

    for item in audio_items:
        source_id = item["source_id"]
        normalized = args.prepared_run / "artifacts/audio/normalized" / f"{source_id}.wav"
        enhanced = args.prepared_run / "artifacts/audio/enhanced" / f"{source_id}.wav"
        with wave.open(str(normalized), "rb") as reader:
            duration = reader.getnframes() / reader.getframerate()
        routes = {
            "original": source_root / item["relative_path"],
            "normalized": normalized,
            "impulse_noise_reduced": enhanced,
        }
        start = 0.0
        while start < duration:
            end = min(duration, start + args.window_seconds)
            start_ms, end_ms = round(start * 1000), round(end * 1000)
            window_id = f"{source_id}_{start_ms:010d}_{end_ms:010d}"
            for route, input_path in routes.items():
                output = flat / f"{window_id}__{route}.wav"
                reused = args.resume and valid_wav(output)
                if not reused:
                    command = [
                        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
                        "-ss", f"{start:.3f}", "-i", str(input_path), "-t", f"{end - start:.3f}",
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output),
                    ]
                    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if completed.returncode != 0 or not valid_wav(output):
                        raise SystemExit(f"window extraction failed: {window_id}:{route}")
                records.append({
                    "window_id": window_id,
                    "source_id": source_id,
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "route": route,
                    "output": str(output),
                    "reused": reused,
                })
            atomic_json(receipt_path, {
                "schema_version": 1,
                "status": "in_progress",
                "window_seconds": args.window_seconds,
                "overlap_seconds": args.overlap_seconds,
                "completed_route_count": len(records),
                "records": records,
                "content_included": False,
            })
            if end >= duration:
                break
            start += stride

    payload = {
        "schema_version": 1,
        "status": "complete",
        "window_seconds": args.window_seconds,
        "overlap_seconds": args.overlap_seconds,
        "window_count": len({item["window_id"] for item in records}),
        "route_count": len(records),
        "records": records,
        "content_included": False,
    }
    atomic_json(receipt_path, payload)
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
