"""`*_private` event exclusion — documented behavior that sat unimplemented
until the throwaway-event test on 2026-09-20 proved the gap. The MCP
server must never list or search events whose directory name ends with
`_private` (e.g. raw personal recordings kept for debugging).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "mcp_meeting_server", WS / "core" / "scripts" / "mcp_meeting_server.py")
mcp = importlib.util.module_from_spec(_spec)
sys.modules["mcp_meeting_server"] = mcp
_spec.loader.exec_module(mcp)


class TestPrivateExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        outputs = Path(self.tmp.name)
        for name in ("lectures-a", "lectures-b", "raw_debug_private"):
            (outputs / name).mkdir()
            (outputs / name / "meeting.db").write_bytes(b"")
        self._orig = mcp.OUTPUTS
        mcp.OUTPUTS = outputs
        self.addCleanup(setattr, mcp, "OUTPUTS", self._orig)

    def test_dbs_skips_private(self):
        names = [name for name, _ in mcp._dbs()]
        self.assertEqual(sorted(names), ["lectures-a", "lectures-b"])

    def test_list_events_hides_private(self):
        import json
        result = json.loads(mcp.tool_list_events({}))
        events = result["events"] if isinstance(result, dict) else result
        listed = [e["event"] if isinstance(e, dict) else e for e in events]
        self.assertNotIn("raw_debug_private", listed)
        self.assertIn("lectures-a", listed)


if __name__ == "__main__":
    unittest.main()
