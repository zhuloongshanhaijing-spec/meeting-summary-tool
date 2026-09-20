# MCP setup — connect your AI chat client

The compiled knowledge package is served by a **zero-dependency stdio MCP
server**: `core/scripts/mcp_meeting_server.py`. It speaks the MCP
2025-06-18 tool subset and echoes the client's requested protocol version
(older clients negotiate down automatically). It reads only `outputs/`.

> **Pitfall:** GUI apps often lack your shell `PATH`. Always use an
> **absolute** python path in the configs below (find yours with
> `python3 -c "import sys; print(sys.executable)"`).

## Tools

| tool | arguments | returns |
|---|---|---|
| `list_events` | — | events (dirs in `outputs/`; `*_private` excluded) |
| `search_meetings` | `query` (required), `event`, `limit` | matching units with evidence IDs + verbatim sentences with timestamps |
| `get_transcript_context` | `event`, `record_id`, `around_seconds`, `context` | surrounding transcript records |
| `get_uncertainties` | `event` | low-certainty units + unresolved note relations |

Search recall is three-tier per token (FTS5 trigram → claim LIKE →
evidence LIKE), multi-token queries merge by coverage, and natural
no-space Chinese questions fall back to CJK bigram shingle recall —
`机器人社招新是什么时候` finds the unit about the robotics club.

## VS Code (native MCP support)

`~/Library/Application Support/Code/User/mcp.json` (macOS):

```json
{
  "servers": {
    "meetings": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/repo/core/scripts/mcp_meeting_server.py"]
    }
  }
}
```

## Roo-Code / Cline

Roo's MCP settings file, with read-only auto-approve:

```json
{
  "mcpServers": {
    "meetings": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/repo/core/scripts/mcp_meeting_server.py"],
      "autoApprove": ["list_events", "search_meetings",
                      "get_transcript_context", "get_uncertainties"],
      "disabled": false
    }
  }
}
```

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "meetings": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/repo/core/scripts/mcp_meeting_server.py"]
    }
  }
}
```

## Terminal (no client needed)

```bash
python3 scripts/ask.py "预算还剩多少" --event my-event
```

## Answer discipline for clients

Tell your AI (system prompt or custom instruction) to: answer from the
returned units, quote the evidence ID and timestamp, mark inferences as
inferences, and use `get_uncertainties` before asserting shaky facts.
The `U → A → R` chain is the whole point — verify one answer against
`02_逐句会议记录.md` occasionally.
