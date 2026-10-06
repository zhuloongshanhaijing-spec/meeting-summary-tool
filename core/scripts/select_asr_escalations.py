#!/usr/bin/env python3
"""Select Qwen windows that require independent ASR escalation."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
from pathlib import Path

from evaluate_asr_ensemble import ROUTES, compact, similarity


HIGH_RISK = re.compile(r"[A-Za-z0-9]|不要|不能|不得|必须|禁止|只有|除非|并非|不是|没有|日期|截止|分数|比例")


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qwen", required=True, type=Path)
    parser.add_argument("--window-receipt", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--min-route-similarity", type=float, default=0.95)
    parser.add_argument("--audit-percent", type=int, default=10)
    args = parser.parse_args()

    qwen = json.loads(args.qwen.read_text(encoding="utf-8"))
    if qwen.get("status") != "complete":
        raise SystemExit("Qwen candidate file is not complete")
    source_records = json.loads(args.window_receipt.read_text(encoding="utf-8"))["records"]
    source_by_name = {Path(item["output"]).name: Path(item["output"]) for item in source_records}
    grouped = {}
    for item in qwen["items"]:
        stem = Path(item["audio"]).stem
        window_id, route = stem.rsplit("__", 1)
        grouped.setdefault(window_id, {})[route] = item

    flat = args.output_dir / "flat"
    flat.mkdir(parents=True, exist_ok=True)
    decisions = []
    for window_id, rows in sorted(grouped.items()):
        if set(rows) != set(ROUTES):
            raise SystemExit(f"incomplete Qwen route set: {window_id}")
        texts = {route: rows[route].get("text", "") for route in ROUTES}
        pair_scores = [similarity(texts[a], texts[b]) for a, b in itertools.combinations(ROUTES, 2)]
        reasons = []
        if any(not compact(text) for text in texts.values()):
            reasons.append("empty_or_silence_candidate")
        if min(pair_scores) < args.min_route_similarity:
            reasons.append("route_disagreement")
        if any(HIGH_RISK.search(text) for text in texts.values()):
            reasons.append("high_risk_text")
        audit_bucket = int(hashlib.sha256(window_id.encode()).hexdigest()[:8], 16) % 100
        if audit_bucket < args.audit_percent:
            reasons.append("deterministic_quality_audit")
        escalate = bool(reasons)
        if escalate:
            for route in ROUTES:
                name = f"{window_id}__{route}.wav"
                source = source_by_name.get(name)
                if source is None:
                    raise SystemExit(f"window source missing from receipt: {name}")
                destination = flat / name
                if not destination.exists():
                    os.link(source, destination)
        decisions.append({
            "window_id": window_id,
            "escalate": escalate,
            "reasons": reasons,
            "minimum_qwen_route_similarity": round(min(pair_scores), 4),
            "mean_qwen_route_similarity": round(sum(pair_scores) / len(pair_scores), 4),
        })

    payload = {
        "schema_version": 1,
        "status": "complete",
        "policy": {
            "min_route_similarity": args.min_route_similarity,
            "audit_percent": args.audit_percent,
            "high_risk_pattern": HIGH_RISK.pattern,
            "one_engine_routes_are_not_independent_votes": True,
        },
        "window_count": len(decisions),
        "escalated_window_count": sum(item["escalate"] for item in decisions),
        "escalated_route_count": sum(item["escalate"] for item in decisions) * len(ROUTES),
        "decisions": decisions,
        "content_included": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt = args.output_dir / "escalation_receipt.json"
    atomic_json(receipt, payload)
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
