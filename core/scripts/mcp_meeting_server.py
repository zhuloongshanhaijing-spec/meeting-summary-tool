#!/usr/bin/env python3
"""MCP server exposing processed meetings to any AI client (Claude Desktop,
Cursor, agents) over the Model Context Protocol (stdio, newline-delimited
JSON-RPC 2.0, spec 2025-06-18 subset: initialize / tools/list / tools/call).

This is the delivery half of the ecosystem: the pipeline compiles
recordings + notes into evidence-linked knowledge (meeting.db), and this
server hands that knowledge to AI without the human ever reading raw
transcripts. Every tool returns claims with evidence ids and audio
timestamps so downstream AI can cite and verify instead of hallucinating.

Zero third-party dependencies (stdlib only) so it runs anywhere.

Config in Claude Desktop (claude_desktop_config.json):
  "mcpServers": {
    "meetings": {
      "command": "python3",
      "args": ["/abs/path/to/core/scripts/mcp_meeting_server.py"]
    }
  }
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUTS = WORKSPACE / "outputs"
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "list_events",
        "description": "List all processed meeting/lecture events with their database stats, "
                       "transcript coverage and quality-gate status. Start here.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_meetings",
        "description": "Full-text search across events. Returns topic units (claims) with "
                       "certainty and evidence ids; falls back to literal evidence lines "
                       "(exact transcript text with audio timestamps). Chinese and English "
                       "both work; numbers and dates are strong anchors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search phrase, any language"},
                "event": {"type": "string", "description": "optional event name to restrict search"},
                "limit": {"type": "integer", "description": "max results per event (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_transcript_context",
        "description": "Return literal transcript records around a time position or record id — "
                       "the verbatim layer behind a claim, for verification before citing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "string"},
                "record_id": {"type": "string", "description": "e.g. R000123"},
                "around_seconds": {"type": "number", "description": "alternative to record_id"},
                "context": {"type": "integer", "description": "records before/after (default 3)"},
            },
            "required": ["event"],
        },
    },
    {
        "name": "get_uncertainties",
        "description": "List uncertain/conflicting items and note-corroboration conflicts for an "
                       "event. Check this before making confident claims.",
        "inputSchema": {
            "type": "object",
            "properties": {"event": {"type": "string"}},
            "required": ["event"],
        },
    },
]


def _dbs() -> list[tuple[str, Path]]:
    found = []
    if OUTPUTS.is_dir():
        for db in sorted(OUTPUTS.glob("*/meeting.db")):
            if db.parent.name.endswith("_private"):
                continue  # documented opt-out: _private suffix excludes from retrieval
            found.append((db.parent.name, db))
    return found


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_seconds(seconds) -> str:
    try:
        s = float(seconds)
        return f"{int(s // 60):02d}:{int(s % 60):02d}"
    except (TypeError, ValueError):
        return "?"


def tool_list_events(_args: dict) -> str:
    events = []
    for name, db in _dbs():
        try:
            conn = _connect(db)
            units = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
            records = conn.execute("SELECT COUNT(*) FROM literal_records").fetchone()[0]
            gate = "unknown"
            report = db.parent / "quality_gate_report.json"
            if report.is_file():
                try:
                    gate = json.loads(report.read_text(encoding="utf-8")).get("status", "unknown")
                except json.JSONDecodeError:
                    pass
            duration = conn.execute("SELECT MAX(end_seconds) FROM literal_records").fetchone()[0]
            conn.close()
            events.append({
                "event": name, "topic_units": units, "literal_records": records,
                "duration": _fmt_seconds(duration), "quality_gate": gate,
            })
        except sqlite3.DatabaseError as exc:
            events.append({"event": name, "error": str(exc)})
    return json.dumps({"events": events}, ensure_ascii=False, indent=1)


def _search_token(conn: sqlite3.Connection, token: str, limit: int) -> tuple[list, list]:
    """Three-tier recall for ONE token: FTS MATCH -> claim LIKE -> evidence LIKE."""
    units: list = []
    evidence: list = []
    try:
        rows = conn.execute(
            "SELECT u.unit_id, u.topic_path, u.claim, u.certainty, u.evidence_ids_json "
            "FROM units_fts f JOIN units u ON u.unit_id = f.unit_id "
            "WHERE units_fts MATCH ? ORDER BY rank LIMIT ?",
            (token, limit)).fetchall()
        for row in rows:
            units.append({
                "unit_id": row["unit_id"], "topic": row["topic_path"],
                "claim": row["claim"], "certainty": row["certainty"],
                "evidence_ids": json.loads(row["evidence_ids_json"]),
            })
    except sqlite3.OperationalError:
        pass
    if not units:
        like = f"%{token}%"
        # short CJK tokens cannot MATCH under trigram; check claims directly
        for row in conn.execute(
            "SELECT unit_id, topic_path, claim, certainty, evidence_ids_json "
            "FROM units WHERE claim LIKE ? OR topic_path LIKE ? LIMIT ?",
            (like, like, limit)):
            units.append({
                "unit_id": row["unit_id"], "topic": row["topic_path"],
                "claim": row["claim"], "certainty": row["certainty"],
                "evidence_ids": json.loads(row["evidence_ids_json"]),
            })
    if not units:
        like = f"%{token}%"
        for row in conn.execute(
            "SELECT record_id, start_seconds, end_seconds, clean_literal "
            "FROM literal_records WHERE clean_literal LIKE ? LIMIT ?", (like, limit)):
            evidence.append({
                "record_id": row["record_id"],
                "at": f"{_fmt_seconds(row['start_seconds'])}-{_fmt_seconds(row['end_seconds'])}",
                "text": row["clean_literal"],
            })
    return units, evidence


def _cjk_shingles(query: str) -> list[str]:
    """Bigram shingles of CJK runs — recall for natural no-space queries.

    '机器人社招新是什么时候' is not a substring of any claim, and has no
    whitespace to split into tokens; bigrams (机器/人社/招新/…) retrieve
    by coverage the units that actually answer the question.
    """
    shingles: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        for i in range(len(run) - 1):
            shingles.add(run[i:i + 2])
    return sorted(shingles)


def _search_one(name: str, db: Path, query: str, limit: int) -> dict:
    out = {"event": name, "units": [], "evidence": []}
    conn = _connect(db)
    try:
        units, evidence = _search_token(conn, query, limit)
        tokens = query.split()
        if not units and not evidence and len(tokens) <= 1 and _cjk_shingles(query):
            tokens = _cjk_shingles(query)  # no-space CJK question: shingle recall
        if not units and not evidence and len(tokens) > 1:
            # multi-token AND semantics starve real questions ("社团 招新" where
            # the corpus says 招聘): fall back to per-token recall merged by
            # how many tokens each hit matches
            unit_merges: dict[str, dict] = {}
            ev_merges: dict[str, dict] = {}
            for token in tokens:
                tu, te = _search_token(conn, token, limit)
                for hit in tu:
                    entry = unit_merges.setdefault(hit["unit_id"], {**hit, "matched": 0})
                    entry["matched"] += 1
                for hit in te:
                    entry = ev_merges.setdefault(hit["record_id"], {**hit, "matched": 0})
                    entry["matched"] += 1
            units = sorted(unit_merges.values(), key=lambda h: -h["matched"])[:limit]
            evidence = sorted(ev_merges.values(), key=lambda h: -h["matched"])[:limit]
        out["units"] = units
        out["evidence"] = evidence
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return out


def tool_search_meetings(args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query required"})
    limit = int(args.get("limit", 5))
    only = args.get("event")
    results = [_search_one(name, db, query, limit) for name, db in _dbs()
               if only is None or name == only]
    hits = [r for r in results if r["units"] or r["evidence"]]
    return json.dumps({"query": query, "results": hits or results[:1]}, ensure_ascii=False, indent=1)


def tool_get_transcript_context(args: dict) -> str:
    name = args.get("event", "")
    target = next(((n, d) for n, d in _dbs() if n == name), None)
    if not target:
        return json.dumps({"error": f"unknown event: {name}"})
    conn = _connect(target[1])
    try:
        if args.get("record_id"):
            row = conn.execute(
                "SELECT record_id, start_seconds, end_seconds, clean_literal, relevance_json "
                "FROM literal_records WHERE record_id = ?",
                (str(args["record_id"]),)).fetchone()
        else:
            row = conn.execute(
                "SELECT record_id, start_seconds, end_seconds, clean_literal, relevance_json "
                "FROM literal_records WHERE start_seconds <= ? ORDER BY start_seconds DESC LIMIT 1",
                (float(args.get("around_seconds", 0)),)).fetchone()
        if not row:
            return json.dumps({"error": "record not found"})
        ctx = int(args.get("context", 3))
        rows = conn.execute(
            "SELECT record_id, start_seconds, end_seconds, clean_literal, relevance_json "
            "FROM literal_records WHERE record_id <= ? ORDER BY record_id DESC LIMIT ?",
            (row["record_id"], ctx + 1)).fetchall()[::-1]
        rows += conn.execute(
            "SELECT record_id, start_seconds, end_seconds, clean_literal, relevance_json "
            "FROM literal_records WHERE record_id > ? ORDER BY record_id LIMIT ?",
            (row["record_id"], ctx)).fetchall()
        lines = []
        for r in rows:
            mark = "→" if r["record_id"] == row["record_id"] else " "
            relevance = json.loads(r["relevance_json"]) if r["relevance_json"] else {}
            tag = f" [{relevance.get('label')}]" if relevance.get("label") in ("logistics", "uncertain") else ""
            lines.append(f"{mark} {r['record_id']} [{_fmt_seconds(r['start_seconds'])}] "
                         f"{r['clean_literal']}{tag}")
        return "\n".join(lines)
    finally:
        conn.close()


def tool_get_uncertainties(args: dict) -> str:
    name = args.get("event", "")
    target = next(((n, d) for n, d in _dbs() if n == name), None)
    if not target:
        return json.dumps({"error": f"unknown event: {name}"})
    out = {"event": name, "uncertain_units": [], "note_conflicts": []}
    conn = _connect(target[1])
    try:
        for row in conn.execute(
                "SELECT unit_id, claim, certainty FROM units WHERE certainty IN ('low','medium') "
                "ORDER BY certainty LIMIT 20"):
            out["uncertain_units"].append(
                f"{row['unit_id']} [{row['certainty']}] {row['claim']}")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    doc = target[1].parent / "05_不确定与冲突.md"
    if doc.is_file():
        text = doc.read_text(encoding="utf-8")
        out["uncertainty_doc_lines"] = min(len(text.splitlines()), 200)
    notes = target[1].parent / "06_笔记佐证与冲突.md"
    if notes.is_file():
        for line in notes.read_text(encoding="utf-8").splitlines():
            if "conflict" in line.lower() or "· conflict" in line:
                out["note_conflicts"].append(line.strip()[:120])
    return json.dumps(out, ensure_ascii=False, indent=1)


HANDLERS = {
    "list_events": tool_list_events,
    "search_meetings": tool_search_meetings,
    "get_transcript_context": tool_get_transcript_context,
    "get_uncertainties": tool_get_uncertainties,
}


def handle(message: dict) -> dict | None:
    method = message.get("method", "")
    msg_id = message.get("id")
    if method == "initialize":
        # MCP version negotiation: echo the client's requested version when
        # present. VS Code 1.137 rejects a server that answers an older
        # version than it asked for ("Server's protocol version is not
        # supported: 2025-06-18" — caught live in exthost logs 2026-09-20).
        client_version = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "meeting-knowledge", "version": "1.0.0"},
        }}
    if method == "ping":
        # MCP keepalive: modern clients (VS Code gateway) ping and treat a
        # missing answer as an unhealthy server
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "notifications/initialized":
        return None  # notification: no response
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params", {})
        handler = HANDLERS.get(params.get("name", ""))
        if handler is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": f"unknown tool: {params.get('name')}"}}
        text = handler(params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text}]}}
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}}
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
