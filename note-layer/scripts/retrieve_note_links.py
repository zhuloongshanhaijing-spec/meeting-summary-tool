#!/usr/bin/env python3
"""Deterministically retrieve note-to-audio candidates without deciding facts."""
import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def call_ollama(url: str, model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "keep_alive": "5m",
        "think": False,  # qwen3: reasoning can exhaust num_predict -> empty body
        "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 768},
    }).encode("utf-8")
    request = urllib.request.Request(url.rstrip("/") + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode("utf-8"))
    raw = body.get("response", "")
    if not raw.strip():
        raise RuntimeError("empty model response")
    return raw


def parse_picks(text: str, note_ids: list[str]) -> dict[str, list[str]] | None:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    for chunk in re.findall(r"\[.*?\]", cleaned, re.DOTALL) + re.findall(r"\[.*\]", cleaned, re.DOTALL):
        try:
            items = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        picks: dict[str, list[str]] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                nid = str(item.get("note_evidence_id", ""))
                rids = [str(r) for r in item.get("record_ids", []) if isinstance(r, (str, int))]
                if nid in note_ids and rids:
                    picks.setdefault(nid, []).extend(rids[:3])
        if picks:
            return picks
    return None


def grams(text: str) -> set[str]:
    value = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", text or "").lower()
    return {value[index:index + 2] for index in range(max(0, len(value) - 1))} or ({value} if value else set())


def score(left: str, right: str) -> float:
    a, b = grams(left), grams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def containment(left: str, right: str) -> float:
    """Score a short note against a long transcript without length dilution."""
    a, b = grams(left), grams(right)
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--minimum-score", type=float, default=0.03)
    parser.add_argument("--document-minimum-score", type=float, default=0.02)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--no-llm", action="store_true", help="disable cross-lingual LLM fallback")
    parser.add_argument("--llm-max-records", type=int, default=120,
                        help="records per LLM fallback batch; larger transcripts are skipped")
    args = parser.parse_args()
    notes, records = load(args.notes), load(args.records)
    by_source = {}
    for note in notes:
        by_source.setdefault(note["source_id"], []).append(note)
    eligible_sources: dict[str, set[str]] = {record["record_id"]: set() for record in records}
    document_candidates: dict[str, list[dict]] = {}
    for record in records:
        ranked = []
        for source_id, source_notes in by_source.items():
            path = source_notes[0]["locator"]["path"]
            topic_hint = path.replace(".md", "").replace("/", " ")
            document_text = topic_hint + " " + " ".join(item["literal_text"] for item in source_notes)
            ranked.append((score(document_text, record.get("clean_literal", "")), source_id, topic_hint))
        ranked.sort(reverse=True)
        document_candidates[record["record_id"]] = [
            {"source_id": source_id, "topic_hint": hint, "lexical_score": round(value, 4)}
            for value, source_id, hint in ranked[:args.top_k]
        ]
        if ranked and ranked[0][0] >= args.document_minimum_score:
            eligible_sources[record["record_id"]].add(ranked[0][1])
    rows = []
    for note in notes:
        ranked = sorted(
            ((containment(note["literal_text"], row.get("clean_literal", "")), row) for row in records if note["source_id"] in eligible_sources[row["record_id"]]),
            key=lambda pair: (-pair[0], pair[1]["record_id"]),
        )
        candidates = [{"record_id": row["record_id"], "lexical_score": round(value, 4)} for value, row in ranked[:args.top_k] if value >= args.minimum_score]
        rows.append({"relation_id": f"NLR{len(rows) + 1:06d}", "note_evidence_id": note["note_evidence_id"],
                     "candidate_audio_records": candidates, "relation": "unknown", "decision_route": "lexical_retrieval_only",
                     "allowed_relations": ["supports", "partial_support", "conflict", "possible_related", "unknown"],
                     "document_candidates": document_candidates})
    # Cross-lingual fallback: zh notes vs en transcripts share no bigrams,
    # so lexical retrieval returns nothing. The LLM only nominates candidate
    # record_ids; relation decisions stay with classify_note_links.
    llm_stats = {"calls": 0, "failures": 0, "notes_helped": 0, "skipped_reason": None}
    weak = [row for row in rows if not row["candidate_audio_records"]]
    by_note_id = {note["note_evidence_id"]: note for note in notes}
    valid_ids = {row["record_id"] for row in records}
    if weak and not args.no_llm:
        if len(records) > args.llm_max_records:
            llm_stats["skipped_reason"] = f"{len(records)} records exceed llm-max-records"
        else:
            note_block = json.dumps([
                {"note_evidence_id": row["note_evidence_id"],
                 "text": by_note_id[row["note_evidence_id"]]["literal_text"][:160]}
                for row in weak], ensure_ascii=False)
            record_block = json.dumps([
                {"record_id": row["record_id"], "text": row.get("clean_literal", "")[:110]}
                for row in records], ensure_ascii=False)
            prompt = (
                "A meeting transcript and researcher notes in different languages.\n"
                "For each note, pick up to 3 record_ids whose spoken content is about the same "
                "subject (translation is expected; numbers/dates are strong anchors).\n"
                "Answer ONLY a JSON array like "
                '[{"note_evidence_id": "...", "record_ids": ["R000123"]}]. '
                "Use [] record_ids when nothing matches.\n\n"
                f"NOTES:\n{note_block}\n\nRECORDS:\n{record_block}"
            )
            for temperature in (0.1, 0.7, 0.4):
                try:
                    llm_stats["calls"] += 1
                    picks = parse_picks(call_ollama(args.ollama_url, args.model, prompt),
                                        [row["note_evidence_id"] for row in weak])
                except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError):
                    llm_stats["failures"] += 1
                    time.sleep(2)
                    continue
                if picks:
                    for row in rows:
                        chosen = [rid for rid in picks.get(row["note_evidence_id"], []) if rid in valid_ids]
                        if chosen:
                            row["candidate_audio_records"] = [
                                {"record_id": rid, "lexical_score": None, "source": "llm_cross_lingual"}
                                for rid in chosen[:3]]
                            row["decision_route"] = "lexical_retrieval_plus_llm_cross_lingual"
                            llm_stats["notes_helped"] += 1
                    break

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.receipt.write_text(json.dumps({"status": "complete", "relation_count": len(rows), "final_decisions": False, "content_included": False, "llm_cross_lingual": llm_stats}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
