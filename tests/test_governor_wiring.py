"""C2 wiring: run_meeting gates heavy stages through the resource governor
and surfaces governor_wait/governor_degrade in progress.jsonl."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
import run_meeting  # noqa: E402


class FakeGovernor:
    def __init__(self, gate_result=None, degrade=None, bypass=False):
        self._gate = gate_result or {"waited_s": 0, "zone": "green", "action": "pass"}
        self._degrade = degrade or {}
        self.bypass = bypass
        self.calls = []

    def gate(self, label):
        self.calls.append(label)
        return dict(self._gate)

    def degrade_env(self):
        return dict(self._degrade)


class GovernorGateTest(unittest.TestCase):
    def setUp(self):
        self._old = (run_meeting._GOVERNOR, run_meeting._GOVERNOR_INIT)
        run_meeting._GOVERNOR = None
        run_meeting._GOVERNOR_INIT = True  # skip lazy loader; inject directly

    def tearDown(self):
        run_meeting._GOVERNOR, run_meeting._GOVERNOR_INIT = self._old

    def test_wait_and_degrade_are_recorded_in_progress_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "runs").mkdir()
            gov = FakeGovernor(
                gate_result={"waited_s": 3.2, "zone": "red", "action": "resume"},
                degrade={"MST_WHISPER_THREADS": "2"})
            with mock.patch.object(run_meeting, "WORKSPACE", root), \
                 mock.patch.dict("os.environ", {}, clear=False):
                run_meeting._GOVERNOR = gov
                run_meeting.governor_gate("测试事件", "asr")
                import os
                self.assertEqual(os.environ.get("MST_WHISPER_THREADS"), "2")
                lines = [json.loads(x) for x in
                         (root / "runs" / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
                kinds = [x["kind"] for x in lines]
                self.assertIn("governor_wait", kinds)
                self.assertIn("governor_degrade", kinds)
                wait = next(x for x in lines if x["kind"] == "governor_wait")
                self.assertEqual(wait["stage"], "asr")
                self.assertEqual(wait["counters"]["zone"], "red")
                self.assertEqual(lines[-1]["counters"]["MST_WHISPER_THREADS"], "2")

    def test_green_zone_emits_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "runs").mkdir()
            gov = FakeGovernor()  # waited 0, no degrade
            with mock.patch.object(run_meeting, "WORKSPACE", root):
                run_meeting._GOVERNOR = gov
                run_meeting.governor_gate("测试事件", "audit")
            p = root / "runs" / "progress.jsonl"
            self.assertFalse(p.exists())

    def test_off_bypass_records_nothing_and_never_imports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "runs").mkdir()
            gov = FakeGovernor(bypass=True)
            with mock.patch.object(run_meeting, "WORKSPACE", root):
                run_meeting._GOVERNOR = gov
                run_meeting.governor_gate("测试事件", "reconcile")
            self.assertFalse((root / "runs" / "progress.jsonl").exists())

    def test_whisper_threads_arg_reads_env(self):
        import os
        os.environ["MST_WHISPER_THREADS"] = "3"
        try:
            self.assertEqual(run_meeting._whisper_threads_arg(), ["-t", "3"])
        finally:
            del os.environ["MST_WHISPER_THREADS"]
        self.assertEqual(run_meeting._whisper_threads_arg(), [])

    def test_missing_module_disables_gating_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "webapp").mkdir()  # no resource_governor.py inside
            with mock.patch.object(run_meeting, "WORKSPACE", root):
                run_meeting._GOVERNOR_INIT = False
                run_meeting._GOVERNOR = None
                try:
                    run_meeting.governor_gate("测试事件", "asr")  # must not raise
                finally:
                    run_meeting._GOVERNOR_INIT = True


if __name__ == "__main__":
    unittest.main()
