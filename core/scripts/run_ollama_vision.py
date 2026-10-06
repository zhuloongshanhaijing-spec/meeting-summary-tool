#!/usr/bin/env python3
"""Run a local Ollama vision model and keep recognized content in an artifact."""

from __future__ import annotations

import argparse
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path


REQUIRED_KEYS = {"visible_text", "uncertain_text", "layout", "notes"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--source-id", help="stable original image stem for output naming")
    args = parser.parse_args()

    if not args.image.is_file():
        raise SystemExit(f"image not found: {args.image}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(args.image.read_bytes()).decode("ascii")
    prompt = (
        "Read this photographed slide conservatively. Do not invent unreadable characters. "
        "Return JSON only with: visible_text (list of directly visible strings in reading order), "
        "uncertain_text (list of objects with candidates and reason), layout (headings and list hierarchy), "
        "and notes (only image-quality observations, not inferred meeting facts). "
        "Keep literal text separate from contextual guesses."
    )
    output_schema = {
        "type": "object",
        "properties": {
            "visible_text": {"type": "array", "items": {"type": "string"}},
            "uncertain_text": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidates": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["candidates", "reason"],
                },
            },
            "layout": {"type": "object"},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["visible_text", "uncertain_text", "layout", "notes"],
    }
    payload = {
        "model": args.model,
        "stream": False,
        "think": False,
        "keep_alive": 0,
        "format": output_schema,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1024},
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [encoded],
            }
        ],
    }
    request = urllib.request.Request(
        args.ollama_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            envelope = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Ollama vision call failed: {exc}") from exc
    raw_content = ((envelope.get("message") or {}).get("content") or "").strip()
    try:
        result = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"vision model returned invalid JSON: {exc}") from exc
    missing = sorted(REQUIRED_KEYS - set(result)) if isinstance(result, dict) else sorted(REQUIRED_KEYS)
    if missing:
        raise SystemExit(f"vision result missing keys: {missing}")
    output_stem = args.source_id or args.image.stem
    result_path = args.output_dir / f"{output_stem}.{args.model.replace(':', '-')}.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "adapter": "Ollama vision",
        "model": args.model,
        "image": str(args.image),
        "source_id": args.source_id,
        "output": str(result_path),
        "visible_item_count": len(result.get("visible_text") or []),
        "uncertain_item_count": len(result.get("uncertain_text") or []),
        "done": envelope.get("done"),
        "total_duration_ns": envelope.get("total_duration"),
    }
    receipt_path = args.output_dir / "vision_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
