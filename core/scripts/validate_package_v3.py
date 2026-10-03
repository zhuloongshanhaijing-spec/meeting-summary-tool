#!/usr/bin/env python3
"""Deterministically validate the v3 literal-plus-slide meeting package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--literal-record", required=True, type=Path)
    parser.add_argument("--relations", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    args = parser.parse_args()
    required = ["00_使用说明.md", "01_主题索引.md", "02_逐句会议记录.md", "03_PPT补充信息.md", "04_会议报告.md", "05_不确定与冲突.md", "meeting.db", "coverage_receipt.json", "completion_receipt.json"]
    missing = [name for name in required if not (args.package_dir / name).is_file() or (args.package_dir / name).stat().st_size == 0]
    checks = {"required_artifacts_nonempty": not missing}
    literal_rows = [json.loads(line) for line in args.literal_record.read_text(encoding="utf-8").splitlines() if line.strip()]
    relations = [json.loads(line) for line in args.relations.read_text(encoding="utf-8").splitlines() if line.strip()]
    literal_text = (args.package_dir / "02_逐句会议记录.md").read_text(encoding="utf-8")
    slide_text = (args.package_dir / "03_PPT补充信息.md").read_text(encoding="utf-8")
    report_text = (args.package_dir / "04_会议报告.md").read_text(encoding="utf-8")
    # Slide evidence headings are E###### (classic photo flow) or I######
    # (screen-recording console flow, design §4.4); both satisfy the check.
    checks.update({
        "literal_ids_complete": set(re.findall(r"^### (R\d{6})", literal_text, re.MULTILINE)) == {row["record_id"] for row in literal_rows},
        "slide_evidence_complete": {evidence_id for row in relations for evidence_id in row.get("slide_evidence_ids") or []}.issubset(set(re.findall(r"^### ([EI]\d{6})", slide_text, re.MULTILINE))),
        "literal_has_timestamps": all("start_seconds" in row and "end_seconds" in row for row in literal_rows),
        "literal_preserves_raw_and_clean": all(isinstance(row.get("raw_text"), str) and isinstance(row.get("clean_literal"), str) for row in literal_rows),
        "utf8_without_replacement_characters": "�" not in literal_text + slide_text + report_text,
    })
    coverage = json.loads((args.package_dir / "coverage_receipt.json").read_text(encoding="utf-8"))
    checks["coverage_invariants_true"] = all((coverage.get("invariants") or {}).values())
    connection = sqlite3.connect(args.package_dir / "meeting.db")
    try:
        checks["database_integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        checks["database_literal_count"] = connection.execute("SELECT COUNT(*) FROM literal_records").fetchone()[0] == len(literal_rows)
        checks["database_relation_count"] = connection.execute("SELECT COUNT(*) FROM source_relations").fetchone()[0] == len(relations)
    finally:
        connection.close()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    root = Path(manifest["source_root"])
    mismatches = []
    for item in manifest.get("files") or []:
        if not item.get("eligible_source", item.get("kind") in {"audio", "image", "note"}):
            continue
        # Derived media (e.g. audio extracted from a screen recording) lives in
        # runs/, not under source_root; such manifest items carry an absolute
        # "path" override (design §4.3) instead of relying on join quirks.
        path = Path(item["path"]) if item.get("path") else root / item["relative_path"]
        if item.get("path") and not path.is_absolute():
            # A relative override would silently resolve against the CWD and
            # hash the wrong file; fail loud instead (review 2026-09-30).
            raise SystemExit(
                f"manifest path override must be absolute: {item['source_id']}: {item['path']}"
            )
        if not path.is_file() or sha256(path) != item["sha256"]:
            mismatches.append(item["source_id"])
    checks["sources_immutable"] = not mismatches
    status = "PASS" if all(checks.values()) else "FAIL"
    receipt = {
        "schema_version": 3, "status": status, "checks": checks, "missing_artifacts": missing,
        "source_mismatches": mismatches, "literal_record_count": len(literal_rows), "relation_count": len(relations),
        "content_included": False,
    }
    output = args.package_dir / "validation_receipt.json"
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
