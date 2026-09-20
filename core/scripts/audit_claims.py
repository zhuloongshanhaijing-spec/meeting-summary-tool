#!/usr/bin/env python3
"""Claim-fidelity audit + repair for reconciled topic units.

The reconcile LLM can invert meaning when compressing evidence into claims
(e.g. 'regressive' -> 「累进性」 — the exact economic antonym), silently
correct garbled ASR, or attribute intent the source never states. Downstream
consumers see a clean claim with a valid evidence id and no way to know. This
stage audits every unit against its cited evidence text, repairs distorted
claims strictly from the evidence, and downgrades residual failures to
low certainty so they surface in the uncertainty view.

Audit verdicts never delete or rewrite the literal record layer; only unit
claims (the derived summary layer) are corrected, each leaving an audit trail.

Usage:
  python3 audit_claims.py --reconciled runs/<ev>/reconciled/reconciled.json \
      --evidence runs/<ev>/evidence/evidence.jsonl \
      --output runs/<ev>/reconciled/reconciled_audited.json \
      --receipt runs/<ev>/reconciled/claim_audit_receipt.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BUG_CLASSES = """逐项检查以下已知缺陷类别：
1. 反义/误译：claim 中的关键词与证据文本含义相反（如 regressive 译成「累进」而非「累退」）。
2. 乱码无声改写：证据是乱码/低质 ASR，claim 却按猜测改写并当作确定事实。
3. 强加意图/因果：claim 加入证据没有的「旨在/因为/为了」等意图或因果框架。
4. 引证缺口：claim 的关键细节（数字、时间范围、主体）不在所引用的证据文本里。
5. 断句失真：claim 给证据里被截断的句子编造了补全词、把残句当作完整事实陈述。
以下均不是缺陷（双前沿审计校准）：证据本身在窗口边界截断（重叠窗口已补足相邻内容）；claim 原样保留乱码词（如音译串）而未按猜测改写；claim 已用「（原文不清）」等标注不确定性。
faithful 的标准：claim 语义与引用证据一致，且关键细节都被证据覆盖。"""


def call_ollama(url: str, model: str, prompt: str, keep_alive: str,
                temperature: float, timeout: int = 300) -> str:
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive,
        "think": False,  # qwen3: reasoning can exhaust num_predict -> empty body
        "options": {"temperature": temperature, "num_ctx": 8192, "num_predict": 2048},
    }).encode("utf-8")
    request = urllib.request.Request(url.rstrip("/") + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    raw = body.get("response", "")
    if not raw.strip():
        raise RuntimeError("empty model response")
    return raw


def parse_verdicts(text: str, expected_ids: set[str]) -> list[dict]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?", "", cleaned)
    candidates = re.findall(r"\[.*\]", cleaned, re.DOTALL)
    if "[" in cleaned and "]" in cleaned and not candidates:
        candidates = [cleaned[cleaned.index("["):cleaned.rindex("]") + 1]]
    for span in candidates:
        try:
            items = json.loads(span)
        except json.JSONDecodeError:
            continue
        verdicts = [v for v in items
                    if isinstance(v, dict) and v.get("unit_id") in expected_ids]
        if verdicts:
            return verdicts
    raise RuntimeError("no valid verdict array in model output")


def _key_details(text: str) -> set[str]:
    """Salient lexical details: numbers and latin tokens (>=3 chars).

    CJK content words are not extractable lexically; this guard therefore
    protects the highest-stakes details (figures, dates, names, units).
    """
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    latin = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)}
    return set(numbers) | latin


def unit_prompt(unit: dict, evidence: dict[str, str]) -> str:
    cited = []
    for eid in unit.get("evidence_ids", []) or []:
        if eid in evidence:
            cited.append(f"{eid}: {evidence[eid]}")
    cited_text = "\n".join(cited) if cited else "（无引用证据）"
    extras = []
    for key in ("conditions", "exceptions", "examples"):
        value = unit.get(key)
        if value:
            extras.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:160]}")
    extras_text = "\n".join(extras)
    return (f"unit_id: {unit['unit_id']}\nclaim: {unit['claim']}\n"
            f"{extras_text}\n引用证据（逐条）：\n{cited_text}")


def _audit_chunk(chunk: list[dict], evidence: dict[str, str], url: str, model: str,
                 keep_alive: str, stats: dict) -> dict[str, dict] | None:
    block = "\n\n".join(unit_prompt(u, evidence) for u in chunk)
    prompt = (
        "你是证据保真审计员。对照每个 unit 的 claim 与其引用证据，判定忠实性。\n"
        f"{BUG_CLASSES}\n"
        "严格度校准：判定标准是语义忠实与关键细节覆盖，不是文风或表述完整性；"
        "无关紧要的省略或改写不算缺陷，判 faithful 即可。\n"
        "若判定不是 faithful，必须给出 corrected_claim：仅依据上面的引用证据文本重写，"
        "不添加证据外的新信息，保留 claim 原语言；证据本身乱码/残缺时，把 claim 改写为"
        "证据实际能支撑的限定表述（例如加「（原文不清）」）。\n"
        "只输出 JSON 数组：[{\"unit_id\": \"...\", \"status\": \"faithful|distorted|unsupported\", "
        "\"issue\": \"...\", \"corrected_claim\": \"...\"}]，不要输出其他文字。\n\n"
        f"{block}"
    )
    expected = {u["unit_id"] for u in chunk}
    for temperature in (0.1, 0.5, 0.3):
        try:
            stats["calls"] += 1
            raw = call_ollama(url, model, prompt, keep_alive, temperature)
            return {v["unit_id"]: v for v in parse_verdicts(raw, expected)}
        except (urllib.error.URLError, TimeoutError, OSError,
                RuntimeError, json.JSONDecodeError):
            stats["failures"] += 1
            time.sleep(2)
    return None


def audit_pass(units: list[dict], evidence: dict[str, str], url: str, model: str,
               keep_alive: str, chunk_size: int, stats: dict) -> dict[str, dict]:
    verdicts: dict[str, dict] = {}
    for start in range(0, len(units), chunk_size):
        chunk = units[start:start + chunk_size]
        result = _audit_chunk(chunk, evidence, url, model, keep_alive, stats)
        if result is None and len(chunk) > 1:
            # escalation: a failed batch almost always survives per-unit calls
            for unit in chunk:
                single = _audit_chunk([unit], evidence, url, model, keep_alive, stats)
                if single is not None:
                    result = {**(result or {}), **single}
        if result is None:
            for unit in chunk:  # un-auditable units must not silently pass
                verdicts.setdefault(unit["unit_id"], {
                    "status": "unsupported", "issue": "audit call failed after escalation"})
        else:
            verdicts.update(result)
    return verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim-fidelity audit + repair")
    parser.add_argument("--reconciled", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--keep-alive", default="5m")
    parser.add_argument("--chunk-size", type=int, default=6)
    args = parser.parse_args()

    reconciled = json.loads(args.reconciled.read_text(encoding="utf-8"))
    units = reconciled.get("units") or []
    evidence = {}
    for line in args.evidence.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            evidence[item["evidence_id"]] = item.get("literal_text", "")

    stats = {"calls": 0, "failures": 0}
    first = audit_pass(units, evidence, args.ollama_url, args.model,
                       args.keep_alive, args.chunk_size, stats)
    faithful_first = sum(1 for v in first.values() if v.get("status") == "faithful")

    # Repair round: apply corrected claims, then re-audit only repaired units.
    repaired_units, repaired_ids = [], []
    trails: dict[str, list] = {}
    for unit in units:
        verdict = first.get(unit["unit_id"], {"status": "unsupported", "issue": "not audited"})
        trails[unit["unit_id"]] = [{
            "round": 1, "status": verdict.get("status"), "issue": verdict.get("issue")}]
        corrected = verdict.get("corrected_claim")
        if verdict.get("status") != "faithful" and isinstance(corrected, str) and corrected.strip():
            unit = dict(unit)
            corrected = corrected.strip()
            # preservation guard: key details (numbers, latin tokens) that the
            # ORIGINAL claim stated AND the cited evidence supports must
            # survive the repair — otherwise the rewrite silently loses correct
            # content and we downgrade to residual instead of repairing
            original_details = _key_details(unit["claim"])
            evidence_pool = " ".join(evidence.get(eid, "") for eid in unit.get("evidence_ids", []) or [])
            # NOTE: a coincidental detail match in cited evidence blocks the
            # repair (fail-closed bias) — deliberate; the flagged original is
            # safer than a possibly lossy rewrite. Cannot judge semantic relevance.
            dropped = original_details - _key_details(corrected)
            backed_lost = dropped & _key_details(evidence_pool)
            if backed_lost:
                trails[unit["unit_id"]].append({
                    "round": 1, "status": "repair_rejected",
                    "issue": f"correction drops evidence-backed details: {sorted(backed_lost)[:6]}"})
            else:
                trails[unit["unit_id"]].append({"round": 1, "status": "repair_accepted",
                                                "original_claim": unit["claim"]})
                unit["claim"] = corrected
                repaired_ids.append(unit["unit_id"])
        repaired_units.append(unit)

    second: dict[str, dict] = {}
    if repaired_ids:
        # re-audit ONLY repaired units — re-auditing everything doubles LLM
        # cost for zero effect (faithful originals short-circuit on round-1)
        second = audit_pass([u for u in repaired_units if u["unit_id"] in repaired_ids],
                            evidence, args.ollama_url, args.model,
                            args.keep_alive, args.chunk_size, stats)
    residual_ids = []
    for unit in repaired_units:
        verdict2 = second.get(unit["unit_id"])
        was_repaired = unit["unit_id"] in repaired_ids
        if was_repaired and verdict2 is not None:
            trails[unit["unit_id"]].append({
                "round": 2, "status": verdict2.get("status"), "issue": verdict2.get("issue")})
        elif was_repaired:
            trails[unit["unit_id"]].append({
                "round": 2, "status": "audit_unavailable",
                "issue": "round-2 re-audit call failed; downgraded conservatively"})
        ok_first = first.get(unit["unit_id"], {}).get("status") == "faithful"
        ok_second = verdict2 is None or verdict2.get("status") == "faithful"
        if ok_first and not was_repaired:
            continue  # faithful, untouched
        if was_repaired and ok_second and verdict2 is not None:
            unit["audit_repaired"] = True  # claim rewritten from evidence, verified
            continue
        # residual: downgrade so the uncertainty view surfaces it
        residual_ids.append(unit["unit_id"])
        unit["certainty"] = "low"
        trail = trails[unit["unit_id"]]
        unit["audit_note"] = "; ".join(
            f"r{t['round']}: {t['issue']}" for t in trail if t.get("issue"))
        if not was_repaired:
            unit["audit_flagged"] = True  # unfaithful, no corrected_claim available

    reconciled["units"] = repaired_units
    reconciled["claim_audit"] = {
        "audited": len(units), "faithful_first_pass": faithful_first,
        "repaired": len(repaired_ids), "residual": len(residual_ids),
        "residual_ids": residual_ids, "llm": stats,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reconciled, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    args.receipt.write_text(json.dumps({
        "status": "complete", "audited": len(units),
        "faithful_first_pass": faithful_first, "repaired": len(repaired_ids),
        "residual": len(residual_ids), "residual_ids": residual_ids,
        "llm_calls": stats["calls"], "llm_failures": stats["failures"],
        "bug_classes_checked": 5, "content_included": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(reconciled["claim_audit"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
