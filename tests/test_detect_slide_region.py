"""Tests for core/scripts/detect_slide_region.py (design spec §3.1).

Runs green under system python3 (cv2-dependent cases skip) and fully under
vendor/tools-venv/bin/python. The detector is always invoked as a SUBPROCESS
via sys.executable (under the tools venv that is exactly the python with
numpy+cv2). Fixture videos (meeting window 640x400 + 1920x1080, all-motion,
dense-single-slide) are generated once in setUpModule into a shared
TemporaryDirectory; detection runs reused by several tests are done there too,
so the suite stays fast.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
for _p in (str(WS), str(WS / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import cv2
    import numpy  # noqa: F401
    _HAS_CV2 = True
except ImportError:  # system python3 without the tools venv
    cv2 = None
    _HAS_CV2 = False

import synth_video  # noqa: E402

SCRIPT = WS / "core" / "scripts" / "detect_slide_region.py"
VIDEO_PARAMS = {"duration_s": 8.0, "fps": 5, "size": (640, 400)}
HD_SIZE = (1920, 1080)
DEFAULT_MAX_FRAMES = 20
DEFAULT_MIN_CONFIDENCE = 0.55
# Shipped-behavior pins for the default 640x400 fixture (T2 review): the
# detection numbers must not drift across algorithm fixes.
SHIPPED_640_RECT = {"x": 0.053125, "y": 0.055, "w": 0.610938, "h": 0.8875}
SHIPPED_640_CONFIDENCE = 0.734

_TMP: tempfile.TemporaryDirectory | None = None
_MEETING_META: dict | None = None
_MOTION_META: dict | None = None
_HD_META: dict | None = None
_MEETING_RUN: dict | None = None
_MOTION_RUN: dict | None = None
_HD_RUN: dict | None = None
_DENSE_RUN: dict | None = None


def _run_detect(video, out_dir: Path, tag: str, *extra: str) -> dict:
    """Invoke the detector subprocess; collect rc/outputs/parsed result JSON."""
    preview = out_dir / f"{tag}-preview.png"
    result = out_dir / f"{tag}-result.json"
    command = [sys.executable, str(SCRIPT),
               "--video", str(video),
               "--preview-out", str(preview),
               "--result-out", str(result), *extra]
    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    elapsed = time.monotonic() - started
    payload = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else None
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "elapsed": elapsed, "payload": payload, "preview": preview, "result": result}


def _gt_rect_rel(meta: dict) -> tuple[float, float, float, float]:
    """Ground-truth ppt_rect_px -> relative (x, y, w, h)."""
    width, height = meta["size"]
    x, y, w, h = meta["ppt_rect_px"]
    return (x / width, y / height, w / width, h / height)


def _iou(rect: dict, other: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = rect["x"], rect["y"], rect["w"], rect["h"]
    bx, by, bw, bh = other
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    intersection = ix * iy
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _dense_slide(frame, box, page: int) -> None:
    """Text-dense slide renderer (review probe recipe): full-page text lines.

    A single dense slide shown for the whole runtime is the documented
    fail-safe limitation: persistent text edges shred the slide interior,
    so detection must collapse to reliable=false (never a confident wrong
    rect, never a region file).
    """
    x, y, w, h = box
    frame[y:y + h, x:x + w] = (245, 245, 245)
    line_h = max(14, h // 22)
    yy, i = y + 2, 0
    while yy + line_h < y + h:
        text = f"Dense{page} L{i}: the quick brown fox jumps over lazy dog {i * 7919 % 97}%"
        cv2.putText(frame, text, (x + 4, yy + line_h - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (35, 35, 35), 1, cv2.LINE_AA)
        yy += line_h
        i += 1


def setUpModule() -> None:
    if not _HAS_CV2:
        return
    global _TMP, _MEETING_META, _MOTION_META, _HD_META
    global _MEETING_RUN, _MOTION_RUN, _HD_RUN, _DENSE_RUN
    _TMP = tempfile.TemporaryDirectory(prefix="detect-slide-region-")
    root = Path(_TMP.name)
    _MEETING_META = synth_video.generate_meeting_video(root / "meeting.mp4", **VIDEO_PARAMS)
    _MOTION_META = synth_video.generate_meeting_video(root / "motion.mp4", ppt_rect=None, **VIDEO_PARAMS)
    _HD_META = synth_video.generate_meeting_video(
        root / "meeting_hd.mp4", duration_s=8.0, fps=5, size=HD_SIZE, mouse_crossings=[])
    original_draw_slide = synth_video._draw_slide
    try:
        synth_video._draw_slide = _dense_slide
        synth_video.generate_meeting_video(
            root / "dense.mp4", duration_s=8.0, fps=5, size=(640, 400),
            schedule=[(0, 0.0, 8.0)], mouse_crossings=[])
    finally:
        synth_video._draw_slide = original_draw_slide
    shared = root / "shared"
    shared.mkdir()
    _MEETING_RUN = _run_detect(root / "meeting.mp4", shared, "meeting")
    _MOTION_RUN = _run_detect(root / "motion.mp4", shared, "motion")
    _HD_RUN = _run_detect(root / "meeting_hd.mp4", shared, "hd")
    _DENSE_RUN = _run_detect(root / "dense.mp4", shared, "dense",
                             "--write-region", str(shared / "dense-region.json"))


def tearDownModule() -> None:
    global _TMP, _MEETING_META, _MOTION_META, _HD_META
    global _MEETING_RUN, _MOTION_RUN, _HD_RUN, _DENSE_RUN
    if _TMP is not None:
        _TMP.cleanup()
    _TMP = None
    _MEETING_META = _MOTION_META = _HD_META = None
    _MEETING_RUN = _MOTION_RUN = _HD_RUN = _DENSE_RUN = None


@unittest.skipUnless(_HAS_CV2, "requires numpy+cv2 (vendor/tools-venv)")
class TestDetectSlideRegion(unittest.TestCase):
    def _assert_detected_well(self, run: dict, meta: dict, min_iou: float = 0.9) -> None:
        self.assertEqual(run["rc"], 0, run["stderr"])
        payload = run["payload"]
        self.assertIsNotNone(payload)
        for key in ("rect", "confidence", "reliable", "frame_count"):
            self.assertIn(key, payload)
        self.assertTrue(payload["reliable"],
                        f"expected reliable detection, got {payload}; stderr={run['stderr']}")
        self.assertIsNotNone(payload["rect"])
        self.assertGreaterEqual(payload["confidence"], DEFAULT_MIN_CONFIDENCE)
        ground_truth = _gt_rect_rel(meta)
        iou = _iou(payload["rect"], ground_truth)
        self.assertGreaterEqual(
            iou, min_iou,
            f"IoU {iou:.4f} < {min_iou}; detected={payload['rect']} ground_truth={ground_truth}")

    def test_meeting_video_reliable_with_high_iou(self):
        self._assert_detected_well(_MEETING_RUN, _MEETING_META)
        payload = _MEETING_RUN["payload"]
        # Shipped-behavior pins (T2 review): exact rect + confidence at 640x400.
        self.assertEqual(payload["rect"], SHIPPED_640_RECT)
        self.assertAlmostEqual(payload["confidence"], SHIPPED_640_CONFIDENCE, places=3)
        self.assertEqual(payload["frame_count"], DEFAULT_MAX_FRAMES)

    def test_hd_1080p_fixture_reliable_with_high_iou(self):
        # Regression pin for the moat-severance fix: at 1920x1080 the
        # persistent-edge moat used to leak through vote-split boundary lines
        # (confident full-frame false positive, IoU 0.558); detection must
        # stay accurate at HD.
        self._assert_detected_well(_HD_RUN, _HD_META)
        payload = _HD_RUN["payload"]
        self.assertEqual(payload["frame_count"], DEFAULT_MAX_FRAMES)
        self.assertLess(payload["rect"]["w"] * payload["rect"]["h"], 0.97,
                        f"full-frame leak: {payload['rect']}")

    def test_all_motion_video_not_reliable(self):
        run = _MOTION_RUN
        self.assertEqual(run["rc"], 0, run["stderr"])  # ran fine; result JSON tells
        payload = run["payload"]
        self.assertIsNotNone(payload)
        self.assertFalse(payload["reliable"], f"all-motion video must not be reliable: {payload}")

    def test_dense_single_slide_fails_safe(self):
        # Documented limitation, pinned in the FAIL-SAFE direction: one
        # text-dense slide across the whole runtime -> never a confident
        # region, --write-region exits 2 and writes no region file.
        run = _DENSE_RUN
        self.assertEqual(run["rc"], 2, f"expected exit 2, stderr={run['stderr']}")
        payload = run["payload"]
        self.assertIsNotNone(payload)
        self.assertFalse(payload["reliable"], f"dense-slide detection must not be reliable: {payload}")
        self.assertIn("unreliable", run["stderr"])
        region = Path(run["preview"]).parent / "dense-region.json"
        self.assertFalse(region.exists(), "region.json must NOT be written for the dense fail-safe case")

    def test_preview_png_matches_video_dimensions(self):
        for run, meta in ((_MEETING_RUN, _MEETING_META), (_MOTION_RUN, _MOTION_META),
                          (_HD_RUN, _HD_META)):
            self.assertEqual(run["rc"], 0, run["stderr"])
            image = cv2.imread(str(run["preview"]), cv2.IMREAD_COLOR)
            self.assertIsNotNone(image, f"preview PNG missing/unreadable: {run['preview']}")
            height, width = image.shape[:2]
            self.assertEqual((width, height), tuple(meta["size"]))

    def test_write_region_on_reliable_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            region = Path(tmp) / "region.json"
            run = _run_detect(_MEETING_META["path"], Path(tmp), "region-ok",
                              "--write-region", str(region))
            self.assertEqual(run["rc"], 0, run["stderr"])
            self.assertTrue(region.is_file(), "region.json not written for reliable detection")
            data = json.loads(region.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["source"], "auto")
            self.assertEqual(data["video"], Path(_MEETING_META["path"]).name)
            self.assertEqual(data["video"], "meeting.mp4")
            for key in ("x", "y", "w", "h"):
                self.assertGreaterEqual(data["rect"][key], 0.0, key)
                self.assertLessEqual(data["rect"][key], 1.0, key)
            self.assertAlmostEqual(data["confidence"], run["payload"]["confidence"], places=6)
            self.assertGreater(data["created_ts"], 0.0)

    def test_write_region_on_unreliable_video_exits_2_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            region = Path(tmp) / "region.json"
            run = _run_detect(_MOTION_META["path"], Path(tmp), "region-bad",
                              "--write-region", str(region))
            self.assertEqual(run["rc"], 2, "unreliable --write-region must exit 2")
            self.assertFalse(region.exists(), "region.json must NOT be written when unreliable")
            self.assertIn("unreliable", run["stderr"])
            # result JSON is still produced and reports reliable=false.
            self.assertIsNotNone(run["payload"])
            self.assertFalse(run["payload"]["reliable"])

    def test_single_detection_wall_clock_under_60s(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            run = _run_detect(_MEETING_META["path"], Path(tmp), "timing")
            elapsed = time.monotonic() - started
            self.assertEqual(run["rc"], 0, run["stderr"])
            self.assertLess(elapsed, 60.0, f"detection took {elapsed:.1f}s (budget 60s)")

    def test_frame_count_within_max_frames(self):
        payload = _MEETING_RUN["payload"]
        self.assertEqual(payload["frame_count"], DEFAULT_MAX_FRAMES)  # 8s @ 5fps = 40 frames, cap 20
        with tempfile.TemporaryDirectory() as tmp:
            run = _run_detect(_MEETING_META["path"], Path(tmp), "max8", "--max-frames", "8")
            self.assertEqual(run["rc"], 0, run["stderr"])
            self.assertEqual(run["payload"]["frame_count"], 8)

    def test_video_shorter_than_max_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth_video.generate_meeting_video(
                Path(tmp) / "short.mp4", duration_s=1.0, fps=5, size=(320, 200))
            run = _run_detect(Path(tmp) / "short.mp4", Path(tmp), "short")
            self.assertEqual(run["rc"], 0, run["stderr"])
            self.assertGreater(run["payload"]["frame_count"], 0)
            self.assertLessEqual(run["payload"]["frame_count"], 5)  # 1s * 5fps total frames
            rect = run["payload"]["rect"]
            if rect is not None:  # rect sanity: never a confident full-frame leak
                for key in ("x", "y", "w", "h"):
                    self.assertGreaterEqual(rect[key], 0.0, key)
                    self.assertLessEqual(rect[key], 1.0, key)
                self.assertLess(rect["w"] * rect["h"], 0.97, f"full-frame leak: {rect}")

    def test_missing_video_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run_detect(Path(tmp) / "absent.mp4", Path(tmp), "missing")
            self.assertNotEqual(run["rc"], 0)
            self.assertIsNone(run["payload"])


if __name__ == "__main__":
    unittest.main()
