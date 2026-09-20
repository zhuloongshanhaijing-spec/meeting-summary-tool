#!/usr/bin/env python3
"""Create conservative ASR-ready audio without touching source recordings."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


# Extra ffmpeg candidates come from MST_FFMPEG (colon-separated); PATH search
# (shutil.which) stays the primary resolution — see INSTALL.md.
KNOWN_FFMPEG = [
    Path(part)
    for part in (os.environ.get("MST_FFMPEG") or "").split(":")
    if part
]

# arnndn model search path (in-project and system)
_ARNNNDN_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "models" / "arnndn" / "cb.rnnn",
    Path.home() / ".local" / "share" / "arnndn" / "cb.rnnn",
]


def find_ffmpeg(explicit: Path | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(explicit)
    environment_path = os.environ.get("MEETING_FFMPEG")
    if environment_path:
        candidates.append(Path(environment_path))
    discovered = shutil.which("ffmpeg")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(KNOWN_FFMPEG)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise SystemExit("ffmpeg not found; pass --ffmpeg or set MEETING_FFMPEG")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--clip-start", type=float)
    parser.add_argument("--clip-seconds", type=float)
    parser.add_argument(
        "--enhanced",
        action="store_true",
        help="also create a conservative impulse/noise-reduced comparison track",
    )
    parser.add_argument(
        "--arnndn-model",
        type=Path,
        help="path to arnndn .rnnn model; auto-detected from project tree by default",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore cached fingerprints and re-run ffmpeg for every source",
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    output_dir = args.run_dir / "artifacts" / "audio" / "normalized"
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(args.ffmpeg)

    # Resolve the arnndn model and planned filter chain up front: both are
    # fingerprint inputs, so the skip decision and a fresh run must agree.
    arnndn_model = args.arnndn_model
    if arnndn_model is None:
        for candidate in _ARNNNDN_CANDIDATES:
            if candidate.is_file() and candidate.stat().st_size > 1000:
                arnndn_model = candidate
                break
    planned_filter = "highpass=f=60,loudnorm=I=-20:LRA=11:TP=-2"
    planned_enhanced_filter = "highpass=f=60,adeclick=w=55:o=75:a=2:t=2,"
    if arnndn_model is not None and arnndn_model.is_file():
        planned_enhanced_filter += f"arnndn=m={arnndn_model},"
    planned_enhanced_filter += "afftdn=nf=-35:tn=1,loudnorm=I=-20:LRA=11:TP=-2"

    cache_dir = args.run_dir / "artifacts" / "audio" / ".idempotency"
    cache_dir.mkdir(parents=True, exist_ok=True)
    records = []
    skipped = 0

    def fingerprint_for(source: Path) -> dict:
        stat = source.stat()
        return {
            "source_bytes": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "clip_start": args.clip_start,
            "clip_seconds": args.clip_seconds,
            "enhanced": bool(args.enhanced),
            "planned_filter": planned_filter,
            "planned_enhanced_filter": planned_enhanced_filter if args.enhanced else None,
            "ffmpeg": str(ffmpeg),
        }

    def outputs_intact(cached: dict) -> bool:
        for entry in cached.get("outputs", []):
            path = args.run_dir / entry["path"]
            if not path.is_file():
                return False
            if abs(path.stat().st_size - entry["bytes"]) > entry["bytes"] * 0.01:
                return False
        return True

    for item in manifest["files"]:
        if item.get("kind") != "audio":
            continue
        source = source_root / item["relative_path"]
        cache_file = cache_dir / f"{item['source_id']}.json"
        if not args.force and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cached = None
            if cached and cached.get("fingerprint") == fingerprint_for(source) and outputs_intact(cached):
                records.extend(cached["records"])
                skipped += 1
                print(f"SKIP {item['source_id']} (fingerprint match, artifacts reused)", flush=True)
                continue

        output = output_dir / f"{item['source_id']}.wav"
        command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
        if args.clip_start is not None:
            command += ["-ss", str(args.clip_start)]
        command += ["-i", str(source)]
        if args.clip_seconds is not None:
            command += ["-t", str(args.clip_seconds)]
        command += [
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            planned_filter,
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 44:
            raise SystemExit(
                f"audio preparation failed for {source}: "
                f"{completed.stderr[-2000:]}"
            )
        file_records = [
            {
                "source_id": item["source_id"],
                "source": str(source),
                "output": str(output.relative_to(args.run_dir)),
                "route": "original_normalized",
                "sample_rate_hz": 16000,
                "channels": 1,
                "clip_start": args.clip_start,
                "clip_seconds": args.clip_seconds,
            }
        ]
        produced = [{"path": str(output.relative_to(args.run_dir)), "bytes": output.stat().st_size}]
        enhanced_filter = planned_enhanced_filter
        if args.enhanced:
            enhanced_dir = args.run_dir / "artifacts" / "audio" / "enhanced"
            enhanced_dir.mkdir(parents=True, exist_ok=True)
            enhanced_output = enhanced_dir / f"{item['source_id']}.wav"
            enhanced_command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
            if args.clip_start is not None:
                enhanced_command += ["-ss", str(args.clip_start)]
            enhanced_command += ["-i", str(source)]
            if args.clip_seconds is not None:
                enhanced_command += ["-t", str(args.clip_seconds)]
            enhanced_command += [
                "-vn", "-ac", "1", "-ar", "16000", "-af", enhanced_filter,
                "-c:a", "pcm_s16le", str(enhanced_output),
            ]
            enhanced_completed = subprocess.run(
                enhanced_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if enhanced_completed.returncode != 0 or not enhanced_output.is_file() or enhanced_output.stat().st_size <= 44:
                raise SystemExit(
                    f"enhanced audio preparation failed for {source}: "
                    f"{enhanced_completed.stderr[-2000:]}"
                )
            # Verify enhanced output duration matches source (arnndn may silently truncate)
            import subprocess as _sp
            src_dur = _sp.run(
                [str(ffmpeg), "-hide_banner", "-i", str(source), "-f", "null", "-"],
                capture_output=True, text=True
            )
            import re as _re
            src_match = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", src_dur.stderr)
            enh_dur = _sp.run(
                [str(ffmpeg), "-hide_banner", "-i", str(enhanced_output), "-f", "null", "-"],
                capture_output=True, text=True
            )
            enh_match = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", enh_dur.stderr)
            if src_match and enh_match:
                src_sec = int(src_match.group(1))*3600 + int(src_match.group(2))*60 + float(src_match.group(3))
                enh_sec = int(enh_match.group(1))*3600 + int(enh_match.group(2))*60 + float(enh_match.group(3))
                if enh_sec < src_sec * 0.9:
                    # arnndn truncated — fall back to afftdn-only
                    fallback_filter = "highpass=f=60,adeclick=w=55:o=75:a=2:t=2,afftdn=nf=-35:tn=1,loudnorm=I=-20:LRA=11:TP=-2"
                    fallback_cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
                    if args.clip_start is not None:
                        fallback_cmd += ["-ss", str(args.clip_start)]
                    fallback_cmd += ["-i", str(source)]
                    if args.clip_seconds is not None:
                        fallback_cmd += ["-t", str(args.clip_seconds)]
                    fallback_cmd += ["-vn", "-ac", "1", "-ar", "16000", "-af", fallback_filter, "-c:a", "pcm_s16le", str(enhanced_output)]
                    fb = _sp.run(fallback_cmd, capture_output=True, text=True)
                    if fb.returncode != 0:
                        raise SystemExit(f"fallback enhanced audio also failed: {fb.stderr[-2000:]}")
                    enhanced_filter = fallback_filter
                    print(f"WARNING: arnndn truncated {source.name} ({enh_sec:.0f}s < {src_sec:.0f}s), fell back to afftdn-only", flush=True)
            file_records.append(
                {
                    "source_id": item["source_id"],
                    "source": str(source),
                    "output": str(enhanced_output.relative_to(args.run_dir)),
                    "route": "impulse_noise_reduced_candidate",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "clip_start": args.clip_start,
                    "clip_seconds": args.clip_seconds,
                    "filter": enhanced_filter,
                }
            )
            produced.append({"path": str(enhanced_output.relative_to(args.run_dir)), "bytes": enhanced_output.stat().st_size})
        cache_file.write_text(json.dumps({
            "fingerprint": fingerprint_for(source),
            "records": file_records,
            "outputs": produced,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        records.extend(file_records)

    if not records:
        raise SystemExit("manifest contains no audio files")
    receipt = {
        "schema_version": 1,
        "ffmpeg": str(ffmpeg),
        "filter": planned_filter,
        "enhanced_requested": args.enhanced,
        "enhanced_filter": planned_enhanced_filter if args.enhanced else None,
        "arnndn_model": str(arnndn_model) if (args.enhanced and arnndn_model) else None,
        "skipped_fingerprint_matches": skipped,
        "tracks": records,
    }
    receipt_path = args.run_dir / "artifacts" / "audio" / "prepare_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    if skipped:
        print(f"idempotent skip: {skipped} source(s) reused from artifacts (use --force to re-run ffmpeg)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
