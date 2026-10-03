#!/usr/bin/env python3
"""Video decomposition: audio-track decision + region-cropped frame streaming +
hysteresis slide segmentation + dedup + sharpest representative PNG per page.

Pipeline position (docs/screen-recording-parsing-design.md §3.2): invoked by
the video_ingest stage; Apple Vision OCR (run_vision_ocr.py) runs separately
over slides/ afterwards — this script never produces recognized text anywhere
(receipts carry counts/metadata only).

Design decisions (coordinator-endorsed, pinned by spike measurements on the
tests/synth_video fixture — 20s/640x400/fps10 sampled at 1 fps):

* ANCHOR-BASED HYSTERESIS. "hamming >= T1 or pixdiff >= T2 persisting for
  >= 2 consecutive samples" is evaluated against the current segment's ANCHOR
  (its first stable frame), not the immediately-previous frame: a hard cut
  produces exactly one prev-frame diff spike, so persistence is only decidable
  against a stable reference. The boundary is placed at the FIRST candidate
  sample's timestamp; the confirming frame is then re-evaluated against the
  new anchor so genuine back-to-back changes still segment. Isolated
  single-frame flicker (mouse sweep, cursor blink) resets the pending counter
  and is absorbed. Measured: page cuts hamming 18-30 / pixdiff 0.046-0.084
  (defaults T1=10, T2=0.06 fire); mouse sweep hamming <= 2 / pixdiff
  <= 0.0011; within-page noise floor hamming 0 / pixdiff <= 2e-5 (never fire).
* CHURN FLOOR. The "mean segment length < 5s → suspected embedded video"
  trigger applies only when >= CHURN_MIN_SEGMENTS (8) confirmed segments
  exist: the heuristic is statistically meaningful only with enough segments,
  and a short legitimate recording (20s fixture: 5 segments, mean 4.0s) must
  not be flagged. Measured all-motion churn: 10-20 segments, mean ~1-2s. The
  pages > max-pages trigger is unconditional and also checked mid-stream
  (early abort bounds work).
* ANOMALY RETRACTION. On anomaly, pages produced so far are REMOVED (slides
  dir contents deleted) and slides.json is written with "slides": [] plus
  warning "segmentation_anomaly_suspected_embedded_video". Keeping flooded
  partial pages would push hundreds of junk PNGs into downstream OCR and
  packaging; the audio track is separate and unaffected (kept).
* RUN-START HYGIENE. The slides dir is emptied before streaming begins, so a
  retry into a reused run-dir never leaves orphan P*.png from an earlier run
  contradicting slides.json (downstream packages via glob); symmetric with
  the retraction above.
* FRAME SHORTFALL. A mid-truncated recording (recorder crash) can decode with
  ffmpeg exiting 0 after fewer frames than the container duration promises,
  with the "partial file" notice only on stderr. On paths that read the stream
  to EOF (NOT page-cap abort, NOT region invalidation — those stop early by
  design), frames_sampled < duration*fps − max(2, 10%) appends warning
  "frame_shortfall_suspected_truncated_video" (iron rule #6: annotate the
  uncertainty instead of silently reporting a complete pass).
* REGION INVALIDATION (window drag, spec §5): >= 3 consecutive sampled frames
  with PREV-frame normalized pixel diff >= 0.5 (near-total change — static
  slide content cannot legitimately do this; measured all-motion noise peaks
  at ~0.10) truncate the slide track at the FIRST such frame; pages/segments
  confirmed before it are kept, warning "region_invalidated_at_<seconds>" is
  added, audio unaffected.
* BOUNDED MEMORY: only per-frame small descriptors (64-bit pHash + 64x64
  gray) and the current segment's best/anchor/pending full-res frames are
  held — never the frame stream. Settled pages persist as 8-byte hashes.
* --manifest is accepted but intentionally unused: manifest injection belongs
  to the video_ingest wiring task (single responsibility).
* Source immutability: nothing is ever written into the video's directory;
  every artifact lands under --run-dir. Deterministic: no RNG; laplacian ties
  resolve to the earliest frame. JSON writes are atomic (tmp + rename).

Runs under vendor/tools-venv python (numpy + cv2); ffmpeg/ffprobe from PATH.
Exit codes: 0 = completed (possibly degraded, see warnings), 2 = silent video
with no external audio (spec §2 fail-fast; note argparse usage errors also
exit with code 2 — the stderr text distinguishes them: usage errors print the
argparse usage block, the audio fail-fast prints AUDIO_MISSING_MESSAGE),
other = fatal error.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError as exc:  # system python3 without the tools venv
    raise SystemExit(
        f"extract_video_slides requires numpy+cv2 (run under vendor/tools-venv/bin/python): {exc}")

SCHEMA_VERSION = 1
PHASH_SIZE = 32            # pHash: 32x32 gray DCT → 8x8 low-frequency bits
SMALL_SIZE = 64            # pixdiff: 64x64 normalized gray
CONFIRM_SAMPLES = 2        # hysteresis: a change must persist this many consecutive samples
MIN_SEGMENT_S = 1.0        # shorter confirmed segments merge into the previous page
CHURN_MIN_SEGMENTS = 8     # floor for the mean-segment anomaly trigger (see docstring)
CHURN_MEAN_S = 5.0
INVALID_PIXDIFF = 0.5      # region-invalidation: near-total prev-frame change
INVALID_RUN = 3            # ...persisting for 3 consecutive samples
FRAME_SHORTFALL_RATIO = 0.10   # shortfall tolerance: 10% of expected samples ...
FRAME_SHORTFALL_MIN = 2.0      # ... but at least 2 frames (fps-filter rounding slack)
ANOMALY_WARNING = "segmentation_anomaly_suspected_embedded_video"
SHORTFALL_WARNING = "frame_shortfall_suspected_truncated_video"
AUDIO_MISSING_MESSAGE = "录屏无声且事件内无独立音频文件:需要含声录屏或另配音频"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def even_floor(value: float) -> int:
    """Largest even integer <= value (epsilon absorbs float round-trips of
    relative region rects, e.g. (396/640)*640 == 395.99999999999994)."""
    return int(math.floor(value + 1e-6)) // 2 * 2


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def empty_directory(dir_path: Path) -> None:
    """Delete every file in dir_path (must exist; subdirectories untouched).
    Shared by run-start hygiene and anomaly retraction."""
    for child in sorted(dir_path.iterdir()):
        if child.is_file():
            child.unlink()


def pixdiff(a, b) -> float:
    return float(np.mean(np.abs(a - b)))


def describe(frame):
    """Per-frame descriptors: (64x64 normalized gray, 64-bit pHash, laplacian
    variance). One gray conversion shared by all three."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (SMALL_SIZE, SMALL_SIZE),
                       interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    tiny = cv2.resize(gray, (PHASH_SIZE, PHASH_SIZE), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(tiny.astype(np.float32))
    low = dct[:8, :8]
    digest = int.from_bytes(np.packbits((low > np.median(low)).reshape(-1)), "big")
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return small, digest, lap


def probe_video(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-show_format", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"ffprobe failed for {path}: {proc.stderr.strip()[-300:]}")
    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise SystemExit(f"no video stream in {path}")
    raw_duration = info.get("format", {}).get("duration") or video.get("duration")
    if raw_duration is None:
        raise SystemExit(f"cannot determine duration of {path}")
    return {"duration": float(raw_duration),
            "width": int(video["width"]),
            "height": int(video["height"]),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def resolve_crop(rect: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """region.json relative rect (0-1) → even-aligned pixel crop at actual size."""
    try:
        rx, ry, rw, rh = (float(rect[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"region.json rect invalid ({rect!r}): {exc}")
    if not (0.0 <= rx < 1.0 and 0.0 <= ry < 1.0 and 0.0 < rw <= 1.0 and 0.0 < rh <= 1.0
            and rx + rw <= 1.0 + 1e-6 and ry + rh <= 1.0 + 1e-6):
        raise SystemExit(f"region.json rect out of range: {rect!r}")
    x = min(max(even_floor(rx * width), 0), width - 2)
    y = min(max(even_floor(ry * height), 0), height - 2)
    w = max(2, even_floor(min(rw * width, width - x)))
    h = max(2, even_floor(min(rh * height, height - y)))
    return x, y, w, h


def resolve_audio(video: Path, run_dir: Path, has_audio: bool,
                  has_external: bool) -> tuple[str, Path | None]:
    """Spec §2 audio decision table (deterministic). Exits 2 BEFORE any
    artifact is created when the recording is silent and no external audio
    file was declared."""
    if has_external:
        return "external", None
    if not has_audio:
        print(AUDIO_MISSING_MESSAGE, file=sys.stderr)
        raise SystemExit(2)
    out = run_dir / "extracted_audio.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    tail = ""
    for codec in (["-c:a", "copy"], ["-c:a", "aac"]):  # copy first, re-encode on failure
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(video), "-vn",
             *codec, str(out)],
            capture_output=True, text=True)
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return "extracted", out
        tail = proc.stderr.strip()[-300:]
        out.unlink(missing_ok=True)  # never leave a partial audio artifact behind
    raise SystemExit(f"ffmpeg audio extraction failed for {video}: {tail}")


# --------------------------------------------------------------------------
# page registry + segmentation state machine
# --------------------------------------------------------------------------

class PageStore:
    """Unique pages in first-appearance order (P1..Pn): dedup by representative
    pHash (hamming <= T3 merges time ranges), PNG written on first appearance."""

    def __init__(self, slides_dir: Path, t3: int, max_pages: int) -> None:
        self.slides_dir = slides_dir
        self.t3 = t3
        self.max_pages = max_pages
        self.pages: list[dict] = []
        self.last_page: dict | None = None

    def add(self, start: float, end: float, rep_hash: int, rep_frame) -> bool:
        """Attach one confirmed segment. Returns False when the page cap is
        exceeded (caller must then abort streaming — spec §5 flood guard)."""
        for page in self.pages:
            if hamming(rep_hash, page["rep_hash"]) <= self.t3:
                page["ranges"].append([start, end])
                self.last_page = page
                return True
        if len(self.pages) >= self.max_pages:
            return False
        page_id = f"P{len(self.pages) + 1}"
        image = f"{page_id}.png"
        if not cv2.imwrite(str(self.slides_dir / image), rep_frame):
            raise SystemExit(f"failed writing representative frame: {self.slides_dir / image}")
        page = {"page_id": page_id, "image": image, "rep_hash": rep_hash,
                "ranges": [[start, end]]}
        self.pages.append(page)
        self.last_page = page
        return True

    def extend_last_range(self, end: float) -> None:
        if self.last_page is not None and self.last_page["ranges"]:
            self.last_page["ranges"][-1][1] = end

    def retract(self) -> None:
        """Anomaly path: empty this run's slides dir (delete every file in it)."""
        empty_directory(self.slides_dir)
        self.pages.clear()
        self.last_page = None


class SlideSegmenter:
    """Anchor-based hysteresis segmenter (see module docstring). push() each
    sampled frame in stream order; finish() closes the final segment at the
    video duration. Memory is bounded: descriptors + <= 3 full-res frames."""

    def __init__(self, *, fps: float, t1: int, t2: float, store: PageStore) -> None:
        self.fps = fps
        self.t1 = t1
        self.t2 = t2
        self.store = store
        self.segments: list[tuple[float, float]] = []
        self.frames_seen = 0
        self.aborted = False
        self.invalidated_at: float | None = None
        self._started = False
        self._seg_start = 0.0
        self._anchor_small = None
        self._anchor_hash = 0
        self._anchor_frame = None
        self._best: tuple | None = None      # (lapvar, t, frame, hash)
        self._pending = None                 # (t, small, hash, lapvar, frame)
        self._pending_count = 0
        self._prev_small = None
        self._invalid_run = 0
        self._invalid_start = 0.0

    def push(self, frame) -> bool:
        """Consume one sampled frame (timestamp = frames_seen / fps).
        Returns False when the caller must stop streaming."""
        t = self.frames_seen / self.fps
        self.frames_seen += 1
        small, digest, lap = describe(frame)
        if self._prev_small is not None and pixdiff(small, self._prev_small) >= INVALID_PIXDIFF:
            self._invalid_run += 1
            if self._invalid_run == 1:
                self._invalid_start = t
            if self._invalid_run >= INVALID_RUN:
                self._invalidate()
                return False
        else:
            self._invalid_run = 0
        self._prev_small = small
        if not self._started:
            self._started = True
            self._begin_segment(t, small, digest, lap, frame)
            return True
        if self._is_change(small, digest):
            return self._on_candidate(t, small, digest, lap, frame)
        self._pending, self._pending_count = None, 0
        self._offer_best(t, lap, frame, digest)
        return True

    def _is_change(self, small, digest) -> bool:
        return (hamming(digest, self._anchor_hash) >= self.t1
                or pixdiff(small, self._anchor_small) >= self.t2)

    def _on_candidate(self, t, small, digest, lap, frame) -> bool:
        if self._pending_count == 0:
            self._pending = (t, small, digest, lap, frame)
            self._pending_count = 1
            return True
        self._pending_count += 1
        if self._pending_count < CONFIRM_SAMPLES:
            # The spec's "persists for >= 2 consecutive samples" rule, kept as
            # a constant-parameterized check. With CONFIRM_SAMPLES=2 the count
            # arrives here already at 1, so this branch is never taken — it
            # becomes live logic only if the confirmation run is ever raised.
            return True
        b_t, b_small, b_hash, b_lap, b_frame = self._pending
        self._pending, self._pending_count = None, 0
        self._close_segment(b_t)
        if self.aborted:
            return False
        self._begin_segment(b_t, b_small, b_hash, b_lap, b_frame)
        # The confirming frame itself belongs to the new segment: re-evaluate
        # it against the new anchor (it may already start the next boundary).
        if self._is_change(small, digest):
            self._pending = (t, small, digest, lap, frame)
            self._pending_count = 1
        else:
            self._offer_best(t, lap, frame, digest)
        return True

    def _begin_segment(self, t, small, digest, lap, frame) -> None:
        self._seg_start = t
        self._anchor_small, self._anchor_hash, self._anchor_frame = small, digest, frame
        self._best = (lap, t, frame, digest)

    def _offer_best(self, t, lap, frame, digest) -> None:
        if self._best is None or lap > self._best[0]:  # strict >: earliest frame wins ties
            self._best = (lap, t, frame, digest)

    def _close_segment(self, end: float) -> None:
        if self._best is None or end <= self._seg_start:
            return
        _lap, best_t, frame, digest = self._best
        if best_t >= end:
            # Representative fell at/after the truncation point (only reachable
            # on region invalidation): fall back to the segment's anchor frame.
            frame, digest = self._anchor_frame, self._anchor_hash
        length = end - self._seg_start
        if self.segments and length < MIN_SEGMENT_S:
            # Sub-second flash: merge its span into the previous page's range
            # instead of emitting a page (spec: shorter segments merge back).
            self.segments[-1] = (self.segments[-1][0], end)
            self.store.extend_last_range(end)
            return
        if not self.store.add(self._seg_start, end, digest, frame):
            self.aborted = True  # page cap exceeded → early abort (flood guard)
            return
        self.segments.append((self._seg_start, end))

    def _invalidate(self) -> None:
        self.invalidated_at = self._invalid_start
        self._pending, self._pending_count = None, 0
        self._close_segment(self._invalid_start)

    def finish(self, duration: float) -> None:
        if self.aborted or self.invalidated_at is not None or not self._started:
            return
        self._pending, self._pending_count = None, 0
        self._close_segment(duration)

    def churn_anomaly(self) -> bool:
        """Mean-segment anomaly trigger, floored at CHURN_MIN_SEGMENTS (see
        module docstring). Skipped on aborted/invalidated tracks."""
        if self.aborted or self.invalidated_at is not None:
            return False
        if len(self.segments) < CHURN_MIN_SEGMENTS:
            return False
        mean = sum(e - s for s, e in self.segments) / len(self.segments)
        return mean < CHURN_MEAN_S


# --------------------------------------------------------------------------
# frame streaming
# --------------------------------------------------------------------------

def stream_frames(video: Path, crop: tuple[int, int, int, int], fps: float,
                  segmenter: SlideSegmenter) -> None:
    """Single ffmpeg process → raw bgr24 frames on stdout, read in fixed-size
    buffers. Frames are pushed into the segmenter and never stored en masse."""
    x, y, w, h = crop
    frame_bytes = w * h * 3
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
           "-vf", f"crop={w}:{h}:{x}:{y},fps={fps}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    errors = tempfile.TemporaryFile()  # avoids stderr-pipe deadlock, leaves no artifact
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            stderr=errors)
    stopped_early = False
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            if not segmenter.push(frame):
                stopped_early = True
                break
    finally:
        proc.stdout.close()
        if stopped_early:
            proc.kill()  # bound work on abort/invalidation
        rc = proc.wait()
        errors.seek(0)
        tail = errors.read().decode("utf-8", "replace")[-500:]
        errors.close()
    if not stopped_early and rc != 0:
        raise SystemExit(f"ffmpeg frame extraction failed for {video}: {tail}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decompose a meeting recording into audio track + slide pages.")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--region-json", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=None,
                        help="accepted for pipeline compatibility; unused by this "
                             "script (manifest injection is owned by video_ingest wiring)")
    parser.add_argument("--fps", type=float, default=1.0, help="frame sampling rate")
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--has-external-audio", action="store_true",
                        help="event has a separate audio file: skip extraction (spec §2)")
    parser.add_argument("--t1", type=int, default=10,
                        help="pHash hamming change threshold (64-bit)")
    parser.add_argument("--t2", type=float, default=0.06,
                        help="normalized pixel-diff change threshold")
    parser.add_argument("--t3", type=int, default=5,
                        help="pHash hamming dedup threshold (revisited pages)")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="default: <run-dir>/video/extract_receipt.json")
    args = parser.parse_args()
    if args.fps <= 0:
        raise SystemExit("--fps must be > 0")
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be >= 1")
    if args.t1 < 0 or args.t2 < 0 or args.t3 < 0:
        raise SystemExit("--t1/--t2/--t3 must be >= 0")
    return args


def load_region(path: Path) -> dict:
    try:
        region = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read region json {path}: {exc}")
    if not isinstance(region, dict) or not isinstance(region.get("rect"), dict):
        raise SystemExit(f"region.json malformed (need object with rect): {path}")
    return region


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    video: Path = args.video
    if not video.is_file():
        raise SystemExit(f"video not found: {video}")
    region = load_region(args.region_json)
    info = probe_video(video)
    duration = info["duration"]

    warnings: list[str] = []
    region_video = region.get("video")
    if region_video and Path(str(region_video)).name != video.name:
        # Re-detection on mismatch is owned by the wiring task; flag only.
        warnings.append("region_video_mismatch")
    crop = resolve_crop(region["rect"], info["width"], info["height"])

    # Audio decision first: the silent fail-fast (exit 2) must leave no artifacts.
    audio_route, audio_path = resolve_audio(video, args.run_dir, info["has_audio"],
                                            args.has_external_audio)

    slides_dir = args.run_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    # Run-start hygiene: retries reuse runs/<event> without cleanup; sweep any
    # stale files so orphans can never contradict the slides.json written below.
    empty_directory(slides_dir)
    store = PageStore(slides_dir, args.t3, args.max_pages)
    segmenter = SlideSegmenter(fps=args.fps, t1=args.t1, t2=args.t2, store=store)
    stream_frames(video, crop, args.fps, segmenter)
    segmenter.finish(duration)

    if segmenter.invalidated_at is not None:
        warnings.append(f"region_invalidated_at_{int(round(segmenter.invalidated_at))}")
    elif not segmenter.aborted:
        # Stream was read to EOF: fewer samples than the container duration
        # promises means a mid-truncated file (recorder crash) that ffmpeg
        # still exits 0 on — annotate instead of silently claiming complete.
        # Early-stop paths (page-cap abort / region invalidation) legitimately
        # sample fewer frames and are excluded.
        expected_frames = duration * args.fps
        tolerance = max(FRAME_SHORTFALL_MIN, FRAME_SHORTFALL_RATIO * expected_frames)
        if segmenter.frames_seen < expected_frames - tolerance:
            warnings.append(SHORTFALL_WARNING)
    if segmenter.aborted or segmenter.churn_anomaly():
        store.retract()  # flood guard: pull back pages already written (see docstring)
        if ANOMALY_WARNING not in warnings:
            warnings.append(ANOMALY_WARNING)

    slides_json = slides_dir / "slides.json"
    atomic_write_json(slides_json, {
        "schema_version": SCHEMA_VERSION,
        "video": video.name,
        "region_source": str(region.get("source") or "auto"),
        "fps_sampled": args.fps,
        "warnings": warnings,
        "slides": [
            {"page_id": page["page_id"], "image": page["image"],
             "time_ranges": [[round(s, 3), round(e, 3)] for s, e in page["ranges"]]}
            for page in store.pages
        ],
    })
    receipt_path = args.receipt or (args.run_dir / "video" / "extract_receipt.json")
    atomic_write_json(receipt_path, {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "frames_sampled": segmenter.frames_seen,
        "segments": len(segmenter.segments),
        "pages": len(store.pages),
        "warnings": warnings,
        "audio_route": audio_route,
        "audio_extracted": str(audio_path) if audio_path is not None else None,
        "video_duration_s": round(duration, 3),
        "elapsed_s": round(time.monotonic() - started, 3),
    })
    print(slides_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
