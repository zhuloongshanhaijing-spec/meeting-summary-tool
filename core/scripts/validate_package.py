#!/usr/bin/env python3
"""Deterministically validate a rendered meeting package and source immutability."""

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
    parser.add_argument("--reconciled", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    args = parser.parse_args()

    required = ["会议报告.md", "主题索引.md", "详细记录.md", "不确定与冲突.md", "meeting.db", "coverage_receipt.json", "completion_receipt.json", "final_synthesis_receipt.json"]
    checks = {}
    missing = [name for name in required if not (args.package_dir / name).is_file() or (args.package_dir / name).stat().st_size == 0]
    checks["required_artifacts_nonempty"] = not missing

    reconciled = json.loads(args.reconciled.read_text(encoding="utf-8"))
    units = reconciled.get("units") or []
    dispositions = reconciled.get("dispositions") or []
    unit_ids = {unit["unit_id"] for unit in units}
    evidence_ids = {item["evidence_id"] for item in dispositions}
    texts = {}
    replacement_count = 0
    for name in ["会议报告.md", "主题索引.md", "详细记录.md", "不确定与冲突.md"]:
        text = (args.package_dir / name).read_text(encoding="utf-8")
        texts[name] = text
        replacement_count += text.count("�")
    report_refs = set(re.findall(r"U\d{6}", texts["会议报告.md"]))
    index_refs = set(re.findall(r"U\d{6}", texts["主题索引.md"]))
    detail_unit_refs = set(re.findall(r"^### (U\d{6})", texts["详细记录.md"], re.MULTILINE))
    detail_evidence_refs = set(re.findall(r"^### (E\d{6})", texts["详细记录.md"], re.MULTILINE))
    checks.update({
        "utf8_without_replacement_characters": replacement_count == 0,
        "report_references_resolve": bool(report_refs) and report_refs.issubset(unit_ids),
        "index_covers_all_units": index_refs == unit_ids,
        "detail_contains_all_units": detail_unit_refs == unit_ids,
        "detail_contains_all_evidence": detail_evidence_refs == evidence_ids,
    })

    coverage = json.loads((args.package_dir / "coverage_receipt.json").read_text(encoding="utf-8"))
    checks["coverage_invariants_true"] = all(coverage.get("invariants", {}).values())
    checks["coverage_count_matches"] = coverage.get("eligible_evidence_count") == len(evidence_ids) == len(dispositions)

    connection = sqlite3.connect(args.package_dir / "meeting.db")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        evidence_count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        unit_count = connection.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        evidence_fts_count = connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone()[0]
        unit_fts_count = connection.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0]
    finally:
        connection.close()
    checks.update({
        "database_integrity": integrity == "ok",
        "database_evidence_count": evidence_count == len(evidence_ids),
        "database_unit_count": unit_count == len(unit_ids),
        "database_fts_counts": evidence_fts_count == evidence_count and unit_fts_count == unit_count,
    })

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    root = Path(manifest["source_root"])
    source_mismatches = []
    checked_sources = 0
    for item in manifest.get("files") or []:
        if item.get("kind") not in {"audio", "image", "note"}:
            continue
        path = root / item["relative_path"]
        checked_sources += 1
        if not path.is_file() or sha256(path) != item["sha256"]:
            source_mismatches.append(item["source_id"])
    checks["sources_immutable"] = not source_mismatches

    passed = all(checks.values())
    receipt = {
        "schema_version": 1,
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "missing_artifacts": missing,
        "source_mismatches": source_mismatches,
        "checked_source_count": checked_sources,
        "evidence_count": len(evidence_ids),
        "unit_count": len(unit_ids),
        "report_reference_count": len(report_refs),
        "package_bytes": sum(p.stat().st_size for p in args.package_dir.iterdir() if p.is_file()),
        "content_included": False,
    }
    output = args.package_dir / "validation_receipt.json"
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
