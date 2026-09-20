#!/usr/bin/env python3
"""Classify bounded note/audio candidates locally; never repair speech."""
import argparse
import atexit
import json
import urllib.request
from pathlib import Path

RELATIONS = ["supports", "partial_support", "conflict", "possible_related", "unknown"]


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def call(url: str, model: str, note: dict, candidates: list[dict]) -> dict:
    schema = {"type": "object", "properties": {"relation": {"type": "string", "enum": RELATIONS}, "matched_record_ids": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}, "conflict_fields": {"type": "array", "items": {"type": "string"}}}, "required": ["relation", "matched_record_ids", "reason", "conflict_fields"]}
    prompt = "你只判断一条用户笔记与候选录音逐句记录是否存在来源关系。笔记不是录音原话，绝对不得补全、改写或提高录音置信度。关系只能是 supports、partial_support、conflict、possible_related、unknown。录音未提到某笔记内容绝不等于conflict，必须选择unknown；只有候选录音明确表达相反事实时才可选择conflict。若选择conflict，matched_record_ids必须列出表达相反事实的录音段落，conflict_fields必须写出那一对相反事实。matched_record_ids只能来自候选。\n" + json.dumps({"note": note, "audio_candidates": candidates}, ensure_ascii=False)
    body = {"model": model, "stream": False, "think": False, "keep_alive": "5m", "format": schema, "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 384}, "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(3):
        try:
            request = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=600) as response:
                raw_response = json.load(response)
                content = (raw_response.get("message") or {}).get("content", "")
                return json.loads(content)
        except (json.JSONDecodeError, Exception) as e:
            if attempt == 2:
                # Final fallback: try without structured format
                body_no_format = dict(body)
                body_no_format.pop("format", None)
                body_no_format["options"]["num_predict"] = 256
                try:
                    request2 = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body_no_format).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request2, timeout=600) as resp2:
                        txt = (json.load(resp2).get("message") or {}).get("content", "")
                    # Extract relation from free text
                    for rel in RELATIONS:
                        if rel in txt:
                            return {"relation": rel, "matched_record_ids": [], "reason": f"fallback_free_text: {txt[:200]}", "conflict_fields": []}
                except Exception:
                    pass
                return {"relation": "unknown", "matched_record_ids": [], "reason": f"classification_parse_failed: {str(e)[:100]}", "conflict_fields": []}
            continue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--relations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    notes = {row["note_evidence_id"]: row for row in load(args.notes)}
    records = {row["record_id"]: row for row in load(args.records)}
    results = []
    for row in load(args.relations):
        candidate_ids = [item["record_id"] for item in row.get("candidate_audio_records", []) if item["record_id"] in records]
        candidates = [{"record_id": item, "clean_literal": records[item]["clean_literal"], "certainty": records[item]["certainty"], "uncertainty": records[item].get("uncertainty")} for item in candidate_ids]
        result = {"relation": "unknown", "matched_record_ids": [], "reason": "no_audio_candidate", "conflict_fields": []} if not candidates else call(args.ollama_url, args.model, notes[row["note_evidence_id"]], candidates)
        if result.get("relation") not in RELATIONS or not set(result.get("matched_record_ids", [])).issubset(candidate_ids):
            raise SystemExit(f"invalid local classification: {row['relation_id']}")
        if result["relation"] == "conflict" and not result.get("matched_record_ids"):
            result = {"relation": "unknown", "matched_record_ids": [], "reason": "no_explicit_opposite_audio_statement; absence is not conflict", "conflict_fields": []}
        results.append({**row, **result, "decision_route": f"ollama:{args.model}", "note_cannot_repair_audio": True})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
    args.receipt.write_text(json.dumps({"status": "complete", "relation_count": len(results), "model": args.model, "content_included": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    atexit.register(lambda: urllib.request.urlopen(urllib.request.Request(args.ollama_url.rstrip("/") + "/api/generate", data=json.dumps({"model": args.model, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"}, method="POST"), timeout=30).read())


if __name__ == "__main__":
    main()
