#!/usr/bin/env python3
"""Render the v3 meeting package with separate literal, slide, index, and report layers."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "??:??:??"
    total = max(0, int(round(float(value))))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--literal-record", required=True, type=Path)
    parser.add_argument("--relations", required=True, type=Path)
    parser.add_argument("--reconciled", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    # Optional screen-recording inputs (design §3.4/§4.7); when both are
    # absent every output file stays byte-identical to the classic behavior.
    parser.add_argument(
        "--ocr-corrections", type=Path,
        help="optional ocr_corrections.jsonl; rendered as advisory annotations only",
    )
    parser.add_argument(
        "--slides-dir", type=Path,
        help="optional directory of P*.png + slides.json copied into the package as 幻灯片/",
    )
    args = parser.parse_args()

    evidence = read_jsonl(args.evidence)
    records = read_jsonl(args.literal_record)
    relations = read_jsonl(args.relations)
    reconciled = json.loads(args.reconciled.read_text(encoding="utf-8"))
    corrections: list[dict[str, Any]] = []
    if args.ocr_corrections is not None and args.ocr_corrections.is_file():
        corrections = read_jsonl(args.ocr_corrections)
    corrections_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for correction in corrections:
        corrections_by_record[correction.get("record_id")].append(correction)
    known_record_ids = {row["record_id"] for row in records}
    unmatched_corrections = sum(
        1 for correction in corrections if correction.get("record_id") not in known_record_ids
    )
    units = reconciled.get("units") or []
    dispositions = reconciled.get("dispositions") or []
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    audio_ids = {item["evidence_id"] for item in evidence if item.get("kind") == "audio"}
    image_ids = {item["evidence_id"] for item in evidence if item.get("kind") == "image"}
    record_audio_ids = {evidence_id for row in records for evidence_id in row.get("evidence_ids") or []}
    relation_image_ids = {evidence_id for row in relations for evidence_id in row.get("slide_evidence_ids") or []}
    if record_audio_ids != audio_ids:
        raise SystemExit("literal record audio coverage mismatch")
    if relation_image_ids != image_ids:
        raise SystemExit("slide relation image coverage mismatch")
    if {item.get("evidence_id") for item in dispositions} != set(evidence_by_id):
        raise SystemExit("reconciled disposition coverage mismatch")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        source_records[row["source_id"]].append(row)
    literal_lines = [
        "# 逐句会议记录", "",
        "> 本文件只保存老师讲述的逐句或逐短段记录。轻度删改均列入编辑记录；时间精度默认是 ASR 窗口，不冒充逐字时间。", "",
    ]
    for source_id, rows in sorted(source_records.items()):
        literal_lines += [f"## 音频来源 {source_id}", ""]
        for row in rows:
            heading = f"### {row['record_id']} · {stamp(row.get('start_seconds'))}–{stamp(row.get('end_seconds'))} · {row.get('certainty', 'unrated')}"
            # OCR 修正只作渲染层标注（决策⑤）：clean_literal 原文永不改写。
            for correction in corrections_by_record.get(row["record_id"], []):
                heading += f" 〔OCR建议: {correction.get('original')}→{correction.get('suggested')} ({correction.get('ocr_page_id')})〕"
            literal_lines += [
                heading,
                "",
                row.get("clean_literal") or "[听不清]",
                f"- 证据：{', '.join(row.get('evidence_ids') or [])}",
                f"- 时间精度：{row.get('time_precision', 'unknown')}",
            ]
            if row.get("clean_literal") != row.get("raw_text"):
                literal_lines.append(f"- 原始候选：{row.get('raw_text')}")
            if row.get("edits"):
                literal_lines.append(f"- 编辑记录：{json.dumps(row['edits'], ensure_ascii=False, separators=(',', ':'))}")
            if row.get("uncertainty"):
                literal_lines.append(f"- 不确定性：{row['uncertainty']}")
            literal_lines.append("")
    (args.output_dir / "02_逐句会议记录.md").write_text("\n".join(literal_lines).rstrip() + "\n", encoding="utf-8")

    slide_lines = [
        "# PPT补充信息", "",
        "> 本文件保存照片中可见、但不一定被老师逐项说出的内容。PPT文字不得自动冒充老师原话。", "",
    ]
    for relation in relations:
        slide_lines += [f"## {relation['relation_id']} · 图片来源 {relation['slide_source_id']}", ""]
        for evidence_id in relation.get("slide_evidence_ids") or []:
            item = evidence_by_id[evidence_id]
            slide_lines += [
                f"### {evidence_id}", "", str(item.get("literal_text") or "[无法识别]"),
                f"- 图像区域：{json.dumps(item.get('locator'), ensure_ascii=False, separators=(',', ':'))}",
                f"- OCR不确定性：{item.get('uncertainty') or '无显式标记'}", "",
            ]
        candidates = relation.get("candidate_audio_records") or []
        slide_lines.append("- 候选语音对应：" + (", ".join(item["record_id"] for item in candidates) if candidates else "未找到"))
        slide_lines.append(f"- 当前关系：{relation.get('relation', 'unknown')}（{relation.get('decision_route')}）")
        slide_lines.append("")
    (args.output_dir / "03_PPT补充信息.md").write_text("\n".join(slide_lines).rstrip() + "\n", encoding="utf-8")

    topics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        topics[" / ".join(unit.get("topic_path") or ["未分类"])].append(unit)
    index_lines = ["# 主题索引", "", "先定位主题，再读取逐句记录、PPT补充和证据编号。", ""]
    evidence_to_records: dict[str, list[str]] = defaultdict(list)
    for row in records:
        for evidence_id in row.get("evidence_ids") or []:
            evidence_to_records[evidence_id].append(row["record_id"])
    for topic, topic_units in topics.items():
        record_ids = sorted({record_id for unit in topic_units for evidence_id in unit.get("evidence_ids") or [] for record_id in evidence_to_records.get(evidence_id, [])})
        index_lines.append(
            f"- **{topic}**：信息单元 {', '.join(unit['unit_id'] for unit in topic_units)}；逐句记录 {', '.join(record_ids) if record_ids else '无直接语音记录'}"
        )
    (args.output_dir / "01_主题索引.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    high = [unit for unit in units if unit.get("certainty") == "high"]
    report_lines = [
        "# 会议报告", "", "## 使用说明", "",
        "本报告用于理解主题与结论，不替代《02_逐句会议记录》和《03_PPT补充信息》。", "",
        "## 主题概览", "",
    ]
    report_lines += [f"- {topic}（{len(topic_units)} 个信息单元）" for topic, topic_units in topics.items()]
    report_lines += ["", "## 高置信度要点", ""]
    report_lines += [f"- [{unit['unit_id']}] {unit['claim']}" for unit in high]
    report_lines += ["", "## 回查路径", "", "主题索引 → 逐句记录/PPT补充 → 证据编号 → 原始录音或照片。", ""]
    (args.output_dir / "04_会议报告.md").write_text("\n".join(report_lines), encoding="utf-8")

    uncertain = [item for item in dispositions if item.get("status") in {"uncertain", "conflict"}]
    uncertain_lines = ["# 不确定与冲突", ""]
    for item in uncertain:
        evidence_id = item["evidence_id"]
        evidence_item = evidence_by_id[evidence_id]
        uncertain_lines.append(
            f"- **{evidence_id}** · {item.get('status')} · {item.get('reason') or '未说明'} "
            f"（来源 {evidence_item['source_id']}，定位 {json.dumps(evidence_item.get('locator'), ensure_ascii=False)}）"
        )
    if not uncertain:
        uncertain_lines.append("自动校验未登记未解决冲突；这不代表百分之百准确。")
    if corrections:
        uncertain_lines += ["", "## OCR 修正建议（录屏幻灯片证据）", ""]
        for correction in corrections:
            uncertain_lines.append(
                f"- 原词「{correction.get('original')}」→ 候选「{correction.get('suggested')}」"
                f"（证据 [{correction.get('ocr_page_id')}]，依据 {correction.get('basis')}）"
            )
    (args.output_dir / "05_不确定与冲突.md").write_text("\n".join(uncertain_lines) + "\n", encoding="utf-8")

    usage_lines = [
        "# 使用说明", "", "- 问老师具体说了什么：查《02_逐句会议记录》。",
        "- 问PPT列出的比赛、日期或事项：查《03_PPT补充信息》。",
        "- 快速定位主题：先查《01_主题索引》。", "- 阅读结论：查《04_会议报告》。",
        "- 低置信度内容：查《05_不确定与冲突》。", "",
    ]
    (args.output_dir / "00_使用说明.md").write_text("\n".join(usage_lines), encoding="utf-8")

    # Slide evidence layer (design §4.7): copy representative frames + slides.json
    # into the package. Only runs when --slides-dir is passed; the classic
    # audio-only call never creates 幻灯片/ and never touches the artifacts list.
    slides_copied = False
    if args.slides_dir is not None and args.slides_dir.is_dir():
        slides_target = args.output_dir / "幻灯片"
        # Idempotent re-runs into the same output dir: drop stale pages from a
        # previous build first (mirrors the meeting.db unlink below).
        shutil.rmtree(slides_target, ignore_errors=True)
        slides_target.mkdir(parents=True, exist_ok=True)
        for image in sorted(args.slides_dir.glob("P*.png")):
            if image.is_file():
                shutil.copy2(image, slides_target / image.name)
        slides_meta = args.slides_dir / "slides.json"
        if slides_meta.is_file():
            shutil.copy2(slides_meta, slides_target / "slides.json")
        slides_copied = True

    database = args.output_dir / "meeting.db"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
        CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, source_id TEXT, kind TEXT, locator_json TEXT, literal_text TEXT, uncertainty TEXT);
        CREATE TABLE literal_records(record_id TEXT PRIMARY KEY, source_id TEXT, start_seconds REAL, end_seconds REAL, raw_text TEXT, clean_literal TEXT, evidence_ids_json TEXT, certainty TEXT, relevance_json TEXT);
        CREATE TABLE units(unit_id TEXT PRIMARY KEY, topic_path TEXT, claim TEXT, certainty TEXT, evidence_ids_json TEXT);
        CREATE TABLE source_relations(relation_id TEXT PRIMARY KEY, slide_source_id TEXT, relation TEXT, payload_json TEXT);
        CREATE VIRTUAL TABLE literal_fts USING fts5(record_id UNINDEXED, clean_literal, raw_text, tokenize='trigram');
        CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, source_id UNINDEXED, literal_text, tokenize='trigram');
        CREATE VIRTUAL TABLE units_fts USING fts5(unit_id UNINDEXED, topic_path, claim, tokenize='trigram');
        """)
        connection.executemany("INSERT INTO evidence VALUES(?,?,?,?,?,?)", [
            (item["evidence_id"], item["source_id"], item["kind"], json.dumps(item.get("locator"), ensure_ascii=False), item.get("literal_text"), item.get("uncertainty")) for item in evidence
        ])
        connection.executemany("INSERT INTO evidence_fts VALUES(?,?,?)", [(item["evidence_id"], item["source_id"], item.get("literal_text")) for item in evidence])
        connection.executemany("INSERT INTO literal_records VALUES(?,?,?,?,?,?,?,?,?)", [
            (row["record_id"], row["source_id"], row.get("start_seconds"), row.get("end_seconds"), row.get("raw_text"), row.get("clean_literal"), json.dumps(row.get("evidence_ids"), ensure_ascii=False), row.get("certainty"),
             json.dumps(row.get("relevance"), ensure_ascii=False) if row.get("relevance") else None) for row in records
        ])
        connection.executemany("INSERT INTO literal_fts VALUES(?,?,?)", [(row["record_id"], row.get("clean_literal"), row.get("raw_text")) for row in records])
        for unit in units:
            topic = " / ".join(unit.get("topic_path") or [])
            connection.execute("INSERT INTO units VALUES(?,?,?,?,?)", (unit["unit_id"], topic, unit["claim"], unit.get("certainty"), json.dumps(unit.get("evidence_ids"), ensure_ascii=False)))
            connection.execute("INSERT INTO units_fts VALUES(?,?,?)", (unit["unit_id"], topic, unit["claim"]))
        connection.executemany("INSERT INTO source_relations VALUES(?,?,?,?)", [(row["relation_id"], row["slide_source_id"], row.get("relation"), json.dumps(row, ensure_ascii=False)) for row in relations])
        connection.commit()
    finally:
        connection.close()

    counts = Counter(item.get("status") for item in dispositions)
    coverage = {
        "schema_version": 3, "audio_evidence_count": len(audio_ids), "image_evidence_count": len(image_ids),
        "literal_record_count": len(records), "slide_relation_count": len(relations), "unit_count": len(units),
        "disposition_counts": dict(sorted(counts.items())),
        "invariants": {
            "every_audio_evidence_in_literal_record": record_audio_ids == audio_ids,
            "every_image_evidence_in_slide_supplement": relation_image_ids == image_ids,
            "every_evidence_has_disposition": {item.get('evidence_id') for item in dispositions} == set(evidence_by_id),
            "unit_references_resolve": all(set(unit.get("evidence_ids") or []).issubset(evidence_by_id) for unit in units),
        },
    }
    (args.output_dir / "coverage_receipt.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    completion = {
        "schema_version": 3, "status": "COMPLETE_WITH_UNCERTAINTY" if uncertain else "COMPLETE",
        "artifacts": ["00_使用说明.md", "01_主题索引.md", "02_逐句会议记录.md", "03_PPT补充信息.md", "04_会议报告.md", "05_不确定与冲突.md", "meeting.db", "coverage_receipt.json"],
        "uncertainty_count": len(uncertain), "archive_status": "not_applied", "content_level_supervision": False,
    }
    if slides_copied:
        completion["artifacts"].append("幻灯片/")
    if corrections:
        # record_ids that matched no rendered 02 record line were skipped
        # silently (annotation-only surface); the count stays auditable here.
        completion["ocr_correction_unmatched"] = unmatched_corrections
    (args.output_dir / "completion_receipt.json").write_text(json.dumps(completion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output_dir / "completion_receipt.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
