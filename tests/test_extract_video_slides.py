"""Tests for core/scripts/extract_video_slides.py (spec §3.2 + §2 audio decision + §5 degradation).

Runs green under system python3 (cv2-dependent cases skip) and fully under
vendor/tools-venv/bin/python. The script under test is invoked as a subprocess
with sys.executable, so it runs under whatever interpreter runs this suite.

Fixture ground truth comes from tests/synth_video.generate_meeting_video, whose
frame-accurate-truth contract is enforced by test_synth_video. Expectations are
pinned by spike measurements on the 20s fixture (extraction at 1 fps):
  - page cuts: pHash hamming 18/26/26/30, pixdiff 0.046-0.084  → T1=10/T2=0.06 fire
  - mouse sweep [4.5,6.0] (inside GT segment 2): hamming <=2, pixdiff <=0.0011 → absorbed
  - within-page noise floor: hamming exactly 0, pixdiff <=2e-5 (deterministic x264
    skip blocks) → no false candidates even at hair-trigger thresholds
  - all-motion: prev-frame pixdiff peaks ~0.10 (<0.5 → no region invalidation),
    ~20 churn segments of ~1s (>= CHURN_MIN_SEGMENTS=8, mean <5s → anomaly)
  - region-invalidation fixture (test 9): two static pages then 1s black/white
    full-region flashing → prev-pixdiff ~0.96-1.0 for consecutive samples;
    detected on the 3rd flash sample, track truncated at the first one (t=9).
"""

from __future__ import annotations

import json
import re
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

try:
    import resource  # non-Unix platforms lack it; the memory case skips then
except ImportError:
    resource = None

import synth_video  # noqa: E402

SCRIPT = WS / "core" / "scripts" / "extract_video_slides.py"
_SESSION: dict = {}

SLIDES_JSON_KEYS = {"schema_version", "video", "region_source", "fps_sampled",
                    "warnings", "slides"}
SLIDE_ITEM_KEYS = {"page_id", "image", "time_ranges"}
RECEIPT_KEYS = {"schema_version", "status", "frames_sampled", "segments", "pages",
                "warnings", "audio_route", "audio_extracted", "video_duration_s",
                "elapsed_s"}
# Receipts/slides.json carry counts + metadata only — never recognized text.
FORBIDDEN_KEY_FRAGMENTS = ("text", "ocr", "content", "literal", "transcript",
                           "items", "words")


def setUpModule() -> None:
    if not _HAS_CV2:
        return  # every test skips under system python3; no fixtures needed
    tmp = Path(tempfile.mkdtemp(prefix="extract-video-slides-fixture-"))
    v1 = synth_video.generate_meeting_video(
        tmp / "v1_voiced.mp4", duration_s=20.0, fps=10, size=(640, 400), audio=True)
    v2 = synth_video.generate_meeting_video(
        tmp / "v2_silent.mp4", duration_s=20.0, fps=10, size=(640, 400), audio=False)
    v3 = synth_video.generate_meeting_video(
        tmp / "v3_motion.mp4", duration_s=20.0, fps=5, size=(640, 400),
        ppt_rect=None, audio=False)
    _SESSION.update({
        "tmp": tmp, "v1": v1, "v2": v2, "v3": v3,
        "region_v1": _write_region(tmp, "region_v1.json", v1, v1["ppt_rect_px"]),
        "region_v2": _write_region(tmp, "region_v2.json", v2, v2["ppt_rect_px"]),
        "region_v3": _write_region(tmp, "region_v3.json", v3, (0, 0, *v3["size"]),
                                   source="auto"),
    })


def tearDownModule() -> None:
    tmp = _SESSION.get("tmp")
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_region(dir_path: Path, name: str, meta: dict, rect_px, source: str = "user") -> Path:
    """region.json per spec §4.1: RELATIVE rect (0-1) derived from fixture pixels."""
    width, height = meta["size"]
    x, y, w, h = rect_px
    payload = {
        "schema_version": 1,
        "video": Path(meta["path"]).name,
        "source": source,
        "rect": {"x": x / width, "y": y / height, "w": w / width, "h": h / height},
        "confidence": 0.9,
        "created_ts": 1759200000.0,
    }
    path = dir_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _run_script(args, expect_rc: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != expect_rc:
        raise AssertionError(
            f"exit {proc.returncode} != {expect_rc}\n"
            f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}")
    return proc


def _extract(run_dir: Path, meta: dict, region: Path, extra=()) -> dict:
    """Run the script (expecting rc 0) and parse its two JSON artifacts."""
    _run_script(["--video", meta["path"], "--region-json", region,
                 "--run-dir", run_dir, *extra])
    slides = json.loads((run_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
    receipt = json.loads((run_dir / "video" / "extract_receipt.json").read_text(encoding="utf-8"))
    return {"slides": slides, "receipt": receipt}


def _expected_unique(meta: dict) -> list[dict]:
    """Fixture unique_pages reordered by FIRST appearance (P1..Pn contract)."""
    first: dict[int, float] = {}
    for seg in meta["segments"]:
        first.setdefault(seg["page"], seg["start"])
    by_page = {u["page"]: u for u in meta["unique_pages"]}
    return [by_page[p] for p in sorted(first, key=lambda p: first[p])]


def _assert_no_text_keys(test: unittest.TestCase, obj, path: str = "$") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            for fragment in FORBIDDEN_KEY_FRAGMENTS:
                test.assertNotIn(fragment, lowered, f"forbidden text key at {path}.{key}")
            _assert_no_text_keys(test, value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _assert_no_text_keys(test, value, f"{path}[{index}]")


def _generate_invalidating_video(path: Path, *, duration_s: float = 14.0,
                                 fps: int = 5,
                                 size: tuple[int, int] = (640, 400)) -> dict:
    """Deterministic region-invalidation fixture (test 9, spec §5 window-drag row).

    Timeline: static slide A [0,6), static slide B [6,9), then the whole region
    alternates black/white every 1s over [9,14) → prev-frame normalized pixel
    diff ~0.96-1.0 ≥ 0.5 for ≥3 consecutive 1fps samples (run starts at the
    first flash sample t=9). A/B are rendered with synth_video's page renderer
    (pages 0/1) so their separation is the spike-measured hamming 18 — above
    T1=10 (segments) and above T3=5 (no dedup merge). Voiced (sine) so the
    "audio unaffected" guarantee is observable. Encoding params mirror
    synth_video (pinned -threads 1 / crf 23 / ultrafast) for determinism.
    """
    width, height = size
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
           "-r", str(fps), "-i", "-",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
           "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-shortest", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        box = (0, 0, width, height)
        for idx in range(int(round(duration_s * fps))):
            t = idx / fps
            if t < 9.0:
                frame = numpy.full((height, width, 3), 28, dtype=numpy.uint8)
                synth_video._draw_slide(frame, box, 0 if t < 6.0 else 1)
            else:
                level = 0 if int(t - 9.0) % 2 == 0 else 255
                frame = numpy.full((height, width, 3), level, dtype=numpy.uint8)
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        # Drain stderr BEFORE wait() to avoid pipe-buffer deadlock (fixture pattern).
        tail = (proc.stderr.read() or b"").decode("utf-8", "replace")[-500:]
        proc.stderr.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed encoding {path}: {tail}")
    return {"path": str(path), "duration_s": duration_s, "size": (width, height),
            "static_a": [0.0, 6.0], "static_b": [6.0, 9.0], "flash_start": 9.0}


@unittest.skipUnless(_HAS_CV2, "requires numpy+cv2 (vendor/tools-venv)")
class TestExtractVideoSlides(unittest.TestCase):
    def test_1_voiced_video_extracts_audio_and_four_deduped_pages(self):
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            out = _extract(run_dir, v1, _SESSION["region_v1"])

            # §2 decision: video has audio + no external file → extracted m4a.
            audio = run_dir / "extracted_audio.m4a"
            self.assertTrue(audio.is_file())
            self.assertTrue(synth_video.probe(audio)["has_audio"])

            slides = out["slides"]["slides"]
            self.assertEqual([s["page_id"] for s in slides], ["P1", "P2", "P3", "P4"])
            expected = _expected_unique(v1)
            # Page count == 4 IS the mouse-transient assertion: the sweep
            # (meta["mouse_crossings"] == [(4.5, 6.0)], strictly inside GT
            # segment 2 [4,8)) created NO extra page and NO extra boundary —
            # an unabsorbed sweep would fragment P2 into extra ranges/pages.
            self.assertEqual(len(slides), len(expected))
            self.assertEqual(v1["mouse_crossings"], [(4.5, 6.0)])
            for slide, gt in zip(slides, expected):
                self.assertEqual(len(slide["time_ranges"]), len(gt["ranges"]),
                                 f"{slide['page_id']} range count")
                for (start, end), (gt_start, gt_end) in zip(slide["time_ranges"],
                                                            gt["ranges"]):
                    self.assertAlmostEqual(start, gt_start, delta=1.5)  # 1fps tolerance
                    self.assertAlmostEqual(end, gt_end, delta=1.5)
            # Dedup ground truth: GT page index 1 is shown in TWO separate
            # ranges and must merge into one page (P2 by first appearance).
            self.assertEqual(len(slides[1]["time_ranges"]), 2)

            # One PNG per page, at crop-region resolution.
            _px, _py, pw, ph = v1["ppt_rect_px"]
            for slide in slides:
                png = run_dir / "slides" / slide["image"]
                self.assertTrue(png.is_file())
                image = cv2.imread(str(png))
                self.assertIsNotNone(image)
                self.assertEqual(image.shape, (ph, pw, 3))

            receipt = out["receipt"]
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(receipt["warnings"], [])
            self.assertEqual(receipt["segments"], 5)
            self.assertEqual(receipt["pages"], 4)
            self.assertAlmostEqual(receipt["frames_sampled"], 20, delta=1)
            self.assertEqual(receipt["audio_route"], "extracted")
            self.assertEqual(receipt["audio_extracted"], str(audio))
            self.assertAlmostEqual(receipt["video_duration_s"], 20.0, delta=0.5)

    def test_2_external_audio_flag_skips_extraction(self):
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            out = _extract(run_dir, v1, _SESSION["region_v1"], ["--has-external-audio"])
            self.assertFalse((run_dir / "extracted_audio.m4a").exists())
            self.assertEqual(out["receipt"]["audio_route"], "external")
            self.assertIsNone(out["receipt"]["audio_extracted"])
            self.assertEqual(len(out["slides"]["slides"]), 4)

    def test_3_silent_video_without_external_audio_fails_fast(self):
        v2 = _SESSION["v2"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            proc = _run_script(["--video", v2["path"], "--region-json",
                                _SESSION["region_v2"], "--run-dir", run_dir],
                               expect_rc=2)
            self.assertIn("录屏无声且事件内无独立音频文件", proc.stderr)
            self.assertIn("含声录屏", proc.stderr)
            # No partial artifacts left behind (spec §5 first row).
            leftovers = sorted(run_dir.rglob("*")) if run_dir.exists() else []
            self.assertEqual(leftovers, [])

    def test_4_max_pages_flood_guard_retracts_pages_keeps_audio(self):
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            out = _extract(run_dir, v1, _SESSION["region_v1"], ["--max-pages", 2])
            self.assertEqual(out["slides"]["slides"], [])
            self.assertTrue(any("segmentation_anomaly" in w
                                for w in out["slides"]["warnings"]))
            self.assertEqual(list((run_dir / "slides").glob("P*.png")), [])
            # Audio track is a separate concern and survives the slide flood guard.
            audio = run_dir / "extracted_audio.m4a"
            self.assertTrue(audio.is_file())
            self.assertGreater(audio.stat().st_size, 0)
            receipt = out["receipt"]
            self.assertEqual(receipt["pages"], 0)
            self.assertEqual(receipt["audio_route"], "extracted")
            self.assertIn("segmentation_anomaly_suspected_embedded_video",
                          receipt["warnings"])

    def test_5_output_schemas_typed_and_text_free(self):
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            out = _extract(run_dir, v1, _SESSION["region_v1"])
            slides_doc, receipt = out["slides"], out["receipt"]

            self.assertEqual(set(slides_doc), SLIDES_JSON_KEYS)
            self.assertEqual(slides_doc["schema_version"], 1)
            self.assertEqual(slides_doc["video"], "v1_voiced.mp4")
            self.assertEqual(slides_doc["region_source"], "user")
            self.assertIsInstance(slides_doc["fps_sampled"], float)
            self.assertEqual(slides_doc["fps_sampled"], 1.0)
            self.assertIsInstance(slides_doc["warnings"], list)
            self.assertTrue(all(isinstance(w, str) for w in slides_doc["warnings"]))
            self.assertIsInstance(slides_doc["slides"], list)
            for slide in slides_doc["slides"]:
                self.assertEqual(set(slide), SLIDE_ITEM_KEYS)
                self.assertRegex(slide["page_id"], r"^P[1-9][0-9]*$")
                self.assertEqual(slide["image"], f"{slide['page_id']}.png")
                self.assertIsInstance(slide["time_ranges"], list)
                self.assertGreater(len(slide["time_ranges"]), 0)
                for span in slide["time_ranges"]:
                    self.assertIsInstance(span, list)
                    self.assertEqual(len(span), 2)
                    start, end = span
                    for value in (start, end):
                        self.assertIsInstance(value, (int, float))
                        self.assertNotIsInstance(value, bool)
                    self.assertLess(start, end)

            self.assertEqual(set(receipt), RECEIPT_KEYS)
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["status"], "complete")
            for key in ("frames_sampled", "segments", "pages"):
                self.assertIsInstance(receipt[key], int)
                self.assertNotIsInstance(receipt[key], bool)
            self.assertIn(receipt["audio_route"], {"extracted", "external", "none"})
            self.assertIsInstance(receipt["audio_extracted"], (str, type(None)))
            self.assertIsInstance(receipt["video_duration_s"], (int, float))
            self.assertIsInstance(receipt["elapsed_s"], (int, float))
            self.assertGreaterEqual(receipt["elapsed_s"], 0.0)
            self.assertTrue(all(isinstance(w, str) for w in receipt["warnings"]))

            # Iron rule: no recognized text anywhere in receipt/slides.json.
            _assert_no_text_keys(self, receipt)
            _assert_no_text_keys(self, slides_doc)

    def test_6_memory_bounded_on_long_video(self):
        # The plan allows a shorter-run + 3-minute-extrapolation substitute;
        # this test instead runs the full 3-minute fixture directly at
        # 1920x1080 (deterministic encode params), extraction at fps=1.
        # Hoarding sensitivity (review-card math): keeping every sampled full
        # 1080p frame would need ~180 x 6.2MB ~= 1.12GB > the 1GB budget, so a
        # frame-hoarding regression FAILS here, while the bounded-descriptor
        # implementation stays far below.
        if resource is None:
            self.skipTest("resource module unavailable on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            meta = synth_video.generate_meeting_video(
                tmp_path / "long.mp4", duration_s=180.0, fps=5, size=(1920, 1080),
                audio=False)
            region = _write_region(tmp_path, "region_long.json", meta, meta["ppt_rect_px"])
            out = _extract(tmp_path / "run", meta, region,
                           ["--fps", 1.0, "--has-external-audio"])
            self.assertEqual(len(out["slides"]["slides"]), 4)
            self.assertEqual(out["receipt"]["segments"], 5)
            self.assertAlmostEqual(out["receipt"]["video_duration_s"], 180.0, delta=1.0)
            peak_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            # macOS: ru_maxrss is in BYTES (Linux would be KB). This is the
            # cumulative max over all reaped children (fixture encodes +
            # extraction); every stage must stay far below the 1GB budget
            # because the extractor holds descriptors + <=3 full-res frames.
            self.assertLess(peak_rss, 1_000_000_000,
                            f"peak child RSS {peak_rss} bytes exceeds 1GB budget")

    def test_7_custom_thresholds_are_wired_both_directions(self):
        # Coordinator-approved replacement for the original "hair-trigger → >4
        # pages" expectation, which is empirically unreachable on this fixture:
        # the deterministic encoder yields a ZERO within-page noise floor
        # (hamming exactly 0, pixdiff <=2e-5 between static samples), so even
        # t1=1/t2=0.001 find nothing except the mouse sweep's single candidate
        # sample (hamming 2 >= 1 at t=5; t=6 is back to hamming 0 / pixdiff
        # 2e-5), which the >=2-consecutive hysteresis absorbs. Any hypothetical
        # fragmentation would also re-merge under T3=5 dedup (all within-page
        # and cursor-frame hashes measured within hamming 2). Hence pages == 4.
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            hair = _extract(Path(tmp) / "run_hair", v1, _SESSION["region_v1"],
                            ["--t1", 1, "--t2", 0.001])
            hair_slides = hair["slides"]["slides"]
            self.assertGreater(len(hair_slides), 0)
            self.assertLessEqual(len(hair_slides), 60)
            self.assertEqual(len(hair_slides), 4)

            # Dead-trigger: thresholds no frame pair can reach (max measured
            # hamming 30, pixdiff <=1.0 on V1) → one segment, one page. The
            # 4-vs-1 collapse from thresholds alone proves --t1/--t2 drive
            # segmentation end-to-end in both directions.
            dead = _extract(Path(tmp) / "run_dead", v1, _SESSION["region_v1"],
                            ["--t1", 64, "--t2", 1.5])
            dead_slides = dead["slides"]["slides"]
            self.assertEqual(len(dead_slides), 1)
            self.assertEqual(dead_slides[0]["page_id"], "P1")
            self.assertEqual(len(dead_slides[0]["time_ranges"]), 1)
            start, end = dead_slides[0]["time_ranges"][0]
            self.assertAlmostEqual(start, 0.0, delta=0.01)
            self.assertAlmostEqual(end, 20.0, delta=0.5)
            self.assertEqual(dead["receipt"]["segments"], 1)

    def test_8_all_motion_video_triggers_churn_anomaly(self):
        # Spec §5 "分段异常" row via the mean-segment trigger: continuous
        # motion → ~20 churn segments of ~1s (>= CHURN_MIN_SEGMENTS=8, mean
        # < 5.0s) → pages retracted, warning written, audio track unaffected.
        # Prev-frame pixdiff on this fixture peaks ~0.10 < 0.5, so the region
        # invalidation path must NOT fire here.
        v3 = _SESSION["v3"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            out = _extract(run_dir, v3, _SESSION["region_v3"], ["--has-external-audio"])
            self.assertEqual(out["slides"]["slides"], [])
            self.assertIn("segmentation_anomaly_suspected_embedded_video",
                          out["slides"]["warnings"])
            self.assertFalse(any(w.startswith("region_invalidated")
                                 for w in out["slides"]["warnings"]))
            self.assertEqual(list((run_dir / "slides").glob("P*.png")), [])
            receipt = out["receipt"]
            self.assertEqual(receipt["pages"], 0)
            self.assertGreaterEqual(receipt["segments"], 8)
            self.assertEqual(receipt["audio_route"], "external")

    def test_9_region_invalidation_truncates_and_keeps_confirmed_pages(self):
        # Spec §5 window-drag row: 3 consecutive samples with prev-frame
        # pixdiff >= 0.5 truncate the slide track at the FIRST such sample
        # (t=9 here → warning "region_invalidated_at_9"). Pages confirmed
        # before the invalidation point are KEPT (not retracted), streaming
        # stops early (bounded abort), and the audio track is unaffected.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            meta = _generate_invalidating_video(tmp_path / "flash.mp4")
            region = _write_region(tmp_path, "region_flash.json", meta,
                                   (0, 0, *meta["size"]), source="auto")
            run_dir = tmp_path / "run"
            out = _extract(run_dir, meta, region)

            warnings = out["slides"]["warnings"]
            self.assertIn("region_invalidated_at_9", warnings)
            self.assertNotIn("segmentation_anomaly_suspected_embedded_video",
                             warnings)
            slides = out["slides"]["slides"]
            self.assertEqual([s["page_id"] for s in slides], ["P1", "P2"])
            # Kept, NOT retracted: both representative PNGs are on disk.
            self.assertTrue((run_dir / "slides" / "P1.png").is_file())
            self.assertTrue((run_dir / "slides" / "P2.png").is_file())
            for slide, gt_range in zip(slides, (meta["static_a"], meta["static_b"])):
                self.assertEqual(len(slide["time_ranges"]), 1)
                start, end = slide["time_ranges"][0]
                self.assertAlmostEqual(start, gt_range[0], delta=0.5)
                self.assertAlmostEqual(end, gt_range[1], delta=0.5)

            receipt = out["receipt"]
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(receipt["pages"], 2)
            self.assertEqual(receipt["segments"], 2)
            # Bounded abort: detection needs the 3rd flash sample (t=11), so
            # streaming stops at 12 samples — strictly fewer than the 14 the
            # full 14s video would yield at fps=1.
            self.assertLess(receipt["frames_sampled"], 14)
            self.assertGreaterEqual(receipt["frames_sampled"], 9)
            # Audio unaffected by the slide-track truncation.
            self.assertEqual(receipt["audio_route"], "extracted")
            audio = run_dir / "extracted_audio.m4a"
            self.assertTrue(audio.is_file())
            self.assertGreater(audio.stat().st_size, 0)
            self.assertTrue(synth_video.probe(audio)["has_audio"])

    def test_10_truncated_video_reports_frame_shortfall(self):
        # Recorder-crash style input (review-proven): faststart remux keeps the
        # moov intact, cutting the mdat at ~55% of bytes makes ffmpeg decode
        # what it can and still exit rc=0 with "partial file" only on stderr.
        # The shortfall must surface as a warning instead of a silent
        # complete-looking receipt (iron rule #6). Measured: 11 of 20 samples.
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            faststart = tmp_path / "faststart.mp4"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", v1["path"],
                 "-c", "copy", "-movflags", "+faststart", str(faststart)],
                capture_output=True, text=True, check=True, timeout=300)
            blob = faststart.read_bytes()
            truncated = tmp_path / "truncated.mp4"
            truncated.write_bytes(blob[: int(len(blob) * 0.55)])
            meta = {"path": str(truncated), "size": v1["size"]}
            region = _write_region(tmp_path, "region_trunc.json", meta,
                                   v1["ppt_rect_px"])
            out = _extract(tmp_path / "run", meta, region, ["--has-external-audio"])

            warning = "frame_shortfall_suspected_truncated_video"
            self.assertIn(warning, out["slides"]["warnings"])
            self.assertIn(warning, out["receipt"]["warnings"])
            # rc is still 0 (degraded completion); per the receipt schema the
            # degradation surfaces via warnings, status stays "complete".
            self.assertEqual(out["receipt"]["status"], "complete")
            self.assertNotIn("segmentation_anomaly_suspected_embedded_video",
                             out["slides"]["warnings"])
            # Well under the 20 samples the intact 20s duration promises at
            # fps=1 (tolerance max(2, 10%) → anything < 18 flags), but > 0.
            self.assertLess(out["receipt"]["frames_sampled"], 18)
            self.assertGreater(out["receipt"]["frames_sampled"], 0)
            self.assertGreaterEqual(out["receipt"]["pages"], 1)

    def test_11_rerun_into_same_run_dir_leaves_no_orphan_pngs(self):
        # run_meeting retries reuse runs/<event> without rmtree: pages from a
        # first run must not survive a second run with different thresholds —
        # spec §3.4 packages slides via glob P*.png, so orphans would ship
        # contradicting slides.json (review-proven before the run-start sweep).
        v1 = _SESSION["v1"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            first = _extract(run_dir, v1, _SESSION["region_v1"])
            self.assertEqual(len(first["slides"]["slides"]), 4)
            self.assertEqual(sorted(p.name for p in (run_dir / "slides").glob("P*.png")),
                             ["P1.png", "P2.png", "P3.png", "P4.png"])
            # Re-run into the SAME run-dir with dead-trigger thresholds → 1 page.
            second = _extract(run_dir, v1, _SESSION["region_v1"],
                              ["--t1", 64, "--t2", 1.5])
            self.assertEqual(len(second["slides"]["slides"]), 1)
            # Exact file-set: the current page plus the manifest, no orphans.
            self.assertEqual(sorted(p.name for p in (run_dir / "slides").iterdir()),
                             ["P1.png", "slides.json"])


if __name__ == "__main__":
    unittest.main()
