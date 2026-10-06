#!/usr/bin/env python3
"""Turn bounded Qwen route candidates into an auditable test transcript."""
import argparse
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qwen", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    args = parser.parse_args()
    payload = json.loads(args.qwen.read_text(encoding="utf-8"))
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for item in payload.get("items") or []:
        stem = Path(item["audio"]).stem
        source_id, route = stem.split("__", 1)
        grouped[source_id][route] = str(item.get("text") or "").strip()
    rows = []
    for ordinal, (source_id, routes) in enumerate(sorted(grouped.items()), start=1):
        preferred = routes.get("normalized") or next(iter(routes.values()), "")
        values = list(routes.values())
        pairs = [
            difflib.SequenceMatcher(None, compact(values[left]), compact(values[right]), autojunk=False).ratio()
            for left in range(len(values)) for right in range(left + 1, len(values))
        ]
        agreement = min(pairs) if pairs else 0.0
        uncertainty = None if agreement >= 0.93 else "acoustic_route_disagreement; note text must not repair this transcript"
        rows.append({
            "record_id": f"R{ordinal:06d}", "source_id": source_id,
            "start_seconds": args.start, "end_seconds": args.end,
            "clean_literal": preferred or "[听不清]", "raw_text": preferred or "[听不清]",
            "certainty": "medium" if uncertainty is None else "low",
            "uncertainty": uncertainty, "route_agreement_min": round(agreement, 4),
            "evidence_ids": [f"A{ordinal:06d}"],
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.receipt.write_text(json.dumps({"status": "complete", "record_count": len(rows), "content_included": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
