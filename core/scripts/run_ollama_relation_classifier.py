#!/usr/bin/env python3
"""Classify bounded audio-slide candidate relations with a local text model."""

from __future__ import annotations

import argparse
import atexit
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


RELATIONS = ["exact", "outline", "audio_adds", "slide_adds", "related", "conflict", "unknown"]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def unload(url: str, model: str) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=60).read()
    except OSError:
        pass


def classify(url: str, model: str, slide_rows: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "relation": {"type": "string", "enum": RELATIONS},
            "matched_record_ids": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "conflict_fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["relation", "matched_record_ids", "reason", "conflict_fields"],
    }
    prompt = (
        "你只判断PPT可见文字与候选语音逐句记录的来源关系，不得补充常识，不得把PPT内容冒充老师原话。"
        "关系只能是 exact、outline、audio_adds、slide_adds、related、conflict、unknown。"
        "matched_record_ids只能来自候选。数字、日期、否定、条件不同应标为conflict或unknown。\n"
        + json.dumps({"slide_evidence": slide_rows, "audio_candidates": candidates}, ensure_ascii=False)
    )
    body = {
        "model": model, "stream": False, "think": False, "keep_alive": "5m", "format": schema,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 512},
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            envelope = json.load(response)
        return json.loads(((envelope.get("message") or {}).get("content") or "").strip())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"relation classification failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--literal-record", required=True, type=Path)
    parser.add_argument("--relations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--checkpoint-dir", required=True, type=Path)
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    atexit.register(unload, args.ollama_url, args.model)
    evidence = {item["evidence_id"]: item for item in read_jsonl(args.evidence)}
    records = {item["record_id"]: item for item in read_jsonl(args.literal_record)}
    relations = read_jsonl(args.relations)
    args.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    output_rows = []
    unknown_count = 0
    for relation in relations:
        checkpoint = args.checkpoint_dir / f"{relation['relation_id']}.json"
        result = None
        if args.resume and checkpoint.is_file():
            try:
                result = json.loads(checkpoint.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result = None
        candidate_ids = [item["record_id"] for item in relation.get("candidate_audio_records") or []]
        if result is None:
            slide_rows = [
                {"evidence_id": evidence_id, "visible_text": evidence[evidence_id].get("literal_text"), "uncertainty": evidence[evidence_id].get("uncertainty")}
                for evidence_id in relation.get("slide_evidence_ids") or []
            ]
            candidates = [
                {"record_id": record_id, "clean_literal": records[record_id].get("clean_literal"), "certainty": records[record_id].get("certainty")}
                for record_id in candidate_ids if record_id in records
            ]
            if not candidates:
                result = {"relation": "unknown", "matched_record_ids": [], "reason": "no_audio_candidate", "conflict_fields": []}
            else:
                result = classify(args.ollama_url, args.model, slide_rows, candidates)
            checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if result.get("relation") not in RELATIONS or not set(result.get("matched_record_ids") or []).issubset(candidate_ids):
            raise SystemExit(f"invalid relation result: {relation['relation_id']}")
        row = dict(relation)
        row.update(
            relation=result["relation"], matched_audio_records=result.get("matched_record_ids") or [],
            decision_route=f"ollama:{args.model}", decision_reason=result.get("reason"), conflict_fields=result.get("conflict_fields") or [],
            requires_model_classification=False,
        )
        if row["relation"] == "unknown":
            unknown_count += 1
        output_rows.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output_rows), encoding="utf-8")
    receipt = {
        "schema_version": 3, "status": "complete", "model": args.model, "relation_count": len(output_rows),
        "unknown_count": unknown_count, "checkpoint_dir": str(args.checkpoint_dir), "content_included": False,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
