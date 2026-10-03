"""Deterministic synthetic "meeting window" video generator (test fixture).

Renders a fake online-meeting window capture: a static PPT share area whose
pages advance on a schedule (including revisits to earlier pages), a camera
tile with continuous random noise + a moving shape, a scrolling chat column,
and optional mouse crossings that sweep transiently over the PPT area.

Used by unit tests (detect_slide_region / extract_video_slides) and smoke
scripts as ground-truth input: the returned metadata records exactly which
page was shown during which time range, so segmentation/dedup assertions
compare against known truth instead of guessed expectations.

Runs under vendor/tools-venv python (needs numpy + cv2); ffmpeg/ffprobe come
from PATH. Encoding is pinned (-threads 1, fixed crf/preset) so the same
arguments produce byte-identical files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:  # system python3 without the tools venv
    cv2 = None
    np = None

# Deterministic per-page palette (BGR) and title words — ASCII only, because
# cv2.putText renders Hershey fonts (no CJK); Apple Vision OCR reads the
# resulting English slide text fine in e2e tests.
_PAGE_COLORS = [
    (245, 245, 245), (235, 244, 250), (250, 244, 232), (240, 250, 240), (248, 238, 248),
]
_PAGE_WORDS = ["Roadmap", "Budget", "Timeline", "Risks", "QA Plan", "Metrics", "Scope"]


def _require_deps() -> None:
    if cv2 is None or np is None:
        raise RuntimeError("synth_video requires numpy+cv2 (run under vendor/tools-venv/bin/python)")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("synth_video requires ffmpeg on PATH")


def _even(value: float) -> int:
    return int(value) // 2 * 2


def _rect_px(rect: tuple[float, float, float, float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = rect
    width, height = size
    px, py = _even(x * width), _even(y * height)
    pw, ph = _even(w * width), _even(h * height)
    return px, py, max(pw, 2), max(ph, 2)


def default_schedule(duration_s: float) -> list[tuple[int, float, float]]:
    """5 segments over 4 unique pages; page 1 is revisited (dedup ground truth).

    Layout: page0 [0, .2D) page1 [.2D, .4D) page2 [.4D, .55D)
            page1 again [.55D, .7D) page3 [.7D, D]
    """
    cuts = [0.0, 0.2, 0.4, 0.55, 0.7, 1.0]
    pages = [0, 1, 2, 1, 3]
    return [(pages[i], round(duration_s * cuts[i], 3), round(duration_s * cuts[i + 1], 3))
            for i in range(len(pages))]


def _draw_slide(frame, box, page: int) -> None:
    """Render one static PPT page into box=(x,y,w,h): solid bg, title, page-specific shapes."""
    x, y, w, h = box
    color = _PAGE_COLORS[page % len(_PAGE_COLORS)]
    frame[y:y + h, x:x + w] = color
    title = f"Topic {page + 1}: {_PAGE_WORDS[page % len(_PAGE_WORDS)]}"
    scale = max(0.5, min(1.2, w / 700.0))
    cv2.putText(frame, title, (x + int(w * 0.06), y + int(h * 0.16)),
                cv2.FONT_HERSHEY_SIMPLEX, scale, (30, 30, 30), 2, cv2.LINE_AA)
    # Page-keyed static shapes: distinct pHash per page, zero motion within a page.
    # RNG seeded by page only (independent of `seed`) on purpose: a page keeps its
    # visual identity across generator seeds so dedup tests compare pages reliably.
    rng = np.random.default_rng([7, page])
    for i in range(page + 2):
        sx = x + int(rng.uniform(0.05, 0.6) * w)
        sy = y + int(h * 0.3) + int(rng.uniform(0.0, 0.4) * h)
        sw_lo, sw_hi = max(int(w * 0.08), 2), max(int(w * 0.3), 4)
        sh_lo, sh_hi = max(int(h * 0.08), 2), max(int(h * 0.25), 4)
        sw = int(rng.integers(sw_lo, sw_hi))
        sh = int(rng.integers(sh_lo, sh_hi))
        shade = tuple(int(v) for v in rng.integers(40, 180, 3))
        if (page + i) % 2 == 0:
            cv2.rectangle(frame, (sx, sy), (min(sx + sw, x + w - 4), min(sy + sh, y + h - 4)), shade, -1)
        else:
            cv2.ellipse(frame, (min(sx + sw // 2, x + w - 4), min(sy + sh // 2, y + h - 4)),
                        (sw // 2, sh // 2), 0, 0, 360, shade, -1)
    cv2.putText(frame, f"- detail item {page + 1}.a", (x + int(w * 0.06), y + int(h * 0.82)),
                cv2.FONT_HERSHEY_SIMPLEX, scale * 0.55, (70, 70, 70), 1, cv2.LINE_AA)


def _draw_camera(frame, box, t: float, seed: int) -> None:
    """Camera tile: per-frame seeded noise blocks + a shape moving with time (never static)."""
    x, y, w, h = box
    frame[y:y + h, x:x + w] = (50, 48, 46)
    rng = np.random.default_rng([seed, int(t * 1000)])
    tile = frame[y:y + h, x:x + w]
    noise = rng.integers(0, 90, (max(h // 8, 1), max(w // 8, 1), 3), dtype=np.uint8)
    tile[:] = cv2.resize(noise, (w, h), interpolation=cv2.INTER_NEAREST)
    cx = x + int(w * (0.5 + 0.3 * np.sin(t * 1.7)))
    cy = y + int(h * (0.5 + 0.25 * np.cos(t * 1.3)))
    cv2.circle(frame, (cx, cy), max(min(w, h) // 8, 4), (200, 210, 220), -1)


def _draw_chat(frame, box, t: float, seed: int) -> None:
    """Chat column: continuously scrolling message lines (always in motion)."""
    x, y, w, h = box
    frame[y:y + h, x:x + w] = (38, 38, 38)
    rng = np.random.default_rng(seed + 1)
    line_h = max(h // 14, 10)
    total_lines = 40
    scroll = (t * 2.2 * line_h) % (total_lines * line_h)
    widths = rng.integers(int(w * 0.35), int(w * 0.9), total_lines)
    for i in range(total_lines + 2):
        ly = y + h - int(scroll) + i * line_h - line_h
        if y <= ly < y + h - 2:
            lw = int(widths[i % total_lines])
            cv2.rectangle(frame, (x + 6, ly), (x + 6 + min(lw, w - 12), ly + line_h - 4),
                          (90, 110, 90 + (i * 7) % 60), -1)


def _draw_mouse(frame, ppt_box, progress: float) -> None:
    """Small cursor sweeping diagonally across the PPT area (transient, tiny footprint)."""
    x, y, w, h = ppt_box
    mx = x + int(w * (0.1 + 0.8 * progress))
    my = y + int(h * (0.85 - 0.7 * progress))
    cv2.arrowedLine(frame, (mx, my), (mx + 14, my + 18), (20, 20, 20), 2, tipLength=0.5)


def generate_meeting_video(
    path,
    *,
    duration_s: float = 20.0,
    fps: int = 10,
    size: tuple[int, int] = (1280, 800),
    ppt_rect: tuple[float, float, float, float] | None = (0.05, 0.05, 0.62, 0.9),
    camera_rect: tuple[float, float, float, float] = (0.70, 0.05, 0.27, 0.30),
    chat_rect: tuple[float, float, float, float] = (0.70, 0.38, 0.27, 0.57),
    schedule: list[tuple[int, float, float]] | None = None,
    audio: bool = False,
    seed: int = 1234,
    mouse_crossings: list[tuple[float, float]] | None = None,
) -> dict:
    """Render the synthetic meeting video and return its ground-truth metadata.

    ppt_rect=None fills the whole window with moving content (no static slide
    region) — the fixture for "no reliable PPT region" detection tests.
    mouse_crossings defaults to one fixed-length 1.5s sweep starting 0.5s into
    segment 2; on short videos it may straddle a page cut (both the sweep
    window and the segments are in the returned metadata, so consumers can
    account for it). A custom `schedule` must be sorted, non-overlapping and
    start at 0.0; time past the last segment's end renders the last page
    (and the ground truth reflects the schedule as given).

    Returned metadata keys: path, duration_s, fps, size, audio, seed,
    ppt_rect_px / camera_rect_px / chat_rect_px (absolute even-aligned boxes,
    ppt None in all-motion mode), segments [{page,start,end}...],
    unique_pages [{page, ranges:[[s,e],...]}...] grouped per page (revisited
    pages keep their separate ranges), mouse_crossings.
    """
    _require_deps()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = _even(size[0]), _even(size[1])
    if schedule is None:
        schedule = default_schedule(duration_s)
    else:
        for (pa, sa, ea), (pb, sb, _eb) in zip(schedule, schedule[1:]):
            if sb < ea or sb < sa:
                raise ValueError(f"custom schedule must be sorted and non-overlapping: {(pa, sa, ea)} then {(pb, sb)}")
        if schedule and schedule[0][1] != 0.0:
            raise ValueError("custom schedule must start at 0.0")
    if mouse_crossings is None and ppt_rect is not None:
        mid_start = schedule[1][1]
        mouse_crossings = [(mid_start + 0.5, mid_start + 2.0)]
    mouse_crossings = mouse_crossings or []

    ppt_box = _rect_px(ppt_rect, (width, height)) if ppt_rect is not None else None
    camera_box = _rect_px(camera_rect, (width, height))
    chat_box = _rect_px(chat_rect, (width, height))

    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}"]
    # Output options must follow ALL inputs, or ffmpeg binds them to the next -i.
    cmd += ["-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "64k", "-shortest"]
    cmd.append(str(path))

    total_frames = int(round(duration_s * fps))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for idx in range(total_frames):
            t = idx / fps
            frame = np.full((height, width, 3), 28, dtype=np.uint8)  # window chrome
            page = schedule[-1][0]
            for page_i, start, end in schedule:
                if start <= t < end:
                    page = page_i
                    break
            if ppt_box is not None:
                _draw_slide(frame, ppt_box, page)
            else:
                _draw_camera(frame, (0, 0, width, height), t, seed)  # all-motion variant
            _draw_camera(frame, camera_box, t, seed)
            _draw_chat(frame, chat_box, t, seed)
            for m_start, m_end in mouse_crossings:
                if m_start <= t <= m_end:
                    _draw_mouse(frame, ppt_box or (0, 0, width, height),
                                (t - m_start) / max(m_end - m_start, 1e-6))
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        # Read stderr to EOF BEFORE wait() to avoid pipe-buffer deadlock.
        stderr_tail = (proc.stderr.read() or b"").decode("utf-8", "replace")[-500:]
        proc.stderr.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg failed encoding {path}: {stderr_tail}")

    # Ground truth: unique pages with merged display ranges (revisits merge).
    unique: dict[int, list[list[float]]] = {}
    for page_i, start, end in schedule:
        unique.setdefault(page_i, []).append([start, end])
    return {
        "path": str(path),
        "duration_s": duration_s,
        "fps": fps,
        "size": (width, height),
        "audio": audio,
        "seed": seed,
        "ppt_rect_px": ppt_box,
        "camera_rect_px": camera_box,
        "chat_rect_px": chat_box,
        "segments": [{"page": p, "start": s, "end": e} for p, s, e in schedule],
        "unique_pages": [{"page": p, "ranges": unique[p]} for p in sorted(unique)],
        "mouse_crossings": mouse_crossings,
    }


def probe(path) -> dict:
    """ffprobe summary used by test assertions: duration/streams/size."""
    if shutil.which("ffprobe") is None:
        raise RuntimeError("synth_video.probe requires ffprobe on PATH")
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60, check=True)
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_s = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(info["format"]["duration"]),
        "has_video": video is not None,
        "has_audio": audio_s is not None,
        "width": int(video["width"]) if video else None,
        "height": int(video["height"]) if video else None,
    }
