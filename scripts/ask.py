#!/usr/bin/env python3
"""Ask a question against the compiled knowledge base through the MCP server.

Zero-dependency stdio JSON-RPC client — the same protocol any MCP-capable
chat client (VS Code / Roo-Code / Claude Desktop) speaks. Prints every hit
with its claim, certainty, evidence IDs and timestamps so you can verify
answers against the verbatim record.

Usage:
  python3 scripts/ask.py "机器人社招新是什么时候"
  python3 scripts/ask.py "robotics club" --event demo-event --limit 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "core" / "scripts" / "mcp_meeting_server.py"


class McpClient:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed unexpectedly")
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, name: str, arguments: dict) -> str:
        resp = self.call("tools/call", {"name": name, "arguments": arguments})
        return "\n".join(c.get("text", "") for c in resp.get("result", {}).get("content", []))

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask the meeting knowledge base via MCP")
    ap.add_argument("query", help="中英文皆可（跨语言检索能力见 README 已知限制）")
    ap.add_argument("--event", help="限定事件（默认全部事件）")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    client = McpClient()
    client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "ask-cli", "version": "1.0"}})
    client.notify("notifications/initialized")

    kwargs: dict = {"query": args.query, "limit": args.limit}
    if args.event:
        kwargs["event"] = args.event
    raw = client.tool("search_meetings", kwargs)
    client.close()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return 1

    total = 0
    for ev in data.get("results", []):
        units, evidence = ev.get("units", []), ev.get("evidence", [])
        if not units and not evidence:
            continue
        print(f"\n◆ 事件 {ev['event']}")
        for u in units:
            print(f"  [{u['unit_id']}] ({u['certainty']}) {u['claim']}")
            print(f"        证据: {', '.join(u.get('evidence_ids', []))}")
            total += 1
        for e in evidence:
            print(f"  [{e['record_id']}] {e['at']}  {e['text'][:80]}")
            total += 1
    if not total:
        print("（无命中——换词试试，或先跑 scripts/demo.sh 编译事件）")
    print(f"\n共 {total} 条可回查结果")
    return 0


if __name__ == "__main__":
    sys.exit(main())
