#!/usr/bin/env python3
"""Irrelevant-speech filter (staff logistics / greetings / AV chatter).

筛选逻辑 (how the filtering decides) — three layers, none destructive:

1. 词法候选层 (deterministic): each literal record is matched against
   lexicons for sound checks, scheduling, housekeeping, and pure
   acknowledgments. A hit only makes the record a *candidate*; it never
   excludes by itself. A content guard (digits, years, money, percentages,
   named entities) flags candidates that also carry substance.

2. LLM 复核层 (qwen3:8b, same Ollama infra as reconcile): candidates are
   batch-classified with neighboring context as content / logistics /
   uncertain, each with a reason. Known Ollama structured-output malformations
   are handled with retries and a free-text fallback (lesson from
   classify_note_links). If Ollama is unavailable, the LLM layer is skipped.

3. 判定层 (decision): a record is marked logistics ONLY when the lexical
   layer flagged it AND the LLM independently concurs (two-signal
   agreement). LLM-content overrides the lexical flag (false positives like
   "Okay, so the fee is three dollars" survive). Uncertainty never excludes.

绝不删除: the annotated records file keeps every record with a relevance
field {label, detector, rules, reason}. The only effect of a logistics
label is exclusion from the topic-reconcile *view* (units and the report);
the literal layer (02 逐句会议记录) and evidence.jsonl stay complete,
because 讲稿是基础. Language is never a criterion — mixed Chinese/English
speech is content, not contamination.

Receipt records per-label counts, fired rules, and LLM verdicts so every
exclusion is auditable and reversible by editing the relevance field.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LEXICONS: dict[str, list[str]] = {
    "lex.soundcheck": [
        r"\btesting\b", r"\btest[, ]+(one|two|mic|check|test)\b", r"\bone[- ]?two\b",
        r"sound check", r"can you hear", r"microphone", r"\bmic\b",
        r"试(一)?下麦", r"测试", r"喂[，,。 ]", r"能(听|聽)(到|清)", r"麦克风", r"声音(可以|行吗)",
    ],
    "lex.scheduling": [
        r"get started", r"we'?ll begin", r"in (about )?(five|5|two|a few) minutes",
        r"take your seats", r"running (slightly )?(behind|late)", r"short break",
        r"就座", r"请先坐好", r"先就座", r"五分钟后", r"稍等", r"签到",
        r"开始[之前以]?我们", r"时间关系", r"茶歇",
    ],
    "lex.housekeeping": [
        r"silence your phones", r"turn (off|on).{0,20}phone", r"name tags?",
        r"handout", r"sign[- ]in sheet", r"问卷", r"工牌", r"工作人员",
        r"拷贝.{0,12}(幻灯片|讲义|ppt)", r"举手登记", r"手机.{0,8}静音", r"静音",
        r"联系邮箱", r"门口",
    ],
    "lex.acknowledgment": [
        r"^(okay|ok|yeah|yes|right|sure|thanks|thank you|good|great)[.!, ]*$",
        r"^(好的|好|嗯|对|行|谢谢|感谢)[。！， ]*$",
    ],
}

CONTENT_GUARD = re.compile(
    r"(\d+\s?(%|percent|百分之)|\d{4}\s?(年|year)|[$€£]\s?\d"
    r"|\d+(\.\d+)?\s?(dollars?|pounds?|公里|万|亿|人次|条|座|美元|英镑|元|倍|个百分点)"
    r"|(singapore|london|beijing|shanghai|地铁|轨道|线网|客流|票价|模型|准确率|误差|corridor|revenue|ridership|congestion))",
    re.IGNORECASE,
)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def lexical_pass(records: list[dict]) -> dict[str, dict]:
    """Return {record_id: {rules: [...], has_content_guard: bool}} for candidates."""
    compiled = {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in LEXICONS.items()}
    candidates: dict[str, dict] = {}
    for rec in records:
        text = rec.get("clean_literal", "").strip()
        if not text:
            continue
        duration = max(rec.get("end_seconds", 0) - rec.get("start_seconds", 0), 0)
        fired = [name for name, pats in compiled.items() if any(p.search(text) for p in pats)]
        if "lex.acknowledgment" in fired and len(text) > 40:
            fired.remove("lex.acknowledgment")  # long utterances are not pure acks
        if fired:
            candidates[rec["record_id"]] = {
                "rules": sorted(fired),
                "short": duration < 2.5,
                "content_guard": bool(CONTENT_GUARD.search(text)),
            }
    return candidates


def call_ollama(url: str, model: str, prompt: str, keep_alive: str, temperature: float = 0.1,
                timeout: int = 120) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        # qwen3 is a thinking model: without think=false the reasoning can
        # exhaust num_predict and the visible response arrives empty.
        "think": False,
        "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": 768},
    }).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    raw = body.get("response", "")
    if not raw.strip():
        # qwen3 occasionally samples an empty generation; treat as retryable.
        raise RuntimeError("empty model response")
    return raw


def parse_verdicts(text: str, batch_ids: list[str]) -> dict[str, dict] | None:
    """Parse a JSON array of verdicts from (possibly chatty) model output."""
    # qwen3 is a thinking model: strip <think>...</think> blocks first so
    # brackets inside the reasoning cannot pollute the array match.
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = None
    for match in re.finditer(r"\[.*?\]", cleaned, re.DOTALL):
        try:
            items = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        verdicts = _valid_verdicts(items, batch_ids)
        if verdicts:
            return verdicts
    if match is None:
        return None
    # Fall back to greedy last array before giving up.
    greedy = re.findall(r"\[.*\]", cleaned, re.DOTALL)
    for chunk in reversed(greedy):
        try:
            return _valid_verdicts(json.loads(chunk), batch_ids)
        except json.JSONDecodeError:
            continue
    return None


def _valid_verdicts(items: object, batch_ids: list[str]) -> dict[str, dict] | None:
    verdicts = {}
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("record_id", ""))
        label = str(item.get("label", "")).lower()
        if rid in batch_ids and label in {"content", "logistics", "uncertain"}:
            verdicts[rid] = {"label": label, "reason": str(item.get("reason", ""))[:200]}
    return verdicts if verdicts else None


def llm_pass(candidates: dict[str, dict], records: list[dict], url: str, model: str,
             keep_alive: str) -> tuple[dict[str, dict], dict]:
    """Adjudicate candidates with neighborhood context; robust to malformed JSON."""
    by_id = {r["record_id"]: r for r in records}
    order = [r["record_id"] for r in records]
    verdicts: dict[str, dict] = {}
    stats = {"calls": 0, "parse_failures": 0, "batches": 0}
    ids = [rid for rid in candidates if rid in by_id]
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        stats["batches"] += 1
        lines = []
        for rid in batch:
            idx = order.index(rid)
            prev_text = by_id[order[idx - 1]].get("clean_literal", "")[-120:] if idx > 0 else ""
            next_text = by_id[order[idx + 1]].get("clean_literal", "")[:120] if idx + 1 < len(order) else ""
            lines.append({
                "record_id": rid,
                "previous": prev_text, "utterance": by_id[rid].get("clean_literal", ""), "next": next_text,
                "lexicon_hits": candidates[rid]["rules"],
            })
        prompt = (
            "You are auditing a lecture transcript. For each utterance below, decide whether it is\n"
            "content (carries subject matter: facts, claims, reasoning, examples),\n"
            "logistics (staff coordination: sound checks, scheduling, seating, handouts, greetings,\n"
            "device/appliance talk), or uncertain. Mixed Chinese/English speech is normal content,\n"
            "never logistics by itself. Answer ONLY a JSON array like\n"
            '[{"record_id": "...", "label": "content|logistics|uncertain", "reason": "short"}].\n\n'
            + json.dumps(lines, ensure_ascii=False)
            + "\n/no_think"  # qwen3 soft switch: skip reasoning, emit the array directly
        )
        # qwen3 sometimes clusters degenerate empty generations at one
        # temperature; rotating the sampler temperature escapes the attractor.
        for attempt, temperature in enumerate((0.1, 0.7, 0.3, 0.5, 0.9)):
            try:
                stats["calls"] += 1
                raw = call_ollama(url, model, prompt, keep_alive, temperature)
                parsed = parse_verdicts(raw, batch)
                if parsed:
                    verdicts.update(parsed)
                    break
                stats["parse_failures"] += 1
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError):
                stats["parse_failures"] += 1
                time.sleep(2)
        # Unadjudicated ids simply stay unmarked; uncertainty never excludes.
    return verdicts, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="annotate literal records with relevance labels")
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--records-out", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--evidence-for-reconcile", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--keep-alive", default="5m")
    parser.add_argument("--no-llm", action="store_true", help="lexical annotation only; nothing is excluded")
    args = parser.parse_args()

    records = load_jsonl(args.records)
    evidence = load_jsonl(args.evidence)
    candidates = lexical_pass(records)

    verdicts, llm_stats = {}, {"calls": 0, "parse_failures": 0, "batches": 0}
    if not args.no_llm and candidates:
        verdicts, llm_stats = llm_pass(candidates, records, args.ollama_url, args.model, args.keep_alive)

    excluded: set[str] = set()
    annotated = []
    for rec in records:
        rid = rec["record_id"]
        if rid not in candidates:
            annotated.append({**rec, "relevance": {"label": "content", "detector": "default", "rules": [], "reason": ""}})
            continue
        cand = candidates[rid]
        verdict = verdicts.get(rid)
        if args.no_llm or verdict is None:
            annotated.append({**rec, "relevance": {
                "label": "uncertain", "detector": "lex-only" if args.no_llm else "lex+llm-unparsed",
                "rules": cand["rules"], "reason": "lexical candidate without confirming LLM verdict; never excluded",
            }})
            continue
        label = verdict["label"]
        # Two-signal agreement: exclude only when both layers say logistics.
        if label == "logistics" and not cand["content_guard"]:
            excluded.add(rid)
        annotated.append({**rec, "relevance": {
            "label": label,
            "detector": "lex+llm",
            "rules": cand["rules"],
            "reason": verdict["reason"],
        }})

    write_jsonl(args.records_out, annotated)
    reconcile_view = [e for e in evidence if
                      e["evidence_id"] not in {r for rec in annotated if rec["record_id"] in excluded
                                               for r in rec.get("evidence_ids", [])}]
    write_jsonl(args.evidence_for_reconcile, reconcile_view)

    labels: dict[str, int] = {}
    for rec in annotated:
        lbl = rec["relevance"]["label"]
        labels[lbl] = labels.get(lbl, 0) + 1
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({
        "schema_version": 1,
        "records_total": len(annotated),
        "labels": labels,
        "lexical_candidates": len(candidates),
        "excluded_from_reconcile": len(excluded),
        "excluded_ids": sorted(excluded),
        "llm": llm_stats,
        "invariants": {
            "literal_layer_complete": len(annotated) == len(records),
            "evidence_layer_complete": len(evidence),
            "reconcile_view_size": len(reconcile_view),
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
