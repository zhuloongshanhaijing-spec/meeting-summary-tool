#!/usr/bin/env python3
"""Retrieve a small evidence-linked context packet from a meeting database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT u.* FROM units_fts f JOIN units u ON u.unit_id=f.unit_id WHERE units_fts MATCH ? ORDER BY rank LIMIT ?",
            (args.query, args.limit),
        ).fetchall()
        if not rows:
            # FTS5 may treat an unspaced Chinese sentence as one token.
            like = f"%{args.query}%"
            rows = connection.execute(
                "SELECT * FROM units WHERE topic_path LIKE ? OR claim LIKE ? LIMIT ?",
                (like, like, args.limit),
            ).fetchall()
        packet = []
        for row in rows:
            unit = dict(row)
            ids = json.loads(unit.pop("evidence_ids_json"))
            marks = ",".join("?" for _ in ids)
            evidence = [dict(x) for x in connection.execute(f"SELECT evidence_id,source_id,kind,locator_json,uncertainty FROM evidence WHERE evidence_id IN ({marks})", ids)] if ids else []
            unit["evidence"] = evidence
            packet.append(unit)
        standalone_evidence = []
        if not packet:
            like = f"%{args.query}%"
            standalone_evidence = [
                dict(row) for row in connection.execute(
                    "SELECT evidence_id,source_id,kind,locator_json,literal_text,uncertainty FROM evidence WHERE literal_text LIKE ? LIMIT ?",
                    (like, args.limit),
                )
            ]
    finally:
        connection.close()
    result = {
        "query": args.query,
        "result_count": len(packet) + len(standalone_evidence),
        "units": packet,
        "standalone_evidence": standalone_evidence,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
