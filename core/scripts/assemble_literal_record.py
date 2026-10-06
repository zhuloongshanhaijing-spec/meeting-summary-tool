#!/usr/bin/env python3
"""Build a chronological, minimally edited literal record from ASR evidence.

This stage is deliberately not a summarizer. It keeps the selected ASR text,
removes only deterministic overlap/filler noise, records every edit, and keeps
window-level timestamps until an optional forced aligner supplies finer ones.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


FILLER_PREFIX = re.compile(
    r"^(?:(?:嗯+|啊+|呃+|唉+|这个(?:呢)?|那个(?:呢)?|就是说|然后呢|那么呢)[，,、。.!！?？；;：:\s]+)+"
)
SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compact_with_map(text: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isspace() or char in "，。！？；：、,.!?;:（）()【】[]\"'“”‘’":
            continue
        compact.append(char)
        positions.append(index)
    return "".join(compact), positions


def exact_overlap_cut(previous: str, current: str, maximum: int = 80) -> tuple[int, int]:
    """Return (original-char cut, compact overlap); never fuzzy-delete speech."""
    left, _ = compact_with_map(previous)
    right, right_positions = compact_with_map(current)
    upper = min(maximum, len(left), len(right))
    for size in range(upper, 3, -1):
        if left[-size:] == right[:size]:
            return right_positions[size - 1] + 1, size
    return 0, 0


def split_sentences(text: str) -> list[str]:
    pieces = [piece.strip() for piece in SENTENCE_END.split(text) if piece.strip()]
    return pieces or ([text.strip()] if text.strip() else [])


def minimal_clean(text: str) -> tuple[str, list[dict[str, Any]]]:
    edits: list[dict[str, Any]] = []
    clean = re.sub(r"\s+", " ", text).strip()
    if clean != text.strip():
        edits.append({"type": "whitespace_normalization"})
    matched = FILLER_PREFIX.match(clean)
    if matched:
        removed = matched.group(0)
        clean = clean[matched.end():].lstrip()
        edits.append({"type": "leading_filler_removed", "removed_text": removed})
    return clean or "[听不清]", edits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()

    evidence = read_jsonl(args.evidence)
    audio = [item for item in evidence if item.get("kind") == "audio"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in audio:
        grouped[item["source_id"]].append(item)
    records: list[dict[str, Any]] = []
    accounted: set[str] = set()
    uncertain_join_count = 0
    exact_overlap_count = 0

    for source_id, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: (
            float((item.get("locator") or {}).get("start_seconds") or 0),
            float((item.get("locator") or {}).get("end_seconds") or 0),
            item["evidence_id"],
        ))
        previous_raw = ""
        for row in rows:
            locator = row.get("locator") or {}
            raw = str(row.get("literal_text") or "").strip() or "[听不清]"
            cut, overlap = exact_overlap_cut(previous_raw, raw)
            window_edits: list[dict[str, Any]] = []
            if cut:
                window_edits.append({
                    "type": "exact_window_overlap_removed",
                    "removed_text": raw[:cut],
                    "matched_compact_characters": overlap,
                })
                raw_for_split = raw[cut:].lstrip(" ，,。；;")
                exact_overlap_count += 1
            else:
                raw_for_split = raw
                if previous_raw:
                    window_edits.append({"type": "overlap_not_deleted_without_exact_support"})
                    uncertain_join_count += 1
            sentences = split_sentences(raw_for_split)
            if not sentences:
                sentences = ["[听不清]"]
            for sentence_index, sentence in enumerate(sentences, start=1):
                clean, edits = minimal_clean(sentence)
                certainty = (row.get("confidence") or {}).get("quality") or "unrated"
                if row.get("uncertainty") or clean == "[听不清]":
                    certainty = "low"
                records.append({
                    "record_id": f"R{len(records) + 1:06d}",
                    "source_id": source_id,
                    "source_type": "audio_spoken",
                    "speaker": "unknown",
                    "start_seconds": locator.get("start_seconds"),
                    "end_seconds": locator.get("end_seconds"),
                    "time_precision": "asr_window",
                    "sentence_index_in_window": sentence_index,
                    "raw_text": sentence,
                    "clean_literal": clean,
                    "evidence_ids": [row["evidence_id"]],
                    "slide_refs": [],
                    "edits": window_edits + edits if sentence_index == 1 else edits,
                    "certainty": certainty,
                    "uncertainty": row.get("uncertainty"),
                })
            accounted.add(row["evidence_id"])
            previous_raw = raw

    expected = {item["evidence_id"] for item in audio}
    if accounted != expected:
        raise SystemExit("literal record does not account for every audio evidence object")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    temporary.replace(args.output)
    receipt = {
        "schema_version": 1,
        "status": "complete",
        "audio_evidence_count": len(audio),
        "accounted_audio_evidence_count": len(accounted),
        "literal_record_count": len(records),
        "exact_overlap_removal_count": exact_overlap_count,
        "uncertain_window_join_count": uncertain_join_count,
        "time_precision": "asr_window",
        "semantic_rewriting": False,
        "content_included": False,
        "output": str(args.output),
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
