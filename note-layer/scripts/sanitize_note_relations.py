#!/usr/bin/env python3
"""Apply the deterministic absence-is-not-conflict safety invariant."""
import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    rows, downgraded = [], []
    for line in args.input.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("relation") == "conflict" and not row.get("matched_record_ids"):
            downgraded.append(row["relation_id"])
            row.update(relation="unknown", conflict_fields=[], reason="no_explicit_opposite_audio_statement; absence is not conflict", safety_normalized=True)
        rows.append(row)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.receipt.write_text(json.dumps({"status":"complete","input_relation_count":len(rows),"downgraded_conflict_ids":downgraded,"content_included":False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
