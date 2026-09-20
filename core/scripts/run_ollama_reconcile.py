#!/usr/bin/env python3
"""Extract information units locally from bounded evidence chunks via Ollama."""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "topic_path": {"type": "array", "items": {"type": "string"}},
                    "conditions": {"type": "array", "items": {"type": "string"}},
                    "exceptions": {"type": "array", "items": {"type": "string"}},
                    "examples": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "certainty": {"type": "string", "enum": ["high", "medium", "low"]},
                    "source_relation": {"type": "string"},
                },
                "required": ["claim", "topic_path", "conditions", "exceptions", "examples", "evidence_ids", "certainty", "source_relation"],
            },
        },
        "dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["covered", "duplicate", "nonsemantic", "uncertain", "conflict"]},
                    "reason": {"type": "string"},
                },
                "required": ["evidence_id", "status", "reason"],
            },
        },
    },
    "required": ["units", "dispositions"],
}


def call_ollama(
    url: str,
    model: str,
    evidence: list[dict[str, Any]],
    keep_alive: str,
    num_ctx: int,
    num_predict: int,
    coverage_policy: str,
) -> dict[str, Any]:
    coverage_instruction = (
        "每条证据都必须在 dispositions 中恰好出现一次。"
        if coverage_policy == "strict"
        else "不要输出 dispositions；控制器会把所有证据保守登记并保留原文。"
    )
    prompt = (
        "你是会议证据整理器。只依据下列证据建立信息单元，不得补写常识或猜测模糊内容。"
        "删除口头填充词和完全重复，但必须保留数字、日期、姓名、否定、条件、例外、步骤、枚举及有规则意义的例子。"
        + coverage_instruction + "冲突或模糊内容不要强行合并。"
        "每块最多输出4个导航信息单元；将同一事项的连续证据合并，但不要删掉条件、例外、步骤和枚举。"
        "claim 使用清晰完整的中文改写；evidence_ids 必须来自输入。\nEVIDENCE_JSON:\n"
        + json.dumps(evidence, ensure_ascii=False)
    )
    output_schema = SCHEMA
    if coverage_policy == "conservative-fill":
        output_schema = {
            "type": "object",
            "properties": {"units": SCHEMA["properties"]["units"]},
            "required": ["units"],
        }
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "format": output_schema,
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1200) as response:
            envelope = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama reconciliation failed: {exc}") from exc
    content = ((envelope.get("message") or {}).get("content") or "").strip()
    return json.loads(content)


def unload_model(url: str, model: str) -> None:
    payload = {"model": model, "keep_alive": 0}
    request = urllib.request.Request(
        url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120):
            pass
    except OSError:
        pass


def _chunk_fingerprint(chunk: list[dict]) -> str:
    """Stable hash of a chunk's evidence content (ids AND text).

    Checkpoints are keyed by chunk index; without a content binding a
    re-run whose evidence text changed silently reuses stale results.
    """
    import hashlib
    digest = hashlib.sha256()
    for item in sorted(chunk, key=lambda x: str(x.get("evidence_id"))):
        digest.update(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def validate_model_result(result: Any, chunk_ids: set[str]) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("units"), list):
        raise ValueError("result must contain a units array")
    for unit in result["units"]:
        refs = unit.get("evidence_ids") or []
        if not refs or any(ref not in chunk_ids for ref in refs):
            raise ValueError("unit contains missing or out-of-chunk evidence IDs")
    for disposition in result.get("dispositions") or []:
        if disposition.get("evidence_id") not in chunk_ids:
            raise ValueError("disposition contains an out-of-chunk evidence ID")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--keep-alive", default="5m")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--num-predict", type=int, default=8192)
    parser.add_argument("--chunk-size", type=int, default=80)
    parser.add_argument("--min-chunk-size", type=int, default=40)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument(
        "--coverage-policy",
        choices=("strict", "conservative-fill"),
        default="strict",
        help="conservative-fill marks omitted evidence uncertain instead of dropping it",
    )
    args = parser.parse_args()
    atexit.register(unload_model, args.ollama_url, args.model)

    evidence = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    valid_ids = {x["evidence_id"] for x in evidence}
    units: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    auto_filled_count = 0
    processed_chunk_count = 0
    checkpoint_dir = args.checkpoint_dir or (args.output.parent / f"{args.output.stem}.chunks")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    failure_log = checkpoint_dir / "failures.jsonl"
    start = 0
    while start < len(evidence):
        existing = sorted(checkpoint_dir.glob(f"chunk-{start:06d}-*.json"), reverse=True)
        checkpoint = existing[0] if existing else None
        result = None
        if checkpoint:
            chunk = None
            try:
                end = int(checkpoint.stem.rsplit("-", 1)[1])
                if end < start or end >= len(evidence):
                    raise ValueError("checkpoint range beyond current evidence")
                chunk = evidence[start : end + 1]
                payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                result = payload["result"]
                if payload.get("_fingerprint") != _chunk_fingerprint(chunk):
                    raise KeyError("stale checkpoint: evidence content changed")
                validate_model_result(result, {x["evidence_id"] for x in chunk})
            except (json.JSONDecodeError, KeyError, ValueError, RuntimeError):
                # stale/corrupt/out-of-range checkpoint (e.g. left by a longer
                # prior run): discard it — and any same-start siblings — and
                # reconcile fresh; never hard-abort on our own stale state
                for stale in checkpoint_dir.glob(f"chunk-{start:06d}-*.json"):
                    stale.unlink()
                checkpoint = None
                result = None
        if checkpoint is None:
            candidate_size = min(args.chunk_size, len(evidence) - start)
            result = None
            while result is None:
                chunk = evidence[start : start + candidate_size]
                end = start + len(chunk) - 1
                chunk_ids = {x["evidence_id"] for x in chunk}
                last_error: Exception = RuntimeError("no attempt made")
                for attempt in range(1, args.max_attempts + 1):
                    try:
                        candidate = call_ollama(
                            args.ollama_url, args.model, chunk, args.keep_alive,
                            args.num_ctx, args.num_predict, args.coverage_policy,
                        )
                        validate_model_result(candidate, chunk_ids)
                        result = candidate
                        break
                    except (RuntimeError, json.JSONDecodeError, ValueError) as exc:
                        last_error = exc
                        with failure_log.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({
                                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "start": start,
                                "end": end,
                                "size": len(chunk),
                                "attempt": attempt,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:300],
                                "content_included": False,
                            }, ensure_ascii=False) + "\n")
                if result is None:
                    if candidate_size <= args.min_chunk_size:
                        raise SystemExit(
                            f"chunk {start}-{end} failed after retries at minimum size: {last_error}"
                        )
                    candidate_size = max(args.min_chunk_size, candidate_size // 2)
            checkpoint = checkpoint_dir / f"chunk-{start:06d}-{end:06d}.json"
            temporary = checkpoint.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(
                {"_fingerprint": _chunk_fingerprint(chunk), "result": result},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(checkpoint)
        chunk_ids = {x["evidence_id"] for x in chunk}
        for unit in result.get("units") or []:
            units.append(unit)
        chunk_dispositions = result.get("dispositions") or []
        deduplicated = {}
        for disposition in chunk_dispositions:
            evidence_id = disposition.get("evidence_id")
            deduplicated.setdefault(evidence_id, disposition)
        missing = chunk_ids - set(deduplicated)
        if missing and args.coverage_policy == "strict":
            raise SystemExit("model did not account for every evidence ID exactly once")
        for evidence_id in sorted(missing):
            deduplicated[evidence_id] = {
                "evidence_id": evidence_id,
                "status": "uncertain",
                "reason": "local extractor omitted disposition; preserved in literal evidence appendix",
            }
            auto_filled_count += 1
        chunk_dispositions = list(deduplicated.values())
        dispositions.extend(chunk_dispositions)
        processed_chunk_count += 1
        start = end + 1

    if {x["evidence_id"] for x in dispositions} != valid_ids:
        raise SystemExit("global evidence coverage mismatch")
    for ordinal, unit in enumerate(units, start=1):
        unit["unit_id"] = f"U{ordinal:06d}"
    referenced_ids = {ref for unit in units for ref in unit.get("evidence_ids") or []}
    promoted_covered_count = 0
    for disposition in dispositions:
        if (
            disposition["evidence_id"] in referenced_ids
            and disposition.get("status") == "uncertain"
            and str(disposition.get("reason", "")).startswith("local extractor omitted disposition")
        ):
            disposition["status"] = "covered"
            disposition["reason"] = "linked by a validated navigation information unit"
            promoted_covered_count += 1
    result = {"schema_version": 1, "model": args.model, "units": units, "dispositions": dispositions}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "model": args.model,
        "evidence_count": len(evidence),
        "unit_count": len(units),
        "disposition_count": len(dispositions),
        "chunk_count": processed_chunk_count,
        "coverage_policy": args.coverage_policy,
        "auto_filled_disposition_count": auto_filled_count,
        "promoted_covered_count": promoted_covered_count,
        "checkpoint_dir": str(checkpoint_dir),
        "content_included": False,
        "output": str(args.output),
    }
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
