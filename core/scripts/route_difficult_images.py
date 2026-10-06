#!/usr/bin/env python3
"""Route only low-confidence Apple Vision images to local Qwen3-VL."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision-jsonl", required=True, type=Path)
    parser.add_argument("--variants-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mean-threshold", type=float, default=0.65)
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    args = parser.parse_args()

    rows = [json.loads(x) for x in args.vision_jsonl.read_text(encoding="utf-8").splitlines() if x.strip()]
    selected: list[tuple[str, float]] = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    adapter = Path(__file__).with_name("run_ollama_vision.py")
    log = args.output_dir / "batch.log"
    with log.open("w", encoding="utf-8") as handle:
        for row in rows:
            scores = [float(item.get("confidence") or 0) for item in row.get("items") or []]
            mean = sum(scores) / len(scores) if scores else 0.0
            if mean >= args.mean_threshold and not row.get("error"):
                continue
            stem = Path(row["file"]).stem
            proxy = args.variants_dir / stem / "06_vlm_proxy.png"
            if not proxy.is_file():
                raise SystemExit(f"missing VLM proxy: {proxy}")
            result_path = args.output_dir / f"{stem}.{args.model.replace(':', '-')}.json"
            if result_path.is_file() and result_path.stat().st_size > 2:
                selected.append((stem, mean))
                continue
            command = [
                sys.executable, str(adapter), "--image", str(proxy),
                "--source-id", stem, "--output-dir", str(args.output_dir),
                "--model", args.model,
            ]
            completed = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, text=True)
            if completed.returncode != 0:
                raise SystemExit(f"vision upgrade failed for {stem}; see {log}")
            selected.append((stem, mean))
    receipt = {
        "schema_version": 1,
        "model": args.model,
        "input_image_count": len(rows),
        "selected_count": len(selected),
        "mean_threshold": args.mean_threshold,
        "selected": [{"source_stem": stem, "apple_vision_mean": round(mean, 4)} for stem, mean in selected],
        "content_included": False,
        "log": str(log),
    }
    path = args.output_dir / "batch_vision_receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
