#!/usr/bin/env python3
"""Select acoustic routes by cross-engine agreement and expose uncertainty.

Each ASR engine gets one vote. Multiple preprocessing routes from the same
engine are alternatives, not independent votes. No language model is allowed
to manufacture a confidence score.
"""

from __future__ import annotations

import argparse
import difflib
import itertools
import json
import unicodedata
from pathlib import Path


ROUTES = ("original", "normalized", "impulse_noise_reduced")


def compact(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch.isalnum() or "\u3400" <= ch <= "\u9fff")


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, lch in enumerate(left, 1):
        current = [row]
        for col, rch in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[col] + 1, previous[col - 1] + (lch != rch)))
        previous = current
    return previous[-1]


def similarity(left: str, right: str) -> float:
    left, right = compact(left), compact(right)
    return 1.0 - edit_distance(left, right) / max(len(left), len(right), 1)


def load_qwen(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {Path(item["audio"]).stem: item.get("text", "") for item in payload["items"]}


def nested_text(value) -> str:
    if isinstance(value, dict):
        here = value.get("text", "") if isinstance(value.get("text"), str) else ""
        return here + "".join(nested_text(v) for k, v in value.items() if k != "text")
    if isinstance(value, list):
        return "".join(nested_text(v) for v in value)
    return ""


def load_funasr(directory: Path) -> dict[str, str]:
    rows = {}
    for path in directory.glob("*.json"):
        if not path.name.endswith("receipt.json"):
            rows[path.stem] = nested_text(json.loads(path.read_text(encoding="utf-8")))
    return rows


def load_whisper(directory: Path) -> dict[str, str]:
    rows = {}
    for path in directory.glob("*.json"):
        if path.name.endswith("receipt.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows[path.stem] = "".join(item.get("text", "") for item in payload.get("transcription", []))
    return rows


def routes_for_clip(rows: dict[str, str], clip_id: str | None) -> dict[str, str]:
    if clip_id is None:
        return rows
    prefix = f"{clip_id}__"
    return {key[len(prefix):]: value for key, value in rows.items() if key.startswith(prefix)}


def support_metrics(base: str, others: list[str]) -> dict:
    normalized_base = compact(base)
    support = [1] * len(normalized_base)
    insertion_characters = 0
    for other in others:
        normalized_other = compact(other)
        matcher = difflib.SequenceMatcher(None, normalized_base, normalized_other, autojunk=False)
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            if tag == "equal":
                for index in range(a0, a1):
                    support[index] += 1
            if tag in ("insert", "replace") and (b1 - b0) > (a1 - a0):
                insertion_characters += (b1 - b0) - (a1 - a0)
    length = max(len(normalized_base), 1)
    return {
        "base_character_count": len(normalized_base),
        "supported_by_two_ratio": round(sum(value >= 2 for value in support) / length, 4),
        "supported_by_three_ratio": round(sum(value >= 3 for value in support) / length, 4),
        "possible_missing_character_count": insertion_characters,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qwen", required=True, type=Path)
    parser.add_argument("--funasr-dir", required=True, type=Path)
    parser.add_argument("--whisper-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--specs", type=Path)
    parser.add_argument("--clip-id")
    args = parser.parse_args()

    engines = {
        "qwen3_asr": routes_for_clip(load_qwen(args.qwen), args.clip_id),
        "paraformer": routes_for_clip(load_funasr(args.funasr_dir), args.clip_id),
        "whisper_large_v3": routes_for_clip(load_whisper(args.whisper_dir), args.clip_id),
    }
    missing = {engine: sorted(set(ROUTES) - set(rows)) for engine, rows in engines.items()}
    if any(missing.values()):
        raise SystemExit(f"missing route candidates: {missing}")

    best = None
    for route_tuple in itertools.product(ROUTES, repeat=len(engines)):
        selected = {engine: engines[engine][route] for engine, route in zip(engines, route_tuple)}
        pairs = {
            f"{left}__{right}": similarity(selected[left], selected[right])
            for left, right in itertools.combinations(selected, 2)
        }
        score = sum(pairs.values()) / len(pairs)
        candidate = (score, route_tuple, selected, pairs)
        if best is None or candidate[0] > best[0]:
            best = candidate

    score, route_tuple, selected, pairs = best
    mean_by_engine = {
        engine: sum(value for key, value in pairs.items() if engine in key) / 2
        for engine in engines
    }
    medoid_engine = max(mean_by_engine, key=mean_by_engine.get)
    metrics = support_metrics(selected[medoid_engine], [text for engine, text in selected.items() if engine != medoid_engine])

    specs = json.loads(args.specs.read_text(encoding="utf-8")) if args.specs else {}
    clip_spec = next((item for item in specs.get("clips", []) if item.get("clip_id") == args.clip_id), {})
    regression_spec = clip_spec or specs
    forbidden = regression_spec.get("forbidden_terms", [])
    expected = regression_spec.get("expected_terms", [])
    forbidden_hits = {
        term: [engine for engine, text in selected.items() if compact(term) in compact(text)]
        for term in forbidden
    }
    expected_hits = {
        term: [engine for engine, text in selected.items() if compact(term) in compact(text)]
        for term in expected
    }
    min_expected = int(regression_spec.get("min_engines_for_expected_term", 2))
    regression_pass = not any(forbidden_hits.values()) and all(len(hits) >= min_expected for hits in expected_hits.values())

    gate_pass = score >= 0.72 and metrics["supported_by_two_ratio"] >= 0.82 and regression_pass
    payload = {
        "schema_version": 1,
        "method": "one-route-per-engine exhaustive selection by mean pairwise normalized edit similarity",
        "clip_id": args.clip_id,
        "confidence_source": "measured cross-engine character agreement; no LLM confidence",
        "selected_routes": dict(zip(engines, route_tuple)),
        "mean_pairwise_similarity": round(score, 4),
        "pairwise_similarity": {key: round(value, 4) for key, value in pairs.items()},
        "medoid_engine": medoid_engine,
        "medoid_mean_similarity": round(mean_by_engine[medoid_engine], 4),
        "support": metrics,
        "regression": {
            "forbidden_term_hits": forbidden_hits,
            "expected_term_hits": expected_hits,
            "min_engines_for_expected_term": min_expected,
            "pass": regression_pass,
        },
        "gate": {
            "mean_pairwise_similarity_min": 0.72,
            "supported_by_two_ratio_min": 0.82,
            "pass": gate_pass,
        },
        "selected_candidates": selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if gate_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
