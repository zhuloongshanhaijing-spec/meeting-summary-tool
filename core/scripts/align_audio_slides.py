#!/usr/bin/env python3
"""Retrieve auditable audio-slide candidates without inventing correspondence."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compact(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", text or "").lower()


def ngrams(text: str, size: int = 2) -> set[str]:
    value = compact(text)
    if len(value) < size:
        return {value} if value else set()
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def similarity(left: str, right: str) -> float:
    a, b = ngrams(left), ngrams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--literal-record", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--minimum-score", type=float, default=0.03)
    args = parser.parse_args()

    evidence = read_jsonl(args.evidence)
    records = read_jsonl(args.literal_record)
    image_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        if item.get("kind") == "image":
            image_groups[item["source_id"]].append(item)
    relations: list[dict[str, Any]] = []
    linked_records: set[str] = set()
    for source_id, image_rows in sorted(image_groups.items()):
        slide_text = "\n".join(str(item.get("literal_text") or "") for item in image_rows)
        ranked = sorted(
            ((similarity(slide_text, row.get("clean_literal") or ""), row) for row in records),
            key=lambda pair: (-pair[0], pair[1]["record_id"]),
        )
        candidates = [
            {"record_id": row["record_id"], "lexical_score": round(score, 4)}
            for score, row in ranked[: args.top_k] if score >= args.minimum_score
        ]
        linked_records.update(item["record_id"] for item in candidates)
        relations.append({
            "relation_id": f"L{len(relations) + 1:06d}",
            "slide_source_id": source_id,
            "slide_evidence_ids": [item["evidence_id"] for item in image_rows],
            "candidate_audio_records": candidates,
            "relation": "unknown",
            "allowed_relations": ["exact", "outline", "audio_adds", "slide_adds", "related", "conflict", "unknown"],
            "decision_route": "lexical_retrieval_only",
            "requires_model_classification": True,
            "inference": None,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in relations), encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "status": "complete",
        "slide_count": len(image_groups),
        "relation_count": len(relations),
        "candidate_audio_record_count": len(linked_records),
        "relation_decisions_final": False,
        "content_included": False,
        "output": str(args.output),
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
