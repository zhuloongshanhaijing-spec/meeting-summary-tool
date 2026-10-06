#!/usr/bin/env python3
"""Synthesize the final report and compact index from local navigation units."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconciled", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="Validate and receipt the already-rendered deterministic report without a second full-corpus model pass.",
    )
    args = parser.parse_args()

    data = json.loads(args.reconciled.read_text(encoding="utf-8"))
    units = data.get("units") or []
    valid_ids = {unit["unit_id"] for unit in units}
    report_path = args.package_dir / "会议报告.md"
    if args.deterministic_only:
        report = report_path.read_text(encoding="utf-8").strip() if report_path.is_file() else ""
        report_refs = set(re.findall(r"U\d{6}", report))
        if not report.strip() or not report_refs or not report_refs.issubset(valid_ids):
            raise SystemExit("deterministic report has missing or invalid unit references")
        receipt = {
            "schema_version": 1,
            "model": None,
            "synthesis_mode": "deterministic_fallback",
            "unit_count": len(units),
            "report_reference_count": len(report_refs),
            "index_source": "deterministic build_package.py",
            "indexed_unit_count": len(valid_ids),
            "content_included": False,
        }
        path = args.package_dir / "final_synthesis_receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path)
        return 0

    prompt = (
        "你是会议报告编辑。只依据输入的信息单元生成可读但信息充分的中文会议报告。"
        "报告必须区分会议直接内容与不确定内容；不得添加常识推断；保留条件、例外、步骤、数字、日期、姓名和有规则意义的例子。"
        "每项关键结论后使用 [U000001] 形式引用单元。不要声称百分之百准确。"
        "报告应包含：范围说明、主题综述、详细要点、可执行事项（仅来源明确时）、风险或未决问题、如何回查详细记录。"
        "只输出 Markdown 正文，不要输出 JSON，不要加代码围栏。\nUNITS_JSON:\n"
        + json.dumps(units, ensure_ascii=False)
    )
    payload = {
        "model": args.model,
        "stream": False,
        "think": False,
        "keep_alive": 0,
        "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        args.ollama_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1200) as response:
            envelope = json.load(response)
        report = ((envelope.get("message") or {}).get("content") or "").strip()
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"final report synthesis failed: {exc}") from exc

    report_refs = set(re.findall(r"U\d{6}", report))
    if not report.strip() or not report_refs or not report_refs.issubset(valid_ids):
        raise SystemExit("final report has missing or invalid unit references")
    args.package_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
    index_path = args.package_dir / "主题索引.md"
    if not index_path.is_file() or index_path.stat().st_size == 0:
        raise SystemExit("deterministic topic index is missing")
    receipt = {
        "schema_version": 1,
        "model": args.model,
        "synthesis_mode": "ollama",
        "unit_count": len(units),
        "report_reference_count": len(report_refs),
        "index_source": "deterministic build_package.py",
        "indexed_unit_count": len(valid_ids),
        "content_included": False,
    }
    path = args.package_dir / "final_synthesis_receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
