"""Progress-hook contract tests (docs/web-console-design.md §4).

Pins the schema shared between run_meeting.py (producer) and
webapp/server.py (consumer): .progress.json snapshot fields, progress.jsonl
line shape, sub-stage index mapping, best-effort failure semantics, and the
.mst-output.json output-dir override.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]

import run_meeting


class EmitProgressTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_jsonl_and_snapshot_schema(self):
        with mock.patch.object(run_meeting, "WORKSPACE", self.workspace):
            run_meeting.emit_progress("ev1", "event_start", message="开始")
            run_meeting.emit_progress("ev1", "stage", "inventory", "文件清单")
            run_meeting.emit_progress("ev1", "stage", "asr.qwen", "Qwen 窗口转录")
        lines = (self.workspace / "runs" / "progress.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            row = json.loads(line)
            for key in ("ts", "event", "kind", "stage", "message", "status", "counters"):
                self.assertIn(key, row)
        snap = json.loads((self.workspace / "runs" / "ev1" / ".progress.json")
                          .read_text(encoding="utf-8"))
        for key in ("event", "status", "stage", "stage_index", "stage_total",
                    "stage_started", "message", "updated"):
            self.assertIn(key, snap)
        self.assertEqual(snap["stage"], "asr.qwen")
        self.assertEqual(snap["stage_total"], len(run_meeting.STAGE_ORDER))

    def test_substage_maps_to_parent_index(self):
        # "asr" is the 5th step of the 14-station table (screen-recording
        # design §3.4); sub-stages must not invent new positions
        self.assertEqual(run_meeting._stage_index("asr.qwen"), 5)
        self.assertEqual(run_meeting._stage_index("asr.whisper"), 5)
        self.assertEqual(run_meeting._stage_index("inventory"), 1)
        self.assertEqual(run_meeting._stage_index("video_ingest.ocr"), 2)
        self.assertEqual(run_meeting._stage_index("slide_align"), 9)
        self.assertEqual(run_meeting._stage_index(""), 0)

    def test_stage_started_advances_on_stage_change(self):
        clock = iter([100.0, 100.5, 101.0])  # deterministic: real clock can tick together
        with mock.patch.object(run_meeting.time, "time", lambda: next(clock)):
            with mock.patch.object(run_meeting, "WORKSPACE", self.workspace):
                run_meeting.emit_progress("ev2", "stage", "inventory", "a")
                run_meeting.emit_progress("ev2", "stage", "inventory", "a2")
                run_meeting.emit_progress("ev2", "stage", "audio_prepare", "b")
        rows = [json.loads(l) for l in (self.workspace / "runs" / "progress.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["ts"] for r in rows], [100.0, 100.5, 101.0])

    def test_never_raises_on_unwritable_workspace(self):
        # observability must not break the pipeline: WORKSPACE pointing at a
        # regular file makes mkdir fail; emit_progress swallows it
        with tempfile.NamedTemporaryFile() as tf:
            broken = Path(tf.name)
            with mock.patch.object(run_meeting, "WORKSPACE", broken):
                run_meeting.emit_progress("ev3", "event_start", message="x")  # no raise


class ResolveOutputDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.event_dir = self.workspace / "input" / "ev"
        self.event_dir.mkdir(parents=True)
        self.event = {"name": "ev", "dir": self.event_dir}

    def test_default(self):
        with mock.patch.object(run_meeting, "WORKSPACE", self.workspace):
            self.assertEqual(run_meeting.resolve_output_dir(self.event),
                             self.workspace / "outputs" / "ev")

    def test_override(self):
        target = self.workspace / "somewhere-else"
        (self.event_dir / ".mst-output.json").write_text(
            json.dumps({"output_dir": str(target)}), encoding="utf-8")
        with mock.patch.object(run_meeting, "WORKSPACE", self.workspace):
            self.assertEqual(run_meeting.resolve_output_dir(self.event), target)

    def test_malformed_override_falls_back(self):
        (self.event_dir / ".mst-output.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(run_meeting, "WORKSPACE", self.workspace):
            self.assertEqual(run_meeting.resolve_output_dir(self.event),
                             self.workspace / "outputs" / "ev")


class StageOrderContractTest(unittest.TestCase):
    def test_fourteen_unique_keys(self):
        keys = [k for k, _ in run_meeting.STAGE_ORDER]
        self.assertEqual(len(keys), 14)
        self.assertEqual(len(set(keys)), 14)
        # order pinned by the spec's step bar (screen-recording design §3.4:
        # video_ingest 紧随 inventory、slide_align 紧随 relevance)
        self.assertEqual(keys[:5], ["inventory", "video_ingest", "audio_prepare",
                                    "lang_probe", "asr"])
        self.assertEqual(keys[keys.index("relevance") + 1], "slide_align")
        self.assertEqual(keys[-1], "validate")


if __name__ == "__main__":
    unittest.main()
