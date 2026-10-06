#!/usr/bin/env python3
"""Consolidate windowed ASR candidates into one evidence row per window.

Qwen acoustic routes are correlated alternatives.  Escalated windows select
one route per independent engine by measured cross-engine agreement; other
windows select the Qwen route medoid.  The script never asks an LLM to guess
speech and never exposes transcript content in its receipt.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path
from typing import Any

from evaluate_asr_ensemble import ROUTES, compact, load_funasr, load_qwen, load_whisper, similarity, support_metrics


WINDOW_RE = re.compile(r"^(F\d{6})_(\d{10})_(\d{10})$")


def qwen_groups(path: Path) -> dict[str, dict[str, str]]:
    rows = load_qwen(path)
    grouped: dict[str, dict[str, str]] = {}
    for stem, text in rows.items():
        window_id, route = stem.rsplit("__", 1)
        grouped.setdefault(window_id, {})[route] = text
    return grouped


def route_medoid(rows: dict[str, str]) -> tuple[str, float, float]:
    means: dict[str, float] = {}
    pair_values: list[float] = []
    for route in ROUTES:
        values = [similarity(rows[route], rows[other]) for other in ROUTES if other != route]
        means[route] = sum(values) / len(values)
    for left, right in itertools.combinations(ROUTES, 2):
        pair_values.append(similarity(rows[left], rows[right]))
    route = max(means, key=means.get)
    return route, means[route], min(pair_values)


def independent_medoid(
    window_id: str,
    qwen: dict[str, str],
    paraformer: dict[str, str],
    whisper: dict[str, str],
) -> tuple[str, str, dict[str, str], float, dict[str, Any]]:
    engines = {"qwen3_asr": qwen, "paraformer": paraformer, "whisper_large_v3": whisper}
    # A failed acoustic route is not silent speech and must never become a
    # placeholder candidate.  Keep the remaining independent engines only,
    # with the caller recording the unavailable route and forcing low quality.
    engines = {
        name: routes for name, routes in engines.items()
        if set(routes) == set(ROUTES) and all(compact(text) for text in routes.values())
    }
    if len(engines) < 2:
        raise ValueError("fewer than two independent ASR engines available")
    best: tuple[float, tuple[str, ...], dict[str, str], dict[str, float]] | None = None
    for route_tuple in itertools.product(ROUTES, repeat=3):
        selected = {engine: engines[engine][route] for engine, route in zip(engines, route_tuple)}
        pairs = {
            f"{left}__{right}": similarity(selected[left], selected[right])
            for left, right in itertools.combinations(selected, 2)
        }
        score = sum(pairs.values()) / len(pairs)
        candidate = (score, route_tuple, selected, pairs)
        if best is None or score > best[0]:
            best = candidate
    assert best is not None
    score, route_tuple, selected, pairs = best
    mean_by_engine = {
        engine: sum(value for key, value in pairs.items() if engine in key) / (len(engines) - 1)
        for engine in engines
    }
    engine = max(mean_by_engine, key=mean_by_engine.get)
    metrics = support_metrics(selected[engine], [text for name, text in selected.items() if name != engine])
    routes = dict(zip(engines, route_tuple))
    return engine, selected[engine], routes, score, metrics


def image_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("kind") == "image":
            item.pop("evidence_id", None)
            rows.append(item)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qwen", required=True, type=Path)
    parser.add_argument("--escalation-receipt", required=True, type=Path)
    parser.add_argument("--funasr-dir", required=True, type=Path)
    parser.add_argument("--whisper-dir", required=True, type=Path)
    parser.add_argument("--existing-evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--agreement-min", type=float, default=0.72)
    parser.add_argument("--support-min", type=float, default=0.82)
    args = parser.parse_args()

    qwen = qwen_groups(args.qwen)
    paraformer_all = load_funasr(args.funasr_dir)
    whisper_all = load_whisper(args.whisper_dir)
    escalation = json.loads(args.escalation_receipt.read_text(encoding="utf-8"))
    decisions = {row["window_id"]: row for row in escalation["decisions"]}
    evidence: list[dict[str, Any]] = []
    escalated_count = 0
    uncertain_count = 0
    engine_counts: dict[str, int] = {}

    for window_id, qwen_routes in sorted(qwen.items()):
        if set(qwen_routes) != set(ROUTES):
            raise SystemExit(f"incomplete Qwen routes: {window_id}")
        match = WINDOW_RE.match(window_id)
        if not match:
            raise SystemExit(f"invalid window id: {window_id}")
        source_id, start_ms, end_ms = match.groups()
        decision = decisions.get(window_id)
        if decision is None:
            raise SystemExit(f"missing escalation decision: {window_id}")

        uncertainty = None
        if decision.get("escalate"):
            escalated_count += 1
            paraformer = {route: paraformer_all.get(f"{window_id}__{route}", "") for route in ROUTES}
            whisper = {route: whisper_all.get(f"{window_id}__{route}", "") for route in ROUTES}
            unavailable_whisper_routes = [route for route, text in whisper.items() if not compact(text)]
            if any(not compact(text) for text in paraformer.values()):
                raise SystemExit(f"missing nonempty secondary candidate: {window_id}")
            engine, text, selected_routes, score, metrics = independent_medoid(
                window_id, qwen_routes, paraformer, whisper
            )
            supported = float(metrics["supported_by_two_ratio"])
            quality = "high" if score >= 0.82 and supported >= 0.9 else "medium"
            if unavailable_whisper_routes or score < args.agreement_min or supported < args.support_min:
                quality = "low"
                uncertainty = (
                    "whisper_acoustic_route_unavailable:" + ",".join(unavailable_whisper_routes)
                    if unavailable_whisper_routes else "cross_engine_agreement_below_gate"
                )
                uncertain_count += 1
            confidence = {
                "route": "asr_ensemble",
                "quality": quality,
                "mean_pairwise_similarity": round(score, 4),
                "supported_by_two_ratio": supported,
                "selected_engine": engine,
                "unavailable_acoustic_routes": {"whisper_large_v3": unavailable_whisper_routes},
            }
            models = ["Qwen3-ASR-1.7B", "Paraformer-zh", "whisper.cpp large-v3-q5_0"]
            engine_counts[engine] = engine_counts.get(engine, 0) + 1
        else:
            route, mean_score, min_score = route_medoid(qwen_routes)
            text = qwen_routes[route]
            selected_routes = {"qwen3_asr": route}
            quality = "high" if min_score >= 0.95 else "medium"
            confidence = {
                "route": "qwen_route_medoid",
                "quality": quality,
                "mean_route_similarity": round(mean_score, 4),
                "minimum_route_similarity": round(min_score, 4),
            }
            models = ["Qwen3-ASR-1.7B"]
            engine_counts["qwen3_asr"] = engine_counts.get("qwen3_asr", 0) + 1

        if not compact(text):
            uncertainty = uncertainty or "empty_candidate_not_certified_as_silence"
            uncertain_count += 1
        evidence.append({
            "source_id": source_id,
            "kind": "audio",
            "locator": {
                "start_seconds": int(start_ms) / 1000,
                "end_seconds": int(end_ms) / 1000,
                "window_id": window_id,
            },
            "literal_text": text.strip() or "[听不清]",
            "confidence": confidence,
            "models": models,
            "selected_routes": selected_routes,
            "uncertainty": uncertainty,
        })

    audio_count = len(evidence)
    images = image_rows(args.existing_evidence)
    evidence.extend(images)
    for ordinal, item in enumerate(evidence, start=1):
        item["evidence_id"] = f"E{ordinal:06d}"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in evidence), encoding="utf-8")
    temporary.replace(args.output)
    receipt = {
        "schema_version": 1,
        "status": "complete",
        "audio_window_count": audio_count,
        "escalated_window_count": escalated_count,
        "image_evidence_count": len(images),
        "evidence_count": len(evidence),
        "uncertain_audio_window_count": uncertain_count,
        "selected_engine_counts": engine_counts,
        "agreement_gate": {"mean_pairwise_similarity_min": args.agreement_min, "supported_by_two_ratio_min": args.support_min},
        "output": str(args.output),
        "content_included": False,
    }
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
