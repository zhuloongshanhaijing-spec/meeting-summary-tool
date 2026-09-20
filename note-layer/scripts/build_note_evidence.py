#!/usr/bin/env python3
"""Create source-linked note evidence; notes remain independent of speech."""
import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    root = Path(manifest["source_root"])
    rows = []
    for item in manifest.get("files") or []:
        if item.get("kind") != "note" or Path(item.get("relative_path", "")).suffix.lower() != ".md":
            continue
        path = root / item["relative_path"]
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            rows.append({"note_evidence_id": f"N{len(rows) + 1:06d}", "source_id": item["source_id"],
                         "kind": "note", "locator": {"path": item["relative_path"], "line_start": number, "line_end": number},
                         "literal_text": text, "uncertainty": "note_is_independent_source_not_speech"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.receipt.write_text(json.dumps({"status": "complete", "note_evidence_count": len(rows), "content_included": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
