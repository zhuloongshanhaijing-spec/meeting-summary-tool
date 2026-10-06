#!/usr/bin/env python3
"""Normalize local ASR/OCR artifacts into stable, evidence-linked JSONL."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def collect_text(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [clean_text(value.get("text"))] if isinstance(value.get("text"), str) else []
        return [x for x in own if x] + [x for key, child in value.items() if key != "text" for x in collect_text(child)]
    if isinstance(value, list):
        return [x for child in value for x in collect_text(child)]
    return []


def offsets(segment: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = segment.get("offsets") or {}
    start = raw.get("from")
    end = raw.get("to")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        # whisper.cpp JSON offsets are milliseconds.
        return round(float(start) / 1000, 3), round(float(end) / 1000, 3)
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--whisper-dir", type=Path)
    parser.add_argument("--funasr-dir", type=Path)
    parser.add_argument("--vision-jsonl", type=Path)
    parser.add_argument("--vlm-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    files = manifest.get("files") or []
    by_name = {
        Path(f.get("relative_path") or f.get("path") or "").name: f["source_id"]
        for f in files
        if f.get("source_id") and (f.get("relative_path") or f.get("path"))
    }
    by_id = {f["source_id"]: f for f in files if f.get("source_id")}
    evidence: list[dict[str, Any]] = []

    if args.whisper_dir:
        for path in sorted(args.whisper_dir.glob("*.json")):
            if path.name.endswith("receipt.json"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            source_id = path.stem
            if source_id not in by_id:
                continue
            for segment in data.get("transcription") or []:
                text = clean_text(segment.get("text"))
                if not text:
                    continue
                start, end = offsets(segment)
                evidence.append({
                    "source_id": source_id,
                    "kind": "audio",
                    "locator": {"start_seconds": start, "end_seconds": end},
                    "literal_text": text,
                    "confidence": {"route": "whisper", "quality": "unrated"},
                    "models": ["whisper.cpp"],
                    "uncertainty": None,
                })

    if args.vision_jsonl:
        for record in read_jsonl(args.vision_jsonl):
            source_id = by_name.get(Path(record.get("file", "")).name)
            if not source_id:
                continue
            for item in record.get("items") or []:
                text = clean_text(item.get("text"))
                if not text:
                    continue
                confidence = float(item.get("confidence") or 0)
                evidence.append({
                    "source_id": source_id,
                    "kind": "image",
                    "locator": {k: item.get(k) for k in ("x", "y", "width", "height")},
                    "literal_text": text,
                    "confidence": {
                        "route": "apple_vision",
                        "score": confidence,
                        "quality": "high" if confidence >= 0.85 else "medium" if confidence >= 0.65 else "low",
                    },
                    "models": ["Apple Vision"],
                    "uncertainty": None if confidence >= 0.85 else "ocr_confidence_below_0.85",
                })

    if args.funasr_dir:
        for path in sorted(args.funasr_dir.glob("*.json")):
            if path.name.endswith("receipt.json"):
                continue
            source_id = path.stem
            if source_id not in by_id:
                continue
            chunks = json.loads(path.read_text(encoding="utf-8"))
            for chunk in chunks:
                text = clean_text(" ".join(collect_text(chunk.get("result"))))
                if not text:
                    continue
                evidence.append({
                    "source_id": source_id,
                    "kind": "audio",
                    "locator": {
                        "start_seconds": chunk.get("start_seconds"),
                        "end_seconds": chunk.get("end_seconds"),
                        "chunk_index": chunk.get("chunk_index"),
                    },
                    "literal_text": text,
                    "confidence": {"route": "sensevoice", "quality": "independent_candidate"},
                    "models": ["SenseVoiceSmall"],
                    "uncertainty": "secondary_asr_candidate_requires_reconciliation",
                })

    if args.vlm_dir:
        for path in sorted(args.vlm_dir.glob("*.json")):
            if path.name.endswith("receipt.json"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            # The image stem must begin with the original filename stem.
            source_id = next((sid for name, sid in by_name.items() if path.name.startswith(Path(name).stem)), None)
            if not source_id:
                continue
            for ordinal, text_raw in enumerate(data.get("visible_text") or [], start=1):
                text = clean_text(text_raw)
                if text:
                    evidence.append({
                        "source_id": source_id,
                        "kind": "image",
                        "locator": {"reading_order": ordinal, "route": "vlm"},
                        "literal_text": text,
                        "confidence": {"route": "qwen3_vl", "quality": "candidate"},
                        "models": ["Qwen3-VL"],
                        "uncertainty": "vision_language_candidate_requires_reconciliation",
                    })

    route_order = {"sensevoice": 0, "whisper": 1, "apple_vision": 2, "qwen3_vl": 3}
    evidence.sort(
        key=lambda item: (
            item["source_id"],
            float((item.get("locator") or {}).get("start_seconds") or 0),
            route_order.get((item.get("confidence") or {}).get("route"), 9),
        )
    )
    for ordinal, item in enumerate(evidence, start=1):
        item["evidence_id"] = f"E{ordinal:06d}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in evidence), encoding="utf-8")
    counts: dict[str, int] = {}
    for item in evidence:
        key = f"{item['kind']}:{item['confidence']['route']}"
        counts[key] = counts.get(key, 0) + 1
    receipt = {
        "schema_version": 1,
        "evidence_count": len(evidence),
        "counts_by_route": counts,
        "unresolved_count": sum(bool(x.get("uncertainty")) for x in evidence),
        "output": str(args.output),
        "content_included": False,
    }
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
