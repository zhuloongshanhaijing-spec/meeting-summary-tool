"""Direct tests of _whisper_hallucination_score — the short-circuit that
routes obvious whisper failure modes to Qwen without an LLM call.

Verified against real artifacts (2026-09-13 lectures): all-zh audio
misdected as English produced "Thank you." loops and generic word-salad.
"""
from __future__ import annotations

import importlib.util
import os
import sys
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


class TestHallucinationScore(unittest.TestCase):
    def test_empty_is_max(self):
        self.assertEqual(rm._whisper_hallucination_score([]), 1.0)
        self.assertEqual(rm._whisper_hallucination_score(["", "  "]), 1.0)

    def test_thank_you_loop(self):
        # real F000002 artifact shape
        score = rm._whisper_hallucination_score(["Thank you."] * 8)
        self.assertGreaterEqual(score, 0.5)
        self.assertEqual(score, 1.0)  # dup=1.0

    def test_generic_filler(self):
        score = rm._whisper_hallucination_score(
            ["Thank you.", "Thanks for watching.", "[Music]", "[Music]", "Amara"])
        self.assertGreaterEqual(score, 0.5)  # gen fraction dominates

    def test_diverse_english_is_clean(self):
        texts = [
            "Hello everyone and welcome to the lecture.",
            "Today we cover three administrative items.",
            "The library hours extend to nine p.m. next week.",
            "Robotics club recruitment is on October fifteenth.",
            "The remaining budget is four thousand two hundred yuan.",
        ]
        self.assertLess(rm._whisper_hallucination_score(texts), 0.5)

    def test_diverse_chinese_is_clean(self):
        texts = [
            "大家好欢迎参加本周的项目例会。",
            "第一图书馆的开放时间延长到晚上九点。",
            "第二机器人社团将在十月十五号举办招新活动。",
            "第三预算方面本学期剩余经费是四千二百元。",
        ]
        self.assertLess(rm._whisper_hallucination_score(texts), 0.5)


if __name__ == "__main__":
    unittest.main()
