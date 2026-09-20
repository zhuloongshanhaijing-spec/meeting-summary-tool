#!/usr/bin/env python3
"""Render a separate note corroboration layer beside an unchanged transcript."""
import argparse
import json
from collections import Counter
from pathlib import Path


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stamp(value: float) -> str:
    value = int(value)
    return f"{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--relations", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    records, notes, relations = load(args.records), {row["note_evidence_id"]: row for row in load(args.notes)}, load(args.relations)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    transcript = ["# 测试逐句记录", "", "> 仅来自录音 ASR；笔记不会修改本文件。", ""]
    for row in records:
        transcript += [f"## {row['record_id']} · {stamp(row['start_seconds'])}–{stamp(row['end_seconds'])} · {row['certainty']}", "", row['clean_literal'], f"- 录音证据：{', '.join(row['evidence_ids'])}"]
        if row.get('uncertainty'): transcript.append(f"- 不确定性：{row['uncertainty']}")
        transcript.append("")
    (args.output_dir / "02_测试逐句记录.md").write_text("\n".join(transcript), encoding="utf-8")
    material_links = [link for link in relations if link.get("relation") in {"supports", "partial_support", "possible_related", "conflict"}]
    counts = Counter(link.get("relation") for link in relations)
    corroboration = ["# 笔记佐证与冲突", "", "> 笔记是独立来源，不是讲者原话；它不能修复录音。", "", f"- 已检查笔记证据：{len(relations)} 条", f"- 未关联（未在本样本显示正文）：{counts.get('unknown', 0) - sum(1 for link in material_links if link.get('relation') == 'unknown')} 条", ""]
    for link in material_links:
        note = notes[link['note_evidence_id']]
        loc = note['locator']
        corroboration += [f"## {link['relation_id']} · {link['relation']}", "", note['literal_text'], f"- 笔记来源：{loc['path']} 第 {loc['line_start']} 行", f"- 候选录音段落：{', '.join(link.get('matched_record_ids') or []) or '无'}", f"- 判定依据：{link.get('reason') or '未说明'}"]
        if link.get('conflict_fields'): corroboration.append(f"- 冲突字段：{', '.join(link['conflict_fields'])}")
        corroboration.append("")
    (args.output_dir / "06_笔记佐证与冲突.md").write_text("\n".join(corroboration), encoding="utf-8")
    coverage = {"status": "complete", "note_evidence_count": len(relations), "disposition_counts": dict(sorted(counts.items())), "displayed_relation_count": len(material_links), "all_note_relations_have_disposition": len(relations) == len(notes)}
    (args.output_dir / "note_coverage_receipt.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.receipt.write_text(json.dumps({"status": "complete", "literal_transcript_modified_by_notes": False, "note_relation_count": len(relations), "displayed_relation_count": len(material_links), "content_included": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
