"""Routing-table tests for _decide_track_engine (per-track ASR arbitration).

The three original scenarios were verified against real lecture artifacts:
  zh audio whisper-hallucinated English  -> qwen (short-circuit)
  en audio whisper transcribed fine      -> whisper
  no qwen track at all                   -> whisper fallback (no loss)

The local-LLM arbiter is stubbed — its live behavior was verified in the
2026-09-13 runs; here we verify the ROUTING around it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
os.environ.setdefault("MST_WHISPER_BIN", "/dummy/whisper-cli")
os.environ.setdefault("MST_WHISPER_MODEL", "/dummy/model.bin")
os.environ.setdefault("MST_QWEN_PYTHON", "/dummy/python")

_spec = importlib.util.spec_from_file_location("run_meeting", WS / "run_meeting.py")
rm = importlib.util.module_from_spec(_spec)
sys.modules["run_meeting"] = rm
_spec.loader.exec_module(rm)


def make_whisper_json(tmp: Path, tag: str, segments: list[str]) -> list[Path]:
    p = tmp / f"whisper_{tag}.json"
    p.write_text(json.dumps(
        {"transcription": [{"text": t} for t in segments]}, ensure_ascii=False),
        encoding="utf-8")
    return [p]


ZH = "大家好，今天讲三件事。第一，图书馆开放时间延长。第二，机器人社招新。"
EN = "Hello everyone. The partner school confirmed the exchange visit for November."


class TestTrackEngineRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_whisper_agrees_zh_audio(self):
        # whisper produced zh -> trust qwen for zh quality
        paths = make_whisper_json(self.tmp_path, "F000001", [ZH])
        got = rm._decide_track_engine("F000001", paths, {"F000001": ZH},
                                      {"F000001": ZH}, qwen_available=True)
        self.assertEqual(got, "qwen")

    def test_conflict_with_loop_short_circuits_to_qwen(self):
        # zh audio, whisper hallucinated a "Thank you." loop, qwen produced zh
        paths = make_whisper_json(self.tmp_path, "F000001", ["Thank you."] * 6)
        called = []
        rm._arbitrate_track_engine = lambda *a, **k: called.append(1) or "whisper"
        got = rm._decide_track_engine("F000001", paths,
                                      {"F000001": "Thank you. " * 6},
                                      {"F000001": ZH}, qwen_available=True)
        self.assertEqual(got, "qwen")
        self.assertEqual(called, [])  # short-circuit: no LLM needed

    def test_conflict_diverse_goes_to_arbiter(self):
        # en audio transliterated? whisper diverse en + qwen zh -> arbiter decides
        segs = [EN, "The robotics club will host a joint workshop on campus.",
                "Twelve students and two teachers will visit in November.",
                "The remaining budget is four thousand two hundred yuan."]
        paths = make_whisper_json(self.tmp_path, "F000001", segs)
        rm._arbitrate_track_engine = lambda *a, **k: "whisper"
        got = rm._decide_track_engine("F000001", paths, {"F000001": " ".join(segs)},
                                      {"F000001": ZH}, qwen_available=True)
        self.assertEqual(got, "whisper")

    def test_both_latin_prefers_whisper(self):
        paths = make_whisper_json(self.tmp_path, "F000001", [EN])
        got = rm._decide_track_engine("F000001", paths, {"F000001": EN},
                                      {"F000001": EN}, qwen_available=True)
        self.assertEqual(got, "whisper")

    def test_no_qwen_track_whisper_stays(self):
        # whole-track-loss guard: qwen never produced windows for this tag
        paths = make_whisper_json(self.tmp_path, "F000001", [EN])
        got = rm._decide_track_engine("F000001", paths, {"F000001": EN},
                                      {}, qwen_available=True)
        self.assertEqual(got, "whisper")

    def test_qwen_unavailable_falls_back_to_whisper(self):
        paths = make_whisper_json(self.tmp_path, "F000001", [ZH])
        got = rm._decide_track_engine("F000001", paths, {"F000001": ZH},
                                      {}, qwen_available=False)
        self.assertEqual(got, "whisper")


if __name__ == "__main__":
    unittest.main()
