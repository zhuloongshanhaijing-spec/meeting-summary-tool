#!/usr/bin/env python3
"""Validate reconciled units and render the searchable meeting package."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def bullets(values: list[str], prefix: str) -> list[str]:
    return [f"  - {prefix}：{value}" for value in values if value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--reconciled", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    evidence = read_jsonl(args.evidence)
    evidence_by_id = {x["evidence_id"]: x for x in evidence}
    reconciled = json.loads(args.reconciled.read_text(encoding="utf-8"))
    units = reconciled.get("units") or []
    dispositions = reconciled.get("dispositions") or []
    evidence_ids = set(evidence_by_id)
    disposition_ids = [x.get("evidence_id") for x in dispositions]
    errors: list[str] = []
    if len(disposition_ids) != len(set(disposition_ids)):
        errors.append("duplicate disposition IDs")
    if set(disposition_ids) != evidence_ids:
        errors.append("disposition coverage does not equal evidence set")
    for unit in units:
        if not unit.get("claim") or not unit.get("evidence_ids"):
            errors.append(f"{unit.get('unit_id')}: missing claim or evidence")
        missing = set(unit.get("evidence_ids") or []) - evidence_ids
        if missing:
            errors.append(f"{unit.get('unit_id')}: missing evidence IDs {sorted(missing)}")
    covered_refs = {ref for u in units for ref in u.get("evidence_ids") or []}
    covered_dispositions = {x["evidence_id"] for x in dispositions if x.get("status") == "covered"}
    if not covered_dispositions.issubset(covered_refs):
        errors.append("covered dispositions lack a linked information unit")
    if errors:
        raise SystemExit("; ".join(errors))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    topics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        path = unit.get("topic_path") or ["未分类"]
        topics[" / ".join(path)].append(unit)

    detail_lines = ["# 详细记录", "", "> 本文按信息单元保存会议内容；每条均链接本地证据编号。", ""]
    for topic, topic_units in topics.items():
        detail_lines += [f"## {topic}", ""]
        for unit in topic_units:
            detail_lines.append(f"### {unit['unit_id']} · {unit.get('certainty', 'unrated')}")
            detail_lines.append("")
            detail_lines.append(str(unit["claim"]))
            detail_lines += bullets(unit.get("conditions") or [], "条件")
            detail_lines += bullets(unit.get("exceptions") or [], "例外")
            detail_lines += bullets(unit.get("examples") or [], "例子")
            detail_lines.append(f"- 证据：{', '.join(unit['evidence_ids'])}")
            detail_lines.append("")
    detail_lines += ["## 逐条证据附录", "", "> 这一附录保留全部识别文字及定位，防止整理模型漏掉细节。候选文字不等于已核实事实。", ""]
    for item in evidence:
        locator = json.dumps(item.get("locator"), ensure_ascii=False, separators=(",", ":"))
        detail_lines += [
            f"### {item['evidence_id']} · {item['source_id']} · {item['confidence'].get('route')}",
            "",
            item["literal_text"],
            f"- 定位：{locator}",
            f"- 不确定性：{item.get('uncertainty') or '无显式标记'}",
            "",
        ]
    (args.output_dir / "详细记录.md").write_text("\n".join(detail_lines).rstrip() + "\n", encoding="utf-8")

    index_lines = ["# 主题索引", "", "先在这里定位主题，再按信息单元编号读取《详细记录》。", ""]
    for topic, topic_units in topics.items():
        index_lines.append(f"- **{topic}**：{', '.join(u['unit_id'] for u in topic_units)}")
    (args.output_dir / "主题索引.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    high = [u for u in units if u.get("certainty") == "high"]
    report_lines = [
        "# 会议报告",
        "",
        "## 使用说明",
        "",
        "本报告是入口，不替代《详细记录》。需要具体条件、步骤、例外或原话依据时，请按单元编号回查。",
        "",
        "## 主题概览",
        "",
    ]
    report_lines += [f"- {topic}（{len(topic_units)} 个信息单元）" for topic, topic_units in topics.items()]
    report_lines += ["", "## 高置信度要点", ""]
    report_lines += [f"- [{u['unit_id']}] {u['claim']}" for u in high]
    report_lines += ["", "## 完整信息入口", "", "详见《主题索引》和《详细记录》。", ""]
    (args.output_dir / "会议报告.md").write_text("\n".join(report_lines), encoding="utf-8")

    uncertain_status = {"uncertain", "conflict"}
    uncertainty = [x for x in dispositions if x.get("status") in uncertain_status]
    uncertain_lines = ["# 不确定与冲突", ""]
    if uncertainty:
        for item in uncertainty:
            ev = evidence_by_id[item["evidence_id"]]
            uncertain_lines.append(
                f"- **{item['evidence_id']}** · {item['status']} · {item.get('reason') or '未说明'} "
                f"（来源 {ev['source_id']}，定位 {json.dumps(ev.get('locator'), ensure_ascii=False)}）"
            )
    else:
        uncertain_lines.append("自动校验未登记未解决冲突；这不等于识别结果百分之百无误。")
    (args.output_dir / "不确定与冲突.md").write_text("\n".join(uncertain_lines) + "\n", encoding="utf-8")

    database = args.output_dir / "meeting.db"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, source_id TEXT, kind TEXT, locator_json TEXT, literal_text TEXT, uncertainty TEXT);
            CREATE TABLE units(unit_id TEXT PRIMARY KEY, topic_path TEXT, claim TEXT, certainty TEXT, evidence_ids_json TEXT);
            CREATE VIRTUAL TABLE units_fts USING fts5(unit_id UNINDEXED, topic_path, claim, tokenize='unicode61');
            CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, source_id UNINDEXED, literal_text, tokenize='unicode61');
            """
        )
        connection.executemany(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?)",
            [(x["evidence_id"], x["source_id"], x["kind"], json.dumps(x.get("locator"), ensure_ascii=False), x["literal_text"], x.get("uncertainty")) for x in evidence],
        )
        connection.executemany(
            "INSERT INTO evidence_fts VALUES(?,?,?)",
            [(x["evidence_id"], x["source_id"], x["literal_text"]) for x in evidence],
        )
        for unit in units:
            topic = " / ".join(unit.get("topic_path") or [])
            connection.execute("INSERT INTO units VALUES(?,?,?,?,?)", (unit["unit_id"], topic, unit["claim"], unit.get("certainty"), json.dumps(unit["evidence_ids"], ensure_ascii=False)))
            connection.execute("INSERT INTO units_fts VALUES(?,?,?)", (unit["unit_id"], topic, unit["claim"]))
        connection.commit()
    finally:
        connection.close()

    counts = Counter(x.get("status") for x in dispositions)
    coverage = {
        "schema_version": 1,
        "eligible_evidence_count": len(evidence),
        "disposition_count": len(dispositions),
        "counts": dict(sorted(counts.items())),
        "unit_count": len(units),
        "invariants": {
            "every_evidence_has_one_disposition": len(disposition_ids) == len(set(disposition_ids)) == len(evidence),
            "covered_evidence_has_unit": covered_dispositions.issubset(covered_refs),
            "unit_references_resolve": all(set(u.get("evidence_ids") or []).issubset(evidence_ids) for u in units),
        },
    }
    (args.output_dir / "coverage_receipt.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    completion = {
        "schema_version": 1,
        "status": "COMPLETE_WITH_UNCERTAINTY" if uncertainty else "COMPLETE",
        "artifacts": ["会议报告.md", "主题索引.md", "详细记录.md", "不确定与冲突.md", "meeting.db", "coverage_receipt.json"],
        "uncertainty_count": len(uncertainty),
        "archive_status": "not_applied",
        "content_level_supervision": False,
    }
    (args.output_dir / "completion_receipt.json").write_text(json.dumps(completion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output_dir / "completion_receipt.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
