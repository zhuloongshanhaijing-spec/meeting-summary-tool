#!/usr/bin/env python3
"""Ask a small local Ollama model to triage a metadata-first supervisor packet."""

from __future__ import annotations

import argparse
import copy
import json
import urllib.error
import urllib.request
from pathlib import Path


ACTIONS = {"START_STAGE", "RETRY", "CHANGE_ROUTE", "REQUEST_EVIDENCE", "BLOCK", "ACCEPT_COMPLETE"}
ALLOWED_BY_STATUS = {
    "READY": {"START_STAGE", "BLOCK"},
    "NEEDS_RETRY": {"RETRY", "CHANGE_ROUTE", "BLOCK"},
    "NEEDS_SUPERVISOR": {"CHANGE_ROUTE", "REQUEST_EVIDENCE", "BLOCK"},
    "COMPLETE": {"ACCEPT_COMPLETE", "BLOCK"},
}


def sanitize(packet: dict, include_bounded_content: bool) -> dict:
    result = copy.deepcopy(packet)
    if not include_bounded_content:
        bounded = []
        for item in result.get("bounded_evidence") or []:
            bounded.append({key: value for key, value in item.items() if key != "text"})
        result["bounded_evidence"] = bounded
        result["content_included"] = False
    return result


def call_ollama(url: str, model: str, packet: dict) -> dict:
    prompt = (
        "You supervise a deterministic meeting-processing pipeline. "
        "Do not infer meeting content. Choose exactly one action from "
        f"{sorted(ACTIONS)}. Prefer deterministic retry only when attempts remain; "
        "request bounded evidence only when a semantic decision is unavoidable. "
        "Return JSON with keys action, reason, next_stage, fallback_route, "
        "requested_evidence, and safe_to_continue. Packet:\n"
        + json.dumps(packet, ensure_ascii=False)
    )
    request_body = {
        "model": model,
        "stream": False,
        "keep_alive": 0,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": "Return a conservative pipeline-control decision as JSON only."},
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            envelope = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Ollama supervisor call failed: {exc}") from exc
    content = ((envelope.get("message") or {}).get("content") or "").strip()
    try:
        decision = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"supervisor returned invalid JSON: {exc}") from exc
    if decision.get("action") not in ACTIONS:
        raise SystemExit(f"invalid supervisor action: {decision.get('action')!r}")
    if not isinstance(decision.get("safe_to_continue"), bool):
        raise SystemExit("supervisor decision lacks boolean safe_to_continue")
    return decision


def enforce_controller_policy(packet: dict, decision: dict) -> tuple[dict, list[str]]:
    status = packet.get("status")
    allowed = ALLOWED_BY_STATUS.get(status, {"BLOCK"})
    if decision.get("action") in allowed:
        return decision, []
    failed_text = " ".join(str(item) for item in packet.get("failed_checks") or []).lower()
    if status == "NEEDS_SUPERVISOR" and "no command adapter configured" in failed_text:
        fallback = "CHANGE_ROUTE"
        reason = "The configured route is absent; retrying the same route cannot succeed."
    else:
        fallback = "BLOCK"
        reason = f"Model action {decision.get('action')!r} is not allowed for controller status {status!r}."
    safe_decision = {
        "action": fallback,
        "reason": reason,
        "next_stage": packet.get("stage"),
        "fallback_route": None,
        "requested_evidence": [],
        "safe_to_continue": False,
    }
    return safe_decision, [reason]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--include-bounded-content", action="store_true")
    args = parser.parse_args()

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    safe_packet = sanitize(packet, args.include_bounded_content)
    raw_decision = call_ollama(args.ollama_url, args.model, safe_packet)
    decision, warnings = enforce_controller_policy(safe_packet, raw_decision)
    result = {
        "schema_version": 1,
        "model": args.model,
        "content_sent": args.include_bounded_content,
        "run_id": packet.get("run_id"),
        "stage": packet.get("stage"),
        "decision": decision,
        "model_decision": raw_decision,
        "validation_warnings": warnings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
