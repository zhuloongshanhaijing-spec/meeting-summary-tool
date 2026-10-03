"""Tests for the synthetic meeting-video fixture generator (tests/synth_video.py).

Runs green under system python3 (cv2-dependent cases skip) and fully under
vendor/tools-venv/bin/python. Videos are kept tiny (640x400, 5fps, ~6s) so
the suite stays fast.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
for _p in (str(WS), str(WS / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False
_HAS_FFMPEG = shutil.which("ffmpeg") is not None

import synth_video  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(_HAS_CV2 and _HAS_FFMPEG,
                     "requires numpy+cv2 (vendor/tools-venv) and ffmpeg on PATH")
class TestSynthVideo(unittest.TestCase):
    def _gen(self, tmp: Path, name: str, **kwargs) -> dict:
        params = {"duration_s": 6.0, "fps": 5, "size": (640, 400)}
        params.update(kwargs)
        return synth_video.generate_meeting_video(tmp / name, **params)

    def test_silent_video_streams_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "silent.mp4")
            info = synth_video.probe(meta["path"])
            self.assertTrue(info["has_video"])
            self.assertFalse(info["has_audio"])
            self.assertAlmostEqual(info["duration"], 6.0, delta=0.4)
            self.assertEqual((info["width"], info["height"]), (640, 400))

    def test_audio_variant_has_audio_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "voiced.mp4", audio=True)
            info = synth_video.probe(meta["path"])
            self.assertTrue(info["has_video"])
            self.assertTrue(info["has_audio"])

    def test_same_seed_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._gen(Path(tmp), "a.mp4", seed=99)
            b = self._gen(Path(tmp), "b.mp4", seed=99)
            self.assertEqual(_sha256(Path(a["path"])), _sha256(Path(b["path"])))

    def test_default_schedule_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "sched.mp4")
            # 5 segments over 4 unique pages; page index 1 revisited once.
            self.assertEqual(len(meta["segments"]), 5)
            self.assertEqual(len(meta["unique_pages"]), 4)
            page1 = next(p for p in meta["unique_pages"] if p["page"] == 1)
            self.assertEqual(len(page1["ranges"]), 2)
            # Segments tile the whole duration without gaps.
            self.assertAlmostEqual(meta["segments"][0]["start"], 0.0)
            self.assertAlmostEqual(meta["segments"][-1]["end"], meta["duration_s"])
            for left, right in zip(meta["segments"], meta["segments"][1:]):
                self.assertAlmostEqual(left["end"], right["start"])

    def test_rendered_frames_match_schedule(self):
        """Frame-level ground truth: decoded frames must show the page metadata claims.

        Regression for the last-frame selection bug (the final frame used to
        render page 0 while metadata claimed the last page): classify the PPT
        background patch of every decoded frame to the nearest palette color.
        """
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "frames.mp4")
            w, h = meta["size"]
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", meta["path"],
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                capture_output=True, check=True).stdout
            frame_bytes = w * h * 3
            n_frames = len(raw) // frame_bytes
            self.assertGreaterEqual(n_frames, int(meta["duration_s"] * meta["fps"]))
            x, y, _bw, _bh = meta["ppt_rect_px"]
            colors = np.array(synth_video._PAGE_COLORS, dtype=int)
            for idx in range(n_frames):
                t = idx / meta["fps"]
                expected = meta["segments"][-1]["page"]
                for seg in meta["segments"]:
                    if seg["start"] <= t < seg["end"]:
                        expected = seg["page"]
                        break
                frame = np.frombuffer(raw[idx * frame_bytes:(idx + 1) * frame_bytes],
                                      dtype=np.uint8).reshape(h, w, 3)
                # Top-left patch of the PPT area: above the title baseline, left
                # of the shapes, never touched by the mouse sweep trajectory.
                patch = frame[y + 2:y + 10, x + 2:x + 30].reshape(-1, 3).astype(int)
                median = np.median(patch, axis=0)
                nearest = int(np.argmin(np.abs(colors - median).sum(axis=1)))
                self.assertEqual(nearest, expected,
                                 f"frame {idx} (t={t:.2f}s) renders page {nearest}, metadata claims {expected}")

    def test_all_motion_variant_has_no_ppt_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "motion.mp4", ppt_rect=None)
            self.assertIsNone(meta["ppt_rect_px"])
            info = synth_video.probe(meta["path"])
            self.assertTrue(info["has_video"])
            self.assertAlmostEqual(info["duration"], 6.0, delta=0.4)

    def test_region_boxes_are_inside_frame_and_even(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._gen(Path(tmp), "boxes.mp4")
            w, h = meta["size"]
            for key in ("ppt_rect_px", "camera_rect_px", "chat_rect_px"):
                x, y, bw, bh = meta[key]
                self.assertEqual(x % 2, 0)
                self.assertEqual(y % 2, 0)
                self.assertEqual(bw % 2, 0)
                self.assertEqual(bh % 2, 0)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + bw, w)
                self.assertLessEqual(y + bh, h)


if __name__ == "__main__":
    unittest.main()
