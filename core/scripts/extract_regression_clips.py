#!/usr/bin/env python3
"""Extract bounded audio clips on multiple immutable candidate routes."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from prepare_audio import find_ffmpeg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--specs", required=True, type=Path)
    parser.add_argument("--prepared-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    sources = {item["source_id"]: item for item in manifest["files"]}
    specs = json.loads(args.specs.read_text(encoding="utf-8"))
    ffmpeg = find_ffmpeg(args.ffmpeg)
    flat_dir = args.output_dir / "flat"
    flat_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for spec in specs.get("clips") or []:
        source_id = spec["source_id"]
        item = sources.get(source_id)
        if not item or item.get("kind") != "audio":
            raise SystemExit(f"unknown audio source_id: {source_id}")
        start = float(spec["start_seconds"])
        end = float(spec["end_seconds"])
        if start < 0 or end <= start:
            raise SystemExit(f"invalid clip range: {spec}")
        routes = {
            "original": source_root / item["relative_path"],
            "normalized": args.prepared_run / "artifacts/audio/normalized" / f"{source_id}.wav",
            "impulse_noise_reduced": args.prepared_run / "artifacts/audio/enhanced" / f"{source_id}.wav",
        }
        clip_dir = args.output_dir / spec["clip_id"]
        clip_dir.mkdir(parents=True, exist_ok=True)
        for route, input_path in routes.items():
            if not input_path.is_file():
                raise SystemExit(f"missing route input: {input_path}")
            output = clip_dir / f"{route}.wav"
            command = [
                str(ffmpeg), "-hide_banner", "-nostdin", "-y",
                "-ss", str(start), "-i", str(input_path), "-t", str(end - start),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output),
            ]
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 44:
                raise SystemExit(f"clip extraction failed for {spec['clip_id']}:{route}")
            flat_output = flat_dir / f"{spec['clip_id']}__{route}.wav"
            shutil.copyfile(output, flat_output)
            records.append({
                "clip_id": spec["clip_id"],
                "source_id": source_id,
                "start_seconds": start,
                "end_seconds": end,
                "reason": spec.get("reason"),
                "route": route,
                "output": str(output),
                "flat_output": str(flat_output),
                "bytes": output.stat().st_size,
            })
    receipt = {
        "schema_version": 1,
        "clip_count": len({record["clip_id"] for record in records}),
        "route_count": len(records),
        "records": records,
        "content_included": False,
    }
    path = args.output_dir / "clip_receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
