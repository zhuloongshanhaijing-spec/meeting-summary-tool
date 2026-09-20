"""Wire-protocol tests against a live server subprocess — the layer the
simulated client missed when VS Code 1.137 rejected our fixed version
string (fixed 2026-09-20: initialize must ECHO the client's requested
protocolVersion, not advertise ours).
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
SERVER = WS / "core" / "scripts" / "mcp_meeting_server.py"


class ServerProc:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


class TestMcpProtocol(unittest.TestCase):
    def setUp(self):
        self.srv = ServerProc()
        self.addCleanup(self.srv.close)

    def test_initialize_echoes_client_version(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            resp = self.srv.request("initialize", {
                "protocolVersion": version, "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}})
            self.assertIsNone(resp.get("error"), resp)
            self.assertEqual(resp["result"]["protocolVersion"], version)

    def test_ping_returns_empty_object(self):
        resp = self.srv.request("ping")
        self.assertEqual(resp.get("result"), {})

    def test_unknown_tool_is_method_param_error(self):
        resp = self.srv.request("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method_is_error(self):
        resp = self.srv.request("resources/list")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_notification_produces_no_response(self):
        # a notification MUST NOT get a response object; the next response
        # line therefore belongs to the ping sent after it
        self.srv.notify("notifications/initialized")
        self.srv.notify("some/unknown/notification")
        resp = self.srv.request("ping")
        self.assertEqual(resp.get("result"), {})

    def test_tools_list_shape(self):
        resp = self.srv.request("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["list_events", "search_meetings",
                                 "get_transcript_context", "get_uncertainties"])


if __name__ == "__main__":
    unittest.main()
