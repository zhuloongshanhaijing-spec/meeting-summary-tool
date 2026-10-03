#!/usr/bin/env python3
"""Detect the static, text-rich slide (PPT) region inside a meeting-window recording.

Spec: docs/screen-recording-parsing-design.md §3.1. Callers: webapp server
(region confirmation flow, synchronous) and run_meeting CLI fallback
(`--write-region` emits a source=auto region.json per §4.1).

Algorithm (spec-pinned; tuning constants below, no RNG anywhere):
  1. ffprobe duration/fps, then ffmpeg per-timestamp seek to grab <= max-frames
     frames at uniform timestamps (videos shorter than max-frames sample every
     frame). Each frame is grayed + downscaled to the analysis grid AT ARRIVAL
     (bounded memory: only the analysis stack plus the single full-resolution
     middle frame kept for --preview-out).
  2. Per-pixel temporal variance across samples;
     static mask = variance <= VARIANCE_THRESHOLD.
  3. Subtract a dilated *persistent edge* band (Canny edges present in >=
     PERSISTENT_EDGE_FRACTION of samples, morphologically CLOSED first to
     bridge short vote gaps): these are region boundaries / fixed window
     chrome that would otherwise merge the slide background with static window
     margin into one component. Each frame's edge mask is dilated by
     PERSISTENT_EDGE_VOTE_DILATE_ITERATIONS BEFORE voting, so boundary lines
     that wobble +-1px across frames (codec/resampling jitter — otherwise
     votes split below the threshold and the moat grows long gaps) still
     accumulate persistent votes; the post-dilation is reduced by the same
     amount so the net moat band width is unchanged. This is the spec's
     component filtering step, applied at mask level.
  4. Morphological opening removes speckle; the largest 8-connected static
     component (by raw pixel count) whose bounding box covers >=
     MIN_CANDIDATE_AREA_FRACTION of the frame is the candidate.
     Full-frame leak guard: if the candidate bbox covers >=
     FULL_FRAME_BBOX_FRACTION of the frame while the raw static mask covers <
     STATIC_DOMINANCE_FRACTION, a moat leak merged the slide with static
     chrome/chat; the result degrades to rect=null / reliable=false instead of
     confidently returning the full frame (deterministic, fail-safe).
  5. Score the candidate by Canny edge-density plausibility inside its bbox:
     text pages land in the mid-density plateau; near-empty or pure-noise
     regions and fully static black regions score low.

Confidence formula (exact):
    static_area_ratio = (raw pixel count of chosen static component)
                        / (area of its bounding box)
    edge_plausibility = piecewise-linear interpolation over EDGE_DENSITY_CURVE
                        at d = mean over sampled frames of (Canny edge pixels
                        inside bbox) / (bbox area)
    confidence        = static_area_ratio * edge_plausibility
    reliable          = confidence >= --min-confidence AND rect is non-null

Known limitations (fail-safe direction): a single text-dense slide shown for
>= ~60% of runtime shreds the slide interior with persistent text edges, so
confidence collapses and reliable=false (no region file is written; the user
draws the box in the UI). Rotation metadata (display matrix) is out of scope
for v1.

Outputs: --result-out JSON {"rect": {x,y,w,h} relative 0-1 | null,
"confidence", "reliable", "frame_count"}; --preview-out middle sampled frame as
full-resolution PNG with NO box burned in (the UI overlays the rect);
optionally --write-region region.json (§4.1) only when reliable.

Exit codes: 0 = detection ran (reliable or not — the result JSON tells);
1 = hard failure (missing video, no video stream, ffmpeg/ffprobe error or
timeout, zero decoded frames);
2 = --write-region requested but detection is unreliable (no region file).

Runs under vendor/tools-venv/bin/python (numpy + cv2); ffmpeg/ffprobe resolve
via PATH, then MST_FFMPEG candidates (same convention as prepare_audio.py).
Metadata only: logs/results never contain recognized content.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

MAX_ANALYSIS_WIDTH = 1280  # spec §3.1: downscale frames to width <= 1280
VARIANCE_THRESHOLD = 64.0  # gray-level temporal variance; fixture static bg measures ~2-30, motion regions ~700+
PERSISTENT_EDGE_FRACTION = 0.6  # edge seen in >= 60% of samples = fixed structure (region boundary / chrome)
PERSISTENT_EDGE_VOTE_DILATE_ITERATIONS = 1  # spatial vote tolerance (+-1px) for boundary lines wobbling across frames
PERSISTENT_EDGE_CLOSE_ITERATIONS = 2  # bridges remaining small vote gaps in persistent boundary lines
PERSISTENT_EDGE_DILATE_ITERATIONS = 1  # band half-width around persistent edges; vote dilation + close + this dilate = 5px net band (as shipped)
OPEN_KERNEL_SIZE = 3  # speckle removal on the static mask
MIN_CANDIDATE_AREA_FRACTION = 0.10  # component filter: bbox must cover >= 10% of the frame
FULL_FRAME_BBOX_FRACTION = 0.97  # leak guard: candidate bbox at/above this fraction of the frame is suspect
STATIC_DOMINANCE_FRACTION = 0.90  # leak guard: full-frame bbox trusted only when raw static pixels dominate
CANNY_LOW, CANNY_HIGH = 50, 150
# Edge-density plausibility curve (density -> score). Measured text-slide
# densities span 0.0039-0.026 across 320x200..2560x1600 sources on the
# analysis grid (text shrinks/blurs when large sources downscale), while
# near-empty or fully static black regions measure <= ~0.0005 and dense noise
# scores far above the plateau; the low knee sits between the two.
EDGE_DENSITY_CURVE = ((0.001, 0.0), (0.0025, 1.0), (0.15, 1.0), (0.35, 0.0))
FFPROBE_TIMEOUT_S = 60
FFMPEG_FRAME_TIMEOUT_S = 120

# Extra tool candidates come from MST_FFMPEG (colon-separated), exactly the
# prepare_audio.py convention; PATH search (shutil.which) stays primary — see
# INSTALL.md. ffprobe resolves as the sibling of each MST_FFMPEG ffmpeg entry.
KNOWN_FFMPEG = [
    Path(part)
    for part in (os.environ.get("MST_FFMPEG") or "").split(":")
    if part
]


def _fail(message: str) -> None:
    raise SystemExit(f"detect_slide_region: {message}")


def find_media_tool(name: str) -> Path:
    """Resolve ffmpeg/ffprobe: PATH first, then MST_FFMPEG candidates."""
    candidates: list[Path] = []
    discovered = shutil.which(name)
    if discovered:
        candidates.append(Path(discovered))
    for part in KNOWN_FFMPEG:
        candidates.append(part if part.name == name else part.parent / name)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    _fail(f"{name} not found on PATH or via MST_FFMPEG (see INSTALL.md)")


def probe_video(video: Path, ffprobe: Path) -> tuple[float, float | None, int, int]:
    """Return (duration_s, fps_or_None, width, height) via ffprobe."""
    try:
        proc = subprocess.run(
            [str(ffprobe), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(video)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _fail("ffprobe timed out")
    if proc.returncode != 0:
        _fail(f"ffprobe failed: {proc.stderr.strip()[-300:]}")
    try:
        info = json.loads(proc.stdout)
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if stream is None:
            _fail("no video stream found")
        width = int(stream["width"])
        height = int(stream["height"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _fail(f"ffprobe output lacks usable video geometry: {exc}")
    raw_duration = (info.get("format") or {}).get("duration") or stream.get("duration")
    try:
        duration = float(raw_duration)  # ffprobe may emit "N/A" -> ValueError
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        _fail("cannot determine video duration")
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key) or ""
        if "/" in rate:
            num, den = rate.split("/", 1)
            try:
                num_f, den_f = float(num), float(den)
            except ValueError:
                continue
            if num_f > 0 and den_f > 0:
                fps = num_f / den_f
                break
    return duration, fps, width, height


def sample_timestamps(duration: float, fps: float | None, max_frames: int) -> list[float]:
    """Uniform timestamps (segment centers); every frame when the video is short."""
    if fps is not None and fps > 0:
        total_frames = max(1, int(duration * fps))
        count = min(max_frames, total_frames)
    else:
        count = max_frames
    count = max(1, count)
    return [min((i + 0.5) * duration / count, max(duration - 0.05, 0.0)) for i in range(count)]


def analysis_size(width: int, height: int) -> tuple[int, int]:
    """Downscaled analysis grid (width <= MAX_ANALYSIS_WIDTH, aspect kept)."""
    scale = min(1.0, MAX_ANALYSIS_WIDTH / float(width))
    if scale >= 1.0:
        return width, height
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def decode_one_frame(video: Path, ffmpeg: Path, timestamp: float,
                     width: int, height: int) -> np.ndarray | None:
    """Decode a single full-res BGR frame; None when it cannot be decoded."""
    expected = width * height * 3
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            capture_output=True, timeout=FFMPEG_FRAME_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _fail(f"ffmpeg timed out decoding the frame at {timestamp:.3f}s")
    if proc.returncode != 0 or len(proc.stdout) < expected:
        return None
    return np.frombuffer(proc.stdout[:expected], dtype=np.uint8).reshape(height, width, 3).copy()


def sample_frames(video: Path, ffmpeg: Path, timestamps: list[float], width: int, height: int,
                  middle_index: int, ana_size: tuple[int, int]
                  ) -> tuple[list[np.ndarray], np.ndarray | None, list[float]]:
    """Sample one frame per timestamp; gray+downscale at arrival (bounded memory).

    Returns (analysis grays, full-res middle frame or None, decoded timestamps).
    Frames that fail to decode are skipped with a warning; zero decoded frames
    is a hard error.
    """
    ana_w, ana_h = ana_size
    grays: list[np.ndarray] = []
    decoded: list[float] = []
    middle: np.ndarray | None = None
    for i, ts in enumerate(timestamps):
        frame = decode_one_frame(video, ffmpeg, ts, width, height)
        if frame is None:
            print(f"detect_slide_region: warning: frame at {ts:.3f}s not decoded, skipped",
                  file=sys.stderr)
            continue
        if i == middle_index:
            middle = frame  # the only full-resolution frame retained (preview)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if (ana_w, ana_h) != (width, height):
            gray = cv2.resize(gray, (ana_w, ana_h), interpolation=cv2.INTER_AREA)
        grays.append(gray)
        decoded.append(ts)
    if not grays:
        _fail("ffmpeg extracted no frames from video")
    return grays, middle, decoded


def edge_plausibility(density: float) -> float:
    """Piecewise-linear bump over EDGE_DENSITY_CURVE."""
    curve = EDGE_DENSITY_CURVE
    if density <= curve[0][0] or density >= curve[-1][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= density <= x1:
            return y0 + (y1 - y0) * (density - x0) / (x1 - x0)
    return 0.0


def detect_region(grays: list[np.ndarray], ana_w: int, ana_h: int) -> tuple[dict | None, float]:
    """Spec §3.1 pipeline over the analysis grays; returns (relative rect | None, confidence)."""
    sample_count = len(grays)

    # Per-pixel temporal variance -> static mask. Streaming two-pass variance
    # (mean, then mean squared deviation) over the gray list: mathematically
    # the population variance np.var would compute, but without materializing
    # an n*H*W float stack (memory stays bounded for large sources).
    sum_acc = np.zeros((ana_h, ana_w), np.float32)
    for gray in grays:
        sum_acc += gray
    mean = sum_acc / sample_count
    variance = np.zeros((ana_h, ana_w), np.float32)
    for gray in grays:
        deviation = gray.astype(np.float32)
        deviation -= mean
        deviation *= deviation
        variance += deviation
    variance /= sample_count
    static_mask = np.where(variance <= VARIANCE_THRESHOLD, 255, 0).astype(np.uint8)
    frame_area = ana_w * ana_h
    # Raw (pre-moat) static dominance, used by the full-frame leak guard below.
    static_pixel_ratio = float(np.count_nonzero(static_mask)) / frame_area

    # Persistent edges (fixed boundaries/chrome) cut the static mask so separate
    # static regions do not merge into one component.
    edges = [cv2.Canny(gray, CANNY_LOW, CANNY_HIGH) for gray in grays]
    kernel3 = np.ones((3, 3), np.uint8)
    votes = np.zeros((ana_h, ana_w), dtype=np.int32)
    for edge in edges:
        # Dilate each frame's edge mask before voting: boundary lines wobbling
        # +-1px across frames (codec/resampling jitter) would otherwise split
        # their votes below the persistence threshold and leave moat gaps that
        # 8-connectivity leaks through (measured: up to 138px gaps at 320x200).
        votes += cv2.dilate((edge > 0).astype(np.uint8), kernel3,
                            iterations=PERSISTENT_EDGE_VOTE_DILATE_ITERATIONS)
    persistent = (votes >= max(1, math.ceil(PERSISTENT_EDGE_FRACTION * sample_count))).astype(np.uint8) * 255
    # Close remaining small vote gaps, then widen to the cut band.
    persistent = cv2.morphologyEx(persistent, cv2.MORPH_CLOSE, kernel3,
                                  iterations=PERSISTENT_EDGE_CLOSE_ITERATIONS)
    moat = cv2.dilate(persistent, kernel3, iterations=PERSISTENT_EDGE_DILATE_ITERATIONS)
    mask = cv2.bitwise_and(static_mask, cv2.bitwise_not(moat))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((OPEN_KERNEL_SIZE, OPEN_KERNEL_SIZE), np.uint8))

    # Largest static connected component with a big-enough bounding box.
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8)
    best = None  # (raw_px, x, y, w, h)
    for label in range(1, count):
        x, y, box_w, box_h, area = (int(v) for v in stats[label])
        if box_w * box_h < MIN_CANDIDATE_AREA_FRACTION * frame_area:
            continue
        if best is None or area > best[0]:
            best = (area, x, y, box_w, box_h)
    if best is None:
        return None, 0.0
    area, x, y, box_w, box_h = best
    bbox_area = box_w * box_h

    # Full-frame leak guard: a whole-frame bbox is only trusted when the frame
    # is genuinely static-dominated; otherwise the moat leaked -> fail safe.
    if (bbox_area >= FULL_FRAME_BBOX_FRACTION * frame_area
            and static_pixel_ratio < STATIC_DOMINANCE_FRACTION):
        return None, 0.0

    # confidence = static_area_ratio * edge_plausibility (formula in module docstring).
    static_area_ratio = area / float(bbox_area)
    densities = [np.count_nonzero(edge[y:y + box_h, x:x + box_w]) / float(bbox_area) for edge in edges]
    plausibility = edge_plausibility(float(np.mean(densities)))
    confidence = static_area_ratio * plausibility

    rect = {
        "x": round(min(max(x / float(ana_w), 0.0), 1.0), 6),
        "y": round(min(max(y / float(ana_h), 0.0), 1.0), 6),
        "w": round(min(max(box_w / float(ana_w), 0.0), 1.0), 6),
        "h": round(min(max(box_h / float(ana_h), 0.0), 1.0), 6),
    }
    return rect, round(confidence, 6)


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_preview_atomic(path: Path, image: np.ndarray) -> None:
    """Lossless PNG encode, then tmp+replace (same atomicity as JSON writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_ok, buffer = cv2.imencode(path.suffix or ".png", image)
    if not encoded_ok:
        _fail(f"failed encoding preview image: {path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(buffer.tobytes())
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--preview-out", required=True, type=Path)
    parser.add_argument("--result-out", required=True, type=Path)
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--write-region", type=Path,
                        help="also write a source=auto region.json here (only when reliable)")
    args = parser.parse_args()

    if not args.video.is_file():
        _fail(f"video not found: {args.video}")
    if args.max_frames < 1:
        _fail("--max-frames must be >= 1")

    ffmpeg = find_media_tool("ffmpeg")
    ffprobe = find_media_tool("ffprobe")
    duration, fps, width, height = probe_video(args.video, ffprobe)
    timestamps = sample_timestamps(duration, fps, args.max_frames)
    ana_size = analysis_size(width, height)
    grays, middle, decoded = sample_frames(
        args.video, ffmpeg, timestamps, width, height,
        middle_index=len(timestamps) // 2, ana_size=ana_size)
    rect, confidence = detect_region(grays, ana_size[0], ana_size[1])
    reliable = rect is not None and confidence >= args.min_confidence

    if middle is None:  # the middle timestamp failed to decode; re-decode a sampled one
        middle = decode_one_frame(args.video, ffmpeg, decoded[len(decoded) // 2], width, height)
        if middle is None:
            _fail("failed to decode any frame for the preview image")
    write_preview_atomic(args.preview_out, middle)

    write_json_atomic(args.result_out, {
        "rect": rect,
        "confidence": confidence,
        "reliable": reliable,
        "frame_count": len(grays),
    })
    print(f"{args.result_out} reliable={reliable} confidence={confidence:.3f} frames={len(grays)}")

    if args.write_region is not None:
        if not reliable:
            print("detect_slide_region: detection unreliable "
                  f"(confidence={confidence:.3f} < {args.min_confidence:.3f} or no plausible region); "
                  "region file not written", file=sys.stderr)
            return 2
        write_json_atomic(args.write_region, {
            "schema_version": 1,
            "video": args.video.name,
            "source": "auto",
            "rect": rect,
            "confidence": confidence,
            "created_ts": time.time(),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
