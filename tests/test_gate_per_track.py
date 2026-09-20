"""Per-track duration check in quality_gate — regression for the multi-track
false-positive where every shorter WAV was compared against the LONGEST
source and flagged as truncated.

Requires ffprobe on PATH (a documented pipeline dependency); WAV files
themselves are generated with stdlib `wave`, no fixtures on disk.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "quality_gate", WS / "core" / "scripts" / "quality_gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["quality_gate"] = gate
_spec.loader.exec_module(gate)


def write_wav(path: Path, seconds: float) -> None:
    rate = 8000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def make_run(tmp: Path, tracks: dict[str, tuple[float, float]]) -> Path:
    """tracks: tag -> (source_duration, actual_wav_duration)"""
    run_dir = tmp / "runs" / "event"
    audio = run_dir / "prepared" / "artifacts" / "audio" / "normalized"
    audio.mkdir(parents=True)
    manifest = {"files": [
        {"kind": "audio", "source_id": tag, "media": {"duration_seconds": src}}
        for tag, (src, _) in tracks.items()]}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for tag, (_, actual) in tracks.items():
        write_wav(audio / f"{tag}.wav", actual)
    return run_dir


class TestPerTrackDuration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_truncated_track_flags_only_itself(self):
        run_dir = make_run(self.tmp_path, {
            "F000001": (10.0, 5.0),   # 50% -> truncated
            "F000002": (10.0, 9.5),   # 95% -> fine
        })
        errors, warnings = gate.check_source_duration(run_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("normalized/F000001.wav", errors[0])
        self.assertIn("50%", errors[0])
        self.assertNotIn("F000002", errors[0])

    def test_healthy_event_is_clean(self):
        run_dir = make_run(self.tmp_path, {
            "F000001": (10.0, 9.8),
            "F000002": (8.0, 7.9),
        })
        errors, warnings = gate.check_source_duration(run_dir)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_missing_manifest_warns_not_errors(self):
        empty = self.tmp_path / "runs" / "other"
        empty.mkdir(parents=True)
        errors, warnings = gate.check_source_duration(empty)
        self.assertEqual(errors, [])
        self.assertTrue(any("manifest" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
