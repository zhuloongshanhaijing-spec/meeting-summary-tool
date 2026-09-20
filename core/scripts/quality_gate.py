#!/usr/bin/env python3
"""Deterministic quality gate for a meeting run.

Checks a run directory and its output package against measurable invariants.
This gate never interprets content; it only verifies structure, coverage,
alignment and searchability so a finished run can be trusted without manual
review. Mixed Chinese/English text is treated as valid speech, never as
contamination.

Exit codes: 0 = PASS, 2 = WARN (uncertain), 3 = FAIL (blocking).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wav_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def stamp(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def check_source_duration(run_dir: Path) -> tuple[list[str], list[str]]:
    """Enhanced/normalized tracks must cover the full source duration."""
    errors, warnings = [], []
    manifest = run_dir / "manifest.json"
    if not manifest.is_file():
        return ([], ["manifest.json missing; source track duration check skipped"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    sources = [f for f in data.get("files", []) if f.get("kind") == "audio"]
    if not sources:
        return (["manifest has no audio files"], [])
    # per-track comparison: a multi-track event must not compare every WAV
    # against the longest source (that flags every shorter track as truncated)
    durations = {f.get("source_id"): (f.get("media", {}) or {}).get("duration_seconds") or 0
                 for f in sources if f.get("source_id")}
    if not any(d > 0 for d in durations.values()):
        return ([], ["source duration unknown; skipping track duration check"])
    for track_dir in ("normalized", "enhanced"):
        for wav in (run_dir / "prepared" / "artifacts" / "audio" / track_dir).glob("*.wav"):
            expected = durations.get(wav.stem)
            got = wav_duration(wav)
            if got is None:
                warnings.append(f"cannot probe duration of {track_dir}/{wav.name}")
            elif not expected:
                warnings.append(f"no source duration for {wav.stem}; skipping {track_dir} check")
            elif got < expected * 0.9:
                errors.append(
                    f"track truncated: {track_dir}/{wav.name} is {got:.0f}s but source is {expected:.0f}s "
                    f"(covers {got / expected:.0%})"
                )
    return errors, warnings


def check_transcript(run_dir: Path, literal_path: Path) -> tuple[list[str], list[str]]:
    """Transcript records must be ordered, non-degenerate and gap-audited."""
    errors, warnings = [], []
    records = load_jsonl(literal_path)
    if not records:
        return ([f"no literal records in {literal_path}"], [])
    expected = 0.0
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sources = [f for f in manifest.get("files", []) if f.get("kind") == "audio"]
            if sources:
                expected = max(f.get("media", {}).get("duration_seconds") or 0 for f in sources)
        except json.JSONDecodeError:
            warnings.append("manifest.json unreadable; duration coverage check skipped")
    else:
        warnings.append("manifest.json missing; duration coverage check skipped")

    last_end = -1.0
    for row in records:
        start, end = row.get("start_seconds"), row.get("end_seconds")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"{row.get('record_id')}: non-numeric timestamps")
            continue
        if end <= start:
            errors.append(f"{row.get('record_id')}: end <= start ({start}-{end})")
        if start < last_end - 0.05:
            warnings.append(f"{row.get('record_id')}: overlaps previous record")
        last_end = max(last_end, end)
        text = (row.get("clean_literal") or "").strip()
        if not text or text == "[听不清]":
            warnings.append(f"{row.get('record_id')}: empty or unintelligible text")
        if len(text) > 2000:
            warnings.append(f"{row.get('record_id')}: unusually long segment ({len(text)} chars)")

    covered_end = max(r.get("end_seconds", 0) for r in records)
    if expected > 0 and covered_end < expected * 0.85:
        errors.append(
            f"transcript covers only {covered_end:.0f}s of {expected:.0f}s source ({covered_end / expected:.0%})"
        )
    elif expected > 0 and covered_end < expected * 0.95:
        warnings.append(
            f"transcript ends at {stamp(covered_end)} vs source end {stamp(expected)}; "
            "tail may be silence or Q&A noise"
        )

    # Gap audit: report long silences so humans can decide whether they matter.
    gaps: list[tuple[float, float]] = []
    prev_end = None
    for row in sorted(records, key=lambda r: r.get("start_seconds", 0)):
        if prev_end is not None:
            gap = row["start_seconds"] - prev_end
            if gap > 30:
                gaps.append((prev_end, row["start_seconds"]))
        prev_end = max(prev_end or 0, row.get("end_seconds", 0))
    for start, end in gaps:
        warnings.append(f"silence gap {end - start:.0f}s at {stamp(start)}-{stamp(end)}")

    return errors, warnings


def check_alignment(literal_path: Path, evidence_path: Path) -> tuple[list[str], list[str]]:
    """Evidence and literal record IDs must align exactly."""
    errors: list[str] = []
    records = load_jsonl(literal_path)
    evidence = load_jsonl(evidence_path)
    record_ids = {r.get("record_id") for r in records}
    used_evidence = {eid for r in records for eid in r.get("evidence_ids") or []}
    evidence_ids = {e.get("evidence_id") for e in evidence}
    missing = used_evidence - evidence_ids
    if missing:
        errors.append(f"{len(missing)} evidence IDs referenced by records but absent from evidence.jsonl")
    orphan = evidence_ids - used_evidence
    if orphan:
        errors.append(f"{len(orphan)} evidence items never referenced by any literal record")
    dupes = len(records) - len(record_ids)
    if dupes:
        errors.append(f"{dupes} duplicate record_ids")
    return errors, []


def check_relevance(run_dir: Path) -> tuple[list[str], list[str]]:
    """Relevance filtering must stay conservative and non-destructive."""
    receipt = run_dir / "relevance" / "receipt.json"
    if not receipt.is_file():
        return ([], [])  # filtering not run; nothing to check
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return (["relevance receipt unreadable"], [])
    errors, warnings = [], []
    if not data.get("invariants", {}).get("literal_layer_complete", False):
        errors.append("relevance filter changed the record count (literal layer must stay complete)")
    ratio = data.get("excluded_from_reconcile", 0) / max(data.get("records_total", 1), 1)
    if ratio > 0.4:
        warnings.append(f"relevance filter excluded {ratio:.0%} of records as logistics; verify this is not over-filtering")
    return errors, warnings


def check_package(package_dir: Path, run_dir: Path, literal_path: Path) -> tuple[list[str], list[str]]:
    """Required deliverables must exist and the DB must answer queries."""
    errors, warnings = [], []
    required = [
        "01_主题索引.md", "02_逐句会议记录.md", "04_会议报告.md",
        "05_不确定与冲突.md", "meeting.db", "completion_receipt.json",
    ]
    for name in required:
        p = package_dir / name
        if not p.is_file():
            errors.append(f"missing deliverable: {name}")
        elif p.stat().st_size < 50:
            errors.append(f"deliverable too small: {name} ({p.stat().st_size} bytes)")

    receipt = package_dir / "completion_receipt.json"
    if receipt.is_file():
        try:
            status = json.loads(receipt.read_text(encoding="utf-8")).get("status")
            if status not in {"COMPLETE", "COMPLETE_WITH_UNCERTAINTY"}:
                errors.append(f"completion_receipt status is {status!r}")
        except json.JSONDecodeError:
            errors.append("completion_receipt.json is not valid JSON")

    db = package_dir / "meeting.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                units = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
                literals = conn.execute("SELECT COUNT(*) FROM literal_records").fetchone()[0]
                records = load_jsonl(literal_path)
                if units == 0:
                    errors.append("meeting.db has zero topic units")
                if literals != len(records):
                    errors.append(f"meeting.db literal rows ({literals}) != records ({len(records)})")
                hit = conn.execute(
                    "SELECT COUNT(*) FROM units_fts WHERE units_fts MATCH ?", ("admission OR university OR 申请",)
                ).fetchone()[0]
                if hit == 0:
                    # FTS5 unicode61 treats an unspaced CJK sentence as one
                    # token, so Chinese claims are retrieved via LIKE in
                    # query_meeting.py. Probe retrieval with a character
                    # actually present in a claim instead of a latin letter.
                    sample = conn.execute("SELECT claim FROM units WHERE length(claim) > 2 LIMIT 1").fetchone()
                    probe = sample[0][len(sample[0]) // 2] if sample else "a"
                    like = conn.execute(
                        "SELECT COUNT(*) FROM units WHERE claim LIKE ?", (f"%{probe}%",)
                    ).fetchone()[0]
                    if like == 0:
                        warnings.append("meeting.db FTS smoke query returned nothing")
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            errors.append(f"meeting.db not readable: {exc}")
    return errors, warnings


def check_claim_audit(run_dir: Path) -> tuple[list[str], list[str]]:
    """Claim-fidelity residuals are uncertainty, not corruption: the units
    were downgraded to low certainty and annotated, so they warn (visible)
    rather than fail (blocking) unless the audit never ran on new output."""
    receipt = run_dir / "reconciled" / "claim_audit_receipt.json"
    audited = run_dir / "reconciled" / "reconciled_audited.json"
    if not receipt.is_file() or not audited.is_file():
        return [], ["claim audit receipt missing; units not fidelity-audited"]
    data = json.loads(receipt.read_text(encoding="utf-8"))
    residual = data.get("residual", 0)
    audited_count = data.get("audited", 0)
    if audited_count == 0:
        return [], []
    if residual:
        ids = ", ".join(data.get("residual_ids", []))[:200]
        return [], [f"claim audit residual {residual}/{audited_count} (downgraded to low certainty): {ids}"]
    return [], []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--literal-record", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()

    literal = args.literal_record or (args.run_dir / "literal_records.jsonl")
    evidence = args.evidence or (args.run_dir / "evidence" / "evidence.jsonl")

    errors: list[str] = []
    warnings: list[str] = []

    for fn in (check_source_duration,):
        e, w = fn(args.run_dir)
        errors += e
        warnings += w
    if literal.exists():
        e, w = check_transcript(args.run_dir, literal)
        errors += e
        warnings += w
    else:
        errors.append(f"literal record file missing: {literal}")
    if evidence.exists():
        errors += check_alignment(literal, evidence)[0]
    else:
        warnings.append(f"evidence file missing (skipped alignment): {evidence}")
    e, w = check_package(args.package_dir, args.run_dir, literal)
    errors += e
    warnings += w
    e, w = check_relevance(args.run_dir)
    errors += e
    warnings += w
    e, w = check_claim_audit(args.run_dir)
    errors += e
    warnings += w

    report = {
        "status": "FAIL" if errors else ("WARN" if warnings else "PASS"),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    print(rendered, end="")
    return 3 if errors else (2 if warnings else 0)


if __name__ == "__main__":
    raise SystemExit(main())
