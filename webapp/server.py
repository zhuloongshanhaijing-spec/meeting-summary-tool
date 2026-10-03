"""Web console control plane — zero-dependency (stdlib only).

Owner of the local HTTP surface defined in docs/web-console-design.md §3.2:
static console page, multipart upload into input/<event>/, single-pipeline
supervision (queue = input/, iron rule: never two pipelines), /api/status
aggregation from disk state, outputs browsing + zip, log tailing — plus the
screen-recording region surface (detect/preview/confirm, design spec
docs/screen-recording-parsing-design.md §3.5).

State model (spec §2, single source of truth per concern):
  input/  = queue          runs/   = progress + artifacts
  outputs/= results        No authoritative in-memory state: a restarted
  server re-derives everything from disk (pidfile + jsonl + dir scans).
"""
from __future__ import annotations

import io
import json
import math
import os
import platform
import re
import shutil
from collections import defaultdict
import signal
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "input"
RUNS_DIR = ROOT / "runs"
OUTPUTS_DIR = ROOT / "outputs"
PROGRESS_JSONL = RUNS_DIR / "progress.jsonl"
WEBAPP_STATE_DIR = RUNS_DIR / ".webapp"
PID_FILE = WEBAPP_STATE_DIR / "pipeline.pid"
PIPELINE_LOG = WEBAPP_STATE_DIR / "pipeline.log"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Must mirror run_meeting.find_input_events (contract, spec §3.2)
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".caf"}
NOTE_EXTS = {".md", ".markdown", ".txt"}
NOTE_CANONICAL = {"notes.md", "note.md"}
# Must mirror meeting_pipeline.VIDEO_EXTENSIONS — single source of truth lives
# there (screen-recording spec §3.5/§3.7); same mirror convention as AUDIO_EXTS
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
EVENT_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]*$")
MAX_AUTO_RESTARTS = 50
DEFAULT_PORT = 8788
# Synchronous slide-region detection budget (screen-recording spec §3.5);
# on expiry the detector's whole process group is killed (it spawns ffmpeg).
REGION_DETECT_TIMEOUT_S = 120
# /api/start whole-batch gate prefix (spec §5 v1: one unconfirmed recording
# blocks the batch; the handler keys the structured body off this prefix)
REGION_GATE_MSG = "录屏区域未确认："
DEPENDENCY_IDS = ("ffmpeg", "ollama", "whisper", "qwen", "tools")
QWEN_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
DEPENDENCY_PLAN = INPUT_DIR / ".meeting-plan.json"
FRAGMENT_PLAN = RUNS_DIR / ".fragment-reading-order.json"

_supervision: dict = {"proc": None, "thread": None, "stop_requested": False,
                      "auto_restarts": 0}
_install: dict = {"proc": None, "ids": [], "started": None, "error": ""}


# --------------------------------------------------------------------------
# Small pure helpers (unit-tested directly)
# --------------------------------------------------------------------------

def sanitize_event_name(name: str | None) -> str | None:
    """Whitelist check for event names; None when invalid/empty."""
    name = (name or "").strip()
    if not name:
        return None
    if not EVENT_NAME_RE.match(name) or name.startswith("."):
        return None
    return name


def sanitize_filename(name: str) -> str | None:
    """Strip any path components; reject empty/dotfile/weird chars."""
    name = Path(unquote(name or "")).name.strip()
    if not name or name.startswith(".") or name != name.strip():
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    return name


def safe_join(base: Path, relative: str) -> Path | None:
    """Join and verify the result stays under base (traversal guard)."""
    try:
        candidate = (base / relative).resolve()
        base_resolved = base.resolve()
        if candidate == base_resolved or base_resolved in candidate.parents:
            return candidate
    except (OSError, ValueError):
        pass
    return None


def default_event_name() -> str:
    return time.strftime("web-%Y%m%d-%H%M%S")


def dependency_status() -> dict:
    """A non-mutating local inventory.  It never uploads data or starts apps."""
    vendor = ROOT / "vendor"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import config
        cfg = config.resolve()
    except SystemExit:
        cfg = {}
    whisper_bin = Path(cfg.get("whisper_bin") or vendor / "whisper.cpp" / "build" / "bin" / "whisper-cli")
    whisper_model = Path(cfg.get("whisper_model") or vendor / "models" / "ggml-large-v3-turbo-q5_0.bin")
    qwen_python = Path(cfg["qwen_python"]) if cfg.get("qwen_python") else vendor / "qwen-asr-venv" / "bin" / "python"
    ollama_model = cfg.get("ollama_model") or "qwen3:8b"
    ollama_ready = False
    try:
        with urllib.request.urlopen((cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/") + "/api/tags", timeout=2) as response:
            tags = json.loads(response.read().decode("utf-8")).get("models", [])
            ollama_ready = any(m.get("name") == ollama_model for m in tags if isinstance(m, dict))
    except Exception:
        pass
    qwen_ready = False
    if qwen_python.is_file() and os.access(qwen_python, os.X_OK):
        # This invokes only the selected local interpreter. local_files_only
        # forbids a network fetch while checking packages and HF cache.
        probe = ("import torch,transformers,accelerate,qwen_asr; "
                 "from huggingface_hub import snapshot_download; "
                 f"snapshot_download({QWEN_ASR_MODEL!r}, local_files_only=True)")
        try:
            result = subprocess.run([str(qwen_python), "-c", probe], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            qwen_ready = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks = {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ollama": ollama_ready,
        "whisper": whisper_bin.is_file() and os.access(whisper_bin, os.X_OK) and whisper_model.is_file() and whisper_model.stat().st_size > 0,
        "qwen": qwen_ready,
        "tools": (vendor / "tools-venv" / "bin" / "python").is_file(),
    }
    meta = {
        "ffmpeg": ("音频与录屏解码", True, "https://ffmpeg.org/download.html"),
        "ollama": ("本机报告整理模型", True, "https://ollama.com/download"),
        "whisper": ("本机 Whisper 转写", True, "https://github.com/ggerganov/whisper.cpp"),
        "qwen": ("中文或中英混合语音增强转写（Qwen3-ASR-1.7B）", False, "https://github.com/QwenLM/Qwen3-ASR"),
        "tools": ("录屏幻灯片与 OCR 工具", False, "https://opencv.org/"),
    }
    sizes = {"ffmpeg": "基础工具", "ollama": "本机模型约 5 GB", "whisper": "模型约 575 MB",
             "qwen": "增强模型为数 GB", "tools": "仅录屏额外工具"}
    items = [{"id": key, "ready": checks[key], "purpose": meta[key][0],
              "required": meta[key][1], "tier": "基础" if meta[key][1] else "增强",
              "download": sizes[key], "official_url": meta[key][2]}
             for key in DEPENDENCY_IDS]
    proc = _install.get("proc")
    running = bool(proc is not None and proc.poll() is None)
    if proc is not None and not running and proc.returncode:
        _install["error"] = f"安装进程退出码 {proc.returncode}；可重试或查看本地日志。"
    return {"items": items, "installing": running, "installing_ids": _install["ids"],
            "error": _install["error"], "local_only": True}


def install_dependencies(ids) -> tuple[int, dict]:
    """Only called by an explicit POST from the local page; setup.sh stays local."""
    wanted = [x for x in ids if x in DEPENDENCY_IDS]
    if not wanted:
        return 400, {"error": "请选择至少一个可安装依赖"}
    proc = _install.get("proc")
    if proc is not None and proc.poll() is None:
        return 409, {"error": "已有安装在进行中", "ids": _install["ids"]}
    skipped = " ".join(x for x in DEPENDENCY_IDS if x not in wanted)
    env = os.environ.copy()
    env["MST_SETUP_SKIP"] = skipped
    log = WEBAPP_STATE_DIR / "dependency-install.log"
    WEBAPP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(["bash", str(ROOT / "setup.sh")], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
    _install.update({"proc": proc, "ids": wanted, "started": time.time(), "error": ""})
    return 202, {"ok": True, "message": "本地安装已开始", "ids": wanted}


def candidate_plan() -> dict:
    """Build a plan only from already-local parse receipts, metadata and notes.
    Raw file mtimes are deliberately not treated as a meeting order."""
    candidates = []
    # Completed runs survive normal input cleanup.  They are the authoritative
    # source for a post-parse plan; queued input is included only when a run
    # still exists for it, never as raw-mtime ordering evidence.
    queued = {q["name"]: q for q in scan_queue()}
    parsed_names = []
    if RUNS_DIR.exists():
        for run in RUNS_DIR.iterdir():
            if (run.is_dir() and not run.name.startswith(".")
                    and (run / "manifest.json").is_file()
                    and (run / "literal_records.jsonl").is_file()
                    and (run / "literal_records.jsonl").stat().st_size > 0):
                parsed_names.append(run.name)
    timeline_starts = {}
    if PROGRESS_JSONL.exists():
        for row in tail_jsonl(100000):
            if row.get("kind") == "event_start" and isinstance(row.get("ts"), (int, float)):
                timeline_starts.setdefault(row.get("event"), row["ts"])
    for name in sorted(set(queued) | set(parsed_names)):
        q = queued.get(name, {"name": name, "files": 0, "notes": 0})
        d = INPUT_DIR if name == "misc" else INPUT_DIR / name
        run = RUNS_DIR / name
        manifest = _read_json(run / "manifest.json") or {}
        records = run / "literal_records.jsonl"
        parsed = records.is_file() and records.stat().st_size > 0
        # A progress event_start is a real local timeline observation. Manifest
        # creation and file mtimes are not meeting-time evidence, so they do
        # not manufacture a candidate order when this receipt is absent.
        start = timeline_starts.get(name)
        candidates.append({"event": name, "order_key": start if parsed else None,
                           "files": q.get("files") or manifest.get("file_count", 0), "independent": True, "parsed": parsed,
                           "evidence": [x for x in ("local transcript" if parsed else None,
                                                       "local progress timeline" if start is not None else None,
                                                       "notes" if q.get("notes") else None) if x]})
    candidates.sort(key=lambda x: (x["order_key"] is None, x["order_key"] or 0, x["event"]))
    uncertainty = []
    if len(candidates) > 1:
        uncertainty.append("候选顺序只可依据已完成的本地转写、进度时间线和笔记；没有可靠时间证据的会议保持未排序。")
        uncertainty.append("候选会议之间不会合并；缺失片段会保留为缺失，不会补写。")
    saved = _read_json(DEPENDENCY_PLAN) or {}
    return {"candidates": candidates, "uncertainties": uncertainty,
            "confirmed": saved.get("confirmed") == [x["event"] for x in candidates],
            "ready_to_confirm": bool(candidates) and all(x["parsed"] and x["order_key"] is not None for x in candidates)}


def confirm_candidate_plan(order) -> tuple[int, dict]:
    plan = candidate_plan()
    actual = [x["event"] for x in plan["candidates"]]
    if not plan["ready_to_confirm"]:
        return 409, {"error": "候选会议尚未全部独立解析；当前不能确认顺序"}
    if sorted(order) != sorted(actual):
        return 400, {"error": "确认列表与当前本地候选会议不一致，请刷新后重试"}
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(DEPENDENCY_PLAN, {"confirmed": order, "confirmed_ts": time.time()})
    return 200, {"ok": True, "order": order}


def _fragment_words(text: str) -> set[str]:
    """Local lexical features: CJK character bigrams plus Latin word tokens."""
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    out = {cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))}
    out.update(re.findall(r"[A-Za-z0-9']+", text.lower()))
    return out


def fragment_reading_plan() -> dict:
    """Conservative post-parse candidate graph for independently processed
    fragment events. It never concatenates transcripts or creates a report."""
    fragments = []
    references, note_segments = [], []
    ref_dir = RUNS_DIR / ".fragment-references"
    if ref_dir.is_dir():
        for path in sorted(p for p in ref_dir.iterdir() if p.is_file()):
            references.append(path.name)
            try:
                # Preserve file/paragraph/bullet order. These are hints only.
                note_segments.extend((path.name, line.strip()) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
            except OSError:
                pass
    if RUNS_DIR.exists():
        for run in sorted(RUNS_DIR.iterdir(), key=lambda p: p.name):
            records = run / "literal_records.jsonl"
            if not (run.is_dir() and run.name.startswith("fragment-") and records.is_file()):
                continue
            try:
                rows = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, ValueError):
                continue
            texts = [str(r.get("clean_literal") or "").strip() for r in rows]
            texts = [x for x in texts if x]
            if not texts:
                continue
            fragments.append({"id": run.name, "source": next((p.name for p in (run / "source").glob("*") if p.is_file()), run.name),
                              "duration_seconds": max((float(r.get("end_seconds") or 0) for r in rows), default=0),
                              "head": texts[0][:180], "tail": texts[-1][-180:]})
    for fragment in fragments:
        words = _fragment_words(fragment["head"] + " " + fragment["tail"])
        best = (0.0, None, None)
        for index, (source, segment) in enumerate(note_segments):
            other = _fragment_words(segment)
            score = len(words & other) / len(words | other) if words and other else 0.0
            if score > best[0]: best = (score, index, source)
        fragment["note_hint_score"] = round(best[0], 3)
        fragment["note_hint_index"] = best[1] if best[0] >= 0.10 else None
        fragment["note_hint_source"] = best[2] if best[0] >= 0.10 else None
    # Directed edge means "tail of A resembles head of B". A deliberately
    # high threshold avoids turning thematic similarity into a false meeting.
    edges = []
    for left in fragments:
        a = _fragment_words(left["tail"])
        for right in fragments:
            if left["id"] == right["id"]:
                continue
            b = _fragment_words(right["head"])
            score = len(a & b) / len(a | b) if a and b else 0.0
            if score >= 0.20:
                edges.append((score, left["id"], right["id"]))
    # Admit only a clearly strongest edge at both endpoints. Ambiguous ties
    # stay unlinked rather than inventing a reading sequence.
    outgoing, incoming = {}, {}
    for score, a, b in edges:
        outgoing.setdefault(a, []).append((score, b))
        incoming.setdefault(b, []).append((score, a))
    admitted = []
    for a, choices in outgoing.items():
        choices.sort(reverse=True)
        score, b = choices[0]
        if len(choices) > 1 and score - choices[1][0] < 0.05:
            continue
        reverse = sorted(incoming[b], reverse=True)
        if reverse[0][1] != a or (len(reverse) > 1 and reverse[0][0] - reverse[1][0] < 0.05):
            continue
        admitted.append((score, a, b))
    # Components use only admitted continuity edges; isolated files remain
    # separate instead of being guessed into a meeting.
    parent = {x["id"]: x["id"] for x in fragments}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        a, b = find(a), find(b)
        if a != b: parent[b] = a
    for _score, a, b in admitted: union(a, b)
    groups = defaultdict(list)
    for f in fragments: groups[find(f["id"])].append(f)
    result = []
    for members in groups.values():
        member_ids = {x["id"] for x in members}
        next_of = {a: b for _s, a, b in admitted if a in member_ids and b in member_ids}
        has_incoming = {b for _s, a, b in admitted if a in member_ids and b in member_ids}
        starts = sorted(member_ids - has_incoming)
        ordered_ids = []
        for start in starts:
            cur = start
            while cur not in ordered_ids:
                ordered_ids.append(cur)
                if cur not in next_of: break
                cur = next_of[cur]
        ordered_ids.extend(sorted(member_ids - set(ordered_ids)))
        by_id = {x["id"]: x for x in members}
        ordered = [by_id[x] for x in ordered_ids]
        hint = min((x.get("note_hint_index") for x in ordered if x.get("note_hint_index") is not None), default=None)
        result.append({"fragments": ordered, "confidence": "low" if len(ordered) == 1 else "candidate",
                       "gap_or_uncertain": len(ordered) == 1, "note_order_hint": hint})
    # Note order is only a secondary display hint for disconnected groups;
    # it never adds an edge or changes directed transcript paths above.
    result.sort(key=lambda g: (g["note_order_hint"] is None, g["note_order_hint"] if g["note_order_hint"] is not None else 0,
                               g["fragments"][0]["id"] if g["fragments"] else ""))
    saved = _read_json(FRAGMENT_PLAN) or {}
    ids = [f["id"] for g in result for f in g["fragments"]]
    return {"groups": result, "edges": [{"from": a, "to": b, "score": round(s, 3)} for s, a, b in admitted], "references": references,
            "confirmed": saved.get("fragment_ids") == ids,
            "notice": "仅生成已确认阅读顺序；各片段报告保持独立，不自动合并或补写缺段。"}


def confirm_fragment_reading_order() -> tuple[int, dict]:
    plan = fragment_reading_plan()
    ids = [f["id"] for g in plan["groups"] for f in g["fragments"]]
    if not ids:
        return 400, {"error": "没有已解析的散乱片段"}
    _atomic_write_json(FRAGMENT_PLAN, {"fragment_ids": ids, "confirmed_ts": time.time(),
                                       "kind": "reading_order_only"})
    return 200, {"ok": True, "fragment_ids": ids}


def scan_queue() -> list[dict]:
    """input/ is the queue (same grouping rules as find_input_events).

    Screen-recording spec §3.5: events carry a video count, and an event with
    video but no region.json is awaiting_region (gates /api/start until the
    user confirms the slide region). Dotfiles (.region-preview.png /
    .region-detect.json) never count — same convention as find_input_events."""
    queue: list[dict] = []
    if not INPUT_DIR.exists():
        return queue
    loose_audio = loose_notes = loose_video = 0
    for entry in sorted(INPUT_DIR.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            files = [p for p in entry.iterdir()
                     if p.is_file() and not p.name.startswith(".")]
            audio = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
            video = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
            notes = [p for p in files
                     if p.suffix.lower() in NOTE_EXTS or p.name.lower() in NOTE_CANONICAL]
            if audio or notes or video:
                queue.append({"name": entry.name, "audio": len(audio),
                              "notes": len(notes), "video": len(video),
                              "files": len(audio) + len(notes) + len(video),
                              "awaiting_region": bool(video)
                              and not (entry / "region.json").is_file()})
        elif entry.suffix.lower() in AUDIO_EXTS:
            loose_audio += 1
        elif entry.suffix.lower() in VIDEO_EXTS:
            loose_video += 1
        elif entry.name.lower() in NOTE_CANONICAL or entry.suffix.lower() in NOTE_EXTS:
            loose_notes += 1
    if loose_audio or loose_notes or loose_video:  # loose files share one "misc" event
        queue.append({"name": "misc", "audio": loose_audio,
                      "notes": loose_notes, "video": loose_video,
                      "files": loose_audio + loose_notes + loose_video,
                      "awaiting_region": bool(loose_video)
                      and not (INPUT_DIR / "region.json").is_file()})
    return queue


def rebuild_notes_md(event_dir: Path) -> None:
    """notes.md = merge of every note-kind file in the event dir (except
    itself). A single file already named notes.md/note.md passes through
    untouched (spec §3.2)."""
    note_files = sorted(
        p for p in event_dir.iterdir()
        if p.is_file() and not p.name.startswith(".")
        and p.suffix.lower() in NOTE_EXTS and p.name != "notes.md")
    if not note_files:
        return
    if len(note_files) == 1 and note_files[0].name.lower() in NOTE_CANONICAL:
        return  # exact canonical name: pipeline reads it directly
    parts = ["# 笔记（网页端合并）", ""]
    for p in note_files:
        parts.append(f"## 来源: {p.name}")
        parts.append("")
        parts.append(p.read_text(encoding="utf-8", errors="replace").strip())
        parts.append("")
    tmp = event_dir / ".notes.md.part"
    tmp.write_text("\n".join(parts), encoding="utf-8")
    os.replace(tmp, event_dir / "notes.md")
    # find_input_events picks whichever canonical name iterdir yields first;
    # a leftover note.md next to a merged notes.md would be nondeterministic,
    # so the colliding original is renamed (content preserved, never deleted)
    collision = event_dir / "note.md"
    if collision.exists():
        collision.rename(event_dir / "note.md.orig")


# --------------------------------------------------------------------------
# Screen-recording region confirmation (design spec §3.5):
# detect (sync, cached) / preview dotfile serving / confirm → region.json §4.1
# --------------------------------------------------------------------------

def event_input_dir(name: str | None) -> Path | None:
    """Sanitized event name → its queue dir; None when the name is invalid.
    "misc" = the loose files directly under input/ (mirrors the misc event of
    find_input_events, whose dir is input/ itself)."""
    name = sanitize_event_name(name)
    if name is None:
        return None
    if name == "misc":
        return INPUT_DIR
    return safe_join(INPUT_DIR, name)


def event_videos(event_dir: Path) -> list[Path]:
    """Non-dotfile videos of an event, sorted by name — v1 processes only the
    first one (spec §5「多视频事件仅处理首个」)."""
    return sorted(p for p in event_dir.iterdir()
                  if p.is_file() and not p.name.startswith(".")
                  and p.suffix.lower() in VIDEO_EXTS)


def awaiting_region_events() -> list[str]:
    """Queued events that have video but no confirmed region.json (§3.5)."""
    return [q["name"] for q in scan_queue() if q.get("awaiting_region")]


def _atomic_write_json(path: Path, payload: dict) -> None:
    """tmp+rename write (house rule); the tmp name is a dotfile so queue and
    pipeline scanners never observe a partial artifact."""
    tmp = path.with_name("." + path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _clamp_rect(raw) -> dict | None:
    """Validate + clamp a relative rect (spec §3.5: fields clamp to [0,1]).

    w/h are additionally SHRUNK to keep the box inside the frame (x+w ≤ 1,
    y+h ≤ 1): the §4.1 validity gates downstream (run_meeting
    ._valid_region_file, extract_video_slides.resolve_crop) silently discard
    overflowing regions — an overflow here would throw away the user's
    confirmation and fall back to auto-detect. w/h must stay > 0 AFTER both
    clamps. Bool inputs are rejected (JSON true must not coerce to 1.0);
    numeric strings stay accepted (JS clients). None = reject with a 400."""
    if not isinstance(raw, dict):
        return None
    if any(isinstance(raw.get(k), bool) for k in ("x", "y", "w", "h")):
        return None
    try:
        vals = {k: float(raw[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in vals.values()):
        return None
    rect = {k: round(min(max(v, 0.0), 1.0), 6) for k, v in vals.items()}
    rect["w"] = round(min(rect["w"], 1.0 - rect["x"]), 6)
    rect["h"] = round(min(rect["h"], 1.0 - rect["y"]), 6)
    if rect["w"] <= 0.0 or rect["h"] <= 0.0:
        return None
    return rect


def _tools_python() -> str:
    """Interpreter of vendor/tools-venv — config.SPEC precedence
    (MST_TOOLS_PYTHON > config.json > default). Raises SystemExit with the
    collective config error when required settings are incomplete."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import config
    return config.resolve()["tools_python"]


def _detect_payload(result: dict, preview_url: str, warning: str | None,
                    cached: bool) -> dict:
    payload = {"cached": cached, "reliable": bool(result.get("reliable")),
               "rect": result.get("rect"), "confidence": result.get("confidence"),
               "preview_url": preview_url}
    if warning:
        payload["warning"] = warning
    return payload


def region_detect(event_name: str) -> tuple[int, dict]:
    """POST /api/region/detect — run detect_slide_region.py synchronously.

    Blocking this worker thread up to REGION_DETECT_TIMEOUT_S follows the
    threaded server's established slow-endpoint pattern (_pick_folder: 600s).
    On timeout the whole process GROUP is SIGKILLed (the detector spawns
    ffmpeg children). Results cache in the .region-detect.json dotfile, keyed
    by video name/mtime/size; the preview frame is .region-preview.png, served
    by GET /api/events/<event>/region_preview.png. The server never passes
    --write-region: region.json is written only by region_confirm."""
    event = sanitize_event_name(event_name)
    if event is None:
        return 400, {"error": "事件名无效（仅中文/字母/数字/连字符）"}
    event_dir = event_input_dir(event)
    if event_dir is None or not event_dir.is_dir():
        return 404, {"error": "事件不存在"}
    videos = event_videos(event_dir)
    if not videos:
        return 400, {"error": "事件中没有视频文件，无法检测幻灯片区域"}
    video = videos[0]
    warning = "多视频事件仅处理首个" if len(videos) > 1 else None
    preview = event_dir / ".region-preview.png"
    cache_path = event_dir / ".region-detect.json"
    preview_url = f"/api/events/{quote(event)}/region_preview.png"
    stat = video.stat()

    cached = _read_json(cache_path)
    if (isinstance(cached, dict) and "rect" in cached and preview.is_file()
            and cached.get("video") == video.name
            and cached.get("video_mtime") == stat.st_mtime
            and cached.get("video_size") == stat.st_size):
        return 200, _detect_payload(cached, preview_url, warning, cached=True)

    try:
        tools = _tools_python()
    except SystemExit as exc:  # config.resolve() collective error
        return 500, {"error": "工具环境未就绪：请先运行 setup.sh 或补全 config.json 配置",
                     "detail": str(exc)}
    script = ROOT / "core" / "scripts" / "detect_slide_region.py"
    if not Path(str(tools)).is_file() or not script.is_file():
        return 500, {"error": "工具环境未就绪：缺少 vendor/tools-venv 或区域检测脚本，"
                              "请先运行 setup.sh"}
    result_tmp = event_dir / ".region-result.part.json"
    cmd = [str(tools), "-B", str(script), "--video", str(video),
           "--preview-out", str(preview), "--result-out", str(result_tmp)]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        _stdout, stderr = proc.communicate(timeout=REGION_DETECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.communicate(timeout=5)  # reap; pipes already drained by kill
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        result_tmp.unlink(missing_ok=True)
        try:  # the detector's own atomic-write tmp: SIGKILL can land between
            # its write and replace (detect_slide_region.write_json_atomic)
            Path(str(result_tmp) + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return 504, {"error": f"区域检测超时（超过 {REGION_DETECT_TIMEOUT_S} 秒），"
                              "请重试，或在弹窗中手动框选区域"}
    result = _read_json(result_tmp)
    result_tmp.unlink(missing_ok=True)
    if proc.returncode != 0 or result is None or "rect" not in result:
        # rc=1 hard failure writes NO result JSON (T2 contract) — friendly 500
        return 500, {"error": "幻灯片区域自动检测失败：视频可能无法解码，"
                              "请重试，或在弹窗中手动框选区域",
                     "detail": (stderr or "").strip()[-300:]}
    cache = dict(result)
    cache.update({"video": video.name, "video_mtime": stat.st_mtime,
                  "video_size": stat.st_size, "detected_ts": time.time()})
    _atomic_write_json(cache_path, cache)
    return 200, _detect_payload(result, preview_url, warning, cached=False)


def region_confirm(event_name: str, rect_raw) -> tuple[int, dict]:
    """POST /api/region/confirm — atomically write input/<event>/region.json (§4.1).

    rect given → source=user (fields clamped to [0,1], box shrunk to stay
    inside the frame, w/h must stay > 0);
    rect omitted → adopt the cached auto detection, but only when it was
    reliable (mirrors detect_slide_region --write-region semantics) →
    source=auto. The .region-preview.png/.region-detect.json dotfiles are
    kept (spec §3.5: they vanish with the event dir after success cleanup)."""
    event = sanitize_event_name(event_name)
    if event is None:
        return 400, {"error": "事件名无效（仅中文/字母/数字/连字符）"}
    event_dir = event_input_dir(event)
    if event_dir is None or not event_dir.is_dir():
        return 404, {"error": "事件不存在"}
    videos = event_videos(event_dir)
    if not videos:
        return 400, {"error": "事件中没有视频文件，无法确认幻灯片区域"}
    if rect_raw is None:
        cached = _read_json(event_dir / ".region-detect.json")
        rect = (_clamp_rect(cached.get("rect"))
                if isinstance(cached, dict) and cached.get("reliable") else None)
        if rect is None:
            return 400, {"error": "尚无可靠的自动检测结果：请先运行「自动检测」，"
                                  "或手动框选区域后提交"}
        source, confidence = "auto", float(cached.get("confidence") or 0.0)
    else:
        rect = _clamp_rect(rect_raw)
        if rect is None:
            return 400, {"error": "区域坐标无效：x/y/w/h 需为数字（越界值自动收拢到 "
                                  "0-1），且宽高必须大于 0"}
        source, confidence = "user", 1.0
    region = {"schema_version": 1, "video": videos[0].name, "source": source,
              "rect": rect, "confidence": confidence, "created_ts": time.time()}
    _atomic_write_json(event_dir / "region.json", region)
    return 200, {"ok": True, "event": event, "region": region}


# --------------------------------------------------------------------------
# Multipart parsing (streaming; stdlib-only, no cgi — spec §0)
# --------------------------------------------------------------------------

class MultipartPart:
    """One form part. Payload is consumed lazily and exactly once; parts()
    drains anything left over before yielding the next part."""

    def __init__(self, headers: dict[str, str], chunks):
        self.headers = headers
        self._chunks = chunks
        disp = headers.get("content-disposition", "")
        self.name = (re.search(r'name="([^"]*)"', disp) or [None, None])[1]
        self.filename = (re.search(r'filename="([^"]*)"', disp) or [None, None])[1]

    def save_to(self, path: Path) -> int:
        written = 0
        with open(path, "wb") as sink:
            for chunk in self._chunks:
                sink.write(chunk)
                written += len(chunk)
        return written

    def read_text(self, limit=65536) -> str:
        return b"".join(self._chunks)[:limit].decode("utf-8", "replace")

    def discard(self) -> None:
        for _ in self._chunks:
            pass


class MultipartParser:
    """Streaming multipart/form-data reader over a raw file-like stream.
    Handles binary payloads (no-newline chunks), partial delimiters at buffer
    edges, and a final boundary."""

    CHUNK = 65536

    def __init__(self, stream, boundary: str, length: int | None = None):
        self.stream = stream
        self.boundary = boundary.encode()
        self.delim = b"\r\n--" + self.boundary
        self.remaining = length  # bytes left in body, if advertised
        self.buf = b"\r\n"  # lets the first boundary match the delimiter rule

    def _fill(self) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        size = self.CHUNK
        if self.remaining is not None:
            size = min(size, self.remaining)
        chunk = self.stream.read(size)
        if not chunk:
            return False
        if self.remaining is not None:
            self.remaining -= len(chunk)
        self.buf += chunk
        return True

    def _line(self) -> bytes | None:
        while b"\r\n" not in self.buf:
            if not self._fill():
                return None
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _payload_chunks(self):
        while True:
            idx = self.buf.find(self.delim)
            if idx >= 0:
                payload, self.buf = self.buf[:idx], self.buf[idx + len(self.delim):]
                if payload:
                    yield payload
                return
            keep = len(self.delim) - 1
            if len(self.buf) > keep:
                emit, self.buf = self.buf[:-keep], self.buf[-keep:]
                yield emit
                continue
            if not self._fill():
                if self.buf:
                    yield self.buf
                    self.buf = b""
                return

    def parts(self):
        """Yield Part objects. Payload generators are chained so that headers
        are parsed only after the previous payload is fully consumed."""
        boundary_line = b"--" + self.boundary
        # preamble: skip anything before the first boundary line (includes the
        # synthetic leading \r\n this parser seeds its buffer with)
        while True:
            first = self._line()
            if first is None:
                return
            if first == boundary_line:
                break
            if first == boundary_line + b"--":
                return
        while True:
            headers: dict[str, str] = {}
            while True:
                line = self._line()
                if line is None:
                    return
                if line == b"":
                    break
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
            consumed = False

            def chunks():
                nonlocal consumed
                consumed = True
                yield from self._payload_chunks()

            yield MultipartPart(headers, chunks())
            if not consumed:  # caller ignored the payload: drain it
                for _ in self._payload_chunks():
                    pass
            tail = self._line()
            if tail is None:
                return
            if tail == b"--":
                return


# --------------------------------------------------------------------------
# Disk-state aggregation (/api/status) — spec §4
# --------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def running_pid() -> int | None:
    data = _read_json(PID_FILE)
    if data and pid_alive(int(data.get("pid", 0))):
        return int(data["pid"])
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)
    return None


def _manifest_audio_total(event: str) -> int | None:
    m = _read_json(RUNS_DIR / event / "manifest.json")
    if not m:
        return None
    counts = m.get("counts", {})
    return counts.get("audio")


def substage_counters(event: str, stage: str) -> dict | None:
    """Done/total derived from on-disk artifacts (spec §4 table)."""
    run = RUNS_DIR / event

    def count(pattern: str) -> int:
        return len(list(run.glob(pattern)))

    if stage == "audio_prepare":
        total = _manifest_audio_total(event)
        done = count("prepared/artifacts/audio/enhanced/*.wav")
        return {"done": done, "total": total} if total else {"done": done}
    if stage == "asr.segment":
        return {"done": count("asr_windows/flat/*.wav")}
    if stage == "asr.whisper":
        total = _manifest_audio_total(event)
        done = count("whisper_*.json")
        return {"done": done, "total": total} if total else {"done": done}
    if stage == "asr.qwen":
        total = count("asr_windows/flat/*.wav")
        payload = _read_json(run / "asr_primary" / "qwen3_asr_candidates.json")
        done = len(payload.get("items", [])) if payload else 0
        return {"done": done, "total": total} if total else {"done": done}
    if stage == "reconcile":
        return {"done": count("reconciled/checkpoints/*")}
    if stage.split(".")[0] == "video_ingest":
        # 「已识别 n 页」(spec §3.4): the slide track lands in runs/<event>/
        # slides/P*.png (§4.2); before the dir exists there is nothing to count
        if (run / "slides").is_dir():
            return {"done": count("slides/P*.png")}
        return None
    return None


def tail_jsonl(n: int = 200) -> list[dict]:
    if not PROGRESS_JSONL.exists():
        return []
    try:
        lines = PROGRESS_JSONL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-n:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def current_event_state() -> dict | None:
    """Current event = last event_start in the timeline; snapshot from its
    .progress.json, reconciled against pid liveness."""
    for row in reversed(tail_jsonl()):
        if row.get("kind") == "event_start":
            event = row.get("event")
            snap = _read_json(RUNS_DIR / event / ".progress.json") or {}
            snap["event"] = event
            if snap.get("status") == "running" and running_pid() is None:
                snap["status"] = "stopped"
                snap["message"] = "流水线已停止"
            return snap
    return None


def aggregate_status() -> dict:
    pid = running_pid()
    current = current_event_state()
    active_event = current.get("event") if current and current.get("status") == "running" else None
    queue = scan_queue()
    for q in queue:
        q["active"] = q["name"] == active_event
    rows = tail_jsonl()
    events_done = sum(1 for r in rows if r.get("kind") == "event_done")
    events_failed = sum(1 for r in rows if r.get("kind") == "event_failed")

    # done counters derive ONLY from completed events' manifests; the running
    # event stays counted under files_pending (its queue entry is kept, marked
    # active) so 总数 = done + pending always holds
    files_done = 0
    audio_seconds = 0.0
    done_events = {r.get("event") for r in rows if r.get("kind") == "event_done"}
    for event in done_events:
        m = _read_json(RUNS_DIR / str(event) / "manifest.json")
        if m:
            files_done += m.get("file_count", 0)
            audio_seconds += m.get("audio_duration_seconds", 0) or 0

    counters = None
    if active_event:
        counters = substage_counters(active_event, current.get("stage", ""))

    outputs = []
    if OUTPUTS_DIR.exists():
        for entry in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.name):
            if entry.is_dir() and not entry.name.startswith("."):
                files = [p for p in entry.rglob("*") if p.is_file()]
                outputs.append({"name": entry.name,
                                "mtime": entry.stat().st_mtime,
                                "files": len(files)})
    return {
        "running": pid is not None,
        "pid": pid,
        "current": current,
        "substage": counters,
        "queue": queue,
        "totals": {"events_done": events_done, "events_failed": events_failed,
                   "files_done": files_done,
                   "files_pending": sum(q["files"] for q in queue),
                   "audio_hours": round(audio_seconds / 3600, 2)},
        "recent": rows[-30:],
        "outputs": outputs,
        "dependencies": dependency_status(),
        "candidate_plan": candidate_plan(),
        "fragment_plan": fragment_reading_plan(),
    }


# --------------------------------------------------------------------------
# Pipeline supervision (single pipeline, queue = input/)
# --------------------------------------------------------------------------

def _pipeline_cmd() -> list[str]:
    override = os.environ.get("MST_PIPELINE_CMD")  # test hook
    if override:
        return json.loads(override)
    return [sys.executable, "-B", str(ROOT / "run_meeting.py")]


def _spawn_pipeline() -> int:
    WEBAPP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_fd = open(PIPELINE_LOG, "ab")
    try:
        proc = subprocess.Popen(_pipeline_cmd(), cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=log_fd, stderr=subprocess.STDOUT,
                                start_new_session=True)
    finally:
        log_fd.close()
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "started": time.time()}),
                        encoding="utf-8")
    _supervision["proc"] = proc
    watcher = threading.Thread(target=_watch_pipeline, args=(proc,), daemon=True)
    watcher.start()
    _supervision["thread"] = watcher
    return proc.pid


def _watch_pipeline(proc: subprocess.Popen) -> None:
    rc = proc.wait()
    PID_FILE.unlink(missing_ok=True)
    _supervision["proc"] = None
    if _supervision["stop_requested"]:
        _supervision["stop_requested"] = False
        return
    # drain semantics (spec §3.2): clean exit + new queued events -> next pass;
    # failed exit never auto-restarts (no retry loops on failing events)
    if rc == 0 and scan_queue() and _supervision["auto_restarts"] < MAX_AUTO_RESTARTS:
        _supervision["auto_restarts"] += 1
        time.sleep(2)
        if not _supervision["stop_requested"]:
            _spawn_pipeline()
    else:
        _supervision["auto_restarts"] = 0


def start_pipeline() -> tuple[int, str]:
    if running_pid() is not None or _supervision["proc"] is not None:
        return 409, "pipeline already running"
    queue = scan_queue()
    if not queue:
        return 400, "input/ 中没有可处理事件（先上传录音、录屏或笔记）"
    awaiting = [q["name"] for q in queue if q.get("awaiting_region")]
    if awaiting:
        # v1 whole-batch gate (screen-recording spec §5): one unconfirmed
        # recording blocks the batch — no partial-start semantics
        return 409, (REGION_GATE_MSG + "、".join(awaiting)
                     + "。请先在队列卡片点击「确认幻灯片区域」，再启动编译")
    _supervision["stop_requested"] = False
    pid = _spawn_pipeline()
    return 202, f"pipeline started (pid {pid})"


def stop_pipeline() -> tuple[int, str]:
    pid = running_pid()
    if pid is None and _supervision["proc"] is None:
        return 409, "no pipeline running"
    _supervision["stop_requested"] = True
    proc = _supervision["proc"]
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            proc.wait(timeout=5)
    PID_FILE.unlink(missing_ok=True)
    # Stamp ONLY a genuinely in-flight event, decided on the RAW disk snapshot:
    # the pidfile is already gone, so current_event_state()'s reconciliation
    # would report "stopped" for any running snapshot and skip the stamp; and
    # during the spawn gap before the new event_start it still names the
    # PREVIOUS event (done/failed) — overwriting THAT would corrupt history.
    current = current_event_state()
    if current and current.get("event"):
        raw = _read_json(RUNS_DIR / current["event"] / ".progress.json") or {}
        if raw.get("status") == "running":
            snap_path = RUNS_DIR / current["event"] / ".progress.json"
            raw.update({"status": "stopped", "message": "用户停止", "updated": time.time()})
            snap_path.parent.mkdir(parents=True, exist_ok=True)
            snap_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            with PROGRESS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "event": current["event"],
                                    "kind": "event_stopped", "stage": raw.get("stage", ""),
                                    "message": "用户停止", "status": "stopped",
                                    "counters": {}}, ensure_ascii=False) + "\n")
    return 202, "pipeline stopped"


# --------------------------------------------------------------------------
# Console shutdown (page button / mst --stop)
# --------------------------------------------------------------------------

_shutting_down = False
_server_ref: list = []   # create_server() 填入；页面关机经它干净退出


def _delayed_server_shutdown() -> None:
    """Give the HTTP response ~0.4s to flush, then stop the serve loop.

    server.shutdown() from this worker thread is signal-free — a background
    console (nohup/&) inherits SIG_IGN for SIGINT, so os.kill(SIGINT) would
    be silently ignored there."""
    time.sleep(0.4)
    srv = _server_ref[0] if _server_ref else None
    if srv is not None:
        srv.shutdown()
    else:                        # 无引用时退回信号路径（前台运行）
        os.kill(os.getpid(), signal.SIGINT)


def request_shutdown(force: bool = False) -> tuple[int, dict]:
    """Shut the console down from the page (or a signal). If a compile is
    in flight: refuse unless force (which first stops it with the normal
    stop semantics — stopped-stamp, inputs kept)."""
    global _shutting_down
    if _shutting_down:
        return 200, {"status": "shutting_down"}
    compile_running = _supervision["proc"] is not None or running_pid() is not None
    if compile_running and not force:
        return 409, {"status": "running",
                     "message": "编译进行中：再次确认将停止编译并关闭服务"}
    if compile_running:
        stop_pipeline()
    _shutting_down = True
    threading.Thread(target=_delayed_server_shutdown, daemon=True).start()
    return 200, {"status": "shutting_down"}


# --------------------------------------------------------------------------
# Upload handling
# --------------------------------------------------------------------------

class UploadBlocked(Exception):
    """Raised when the upload targets the event currently being processed
    (spec §3.2 race: appended files would be deleted by success-cleanup)."""

    def __init__(self, event: str):
        super().__init__(event)
        self.event = event


def handle_upload(parser: MultipartParser, event_dir_of) -> tuple[int, dict]:
    """Parse the multipart body and materialize the event dir. event_dir_of is
    a callable(event_name) -> Path; it raises UploadBlocked to veto saves."""
    event_name: str | None = None
    fragment_mode = False
    output_dir = None
    saved_audio: list[str] = []
    saved_notes: list[str] = []
    saved_video: list[str] = []
    saved_of = {"audio": saved_audio, "notes": saved_notes, "video": saved_video}
    for part in parser.parts():
        if part.filename is None:
            text = part.read_text().strip()
            if part.name == "event":
                event_name = text or None
            elif part.name == "fragment_mode":
                fragment_mode = text == "1"
            elif part.name == "output_dir":
                output_dir = text or None
            continue
        fname = sanitize_filename(part.filename)
        if fname is None:
            part.discard()
            continue
        ext = Path(fname).suffix.lower()
        kind = None
        if ext in AUDIO_EXTS:
            kind = "audio"
        elif ext in VIDEO_EXTS:  # screen-recording uploads (spec §3.5)
            kind = "video"
        elif ext in NOTE_EXTS or fname.lower() in NOTE_CANONICAL:
            kind = "notes"
        if kind is None:
            part.discard()
            continue
        if fragment_mode and kind == "notes":
            ref_dir = RUNS_DIR / ".fragment-references"
            ref_dir.mkdir(parents=True, exist_ok=True)
            dest = ref_dir / fname
            part_path = dest.with_name(dest.name + ".part")
            part.save_to(part_path)
            os.replace(part_path, dest)
            saved_notes.append(fname)
            continue
        if fragment_mode:
            # Each file becomes a separately auditable event. The original
            # filename remains the source evidence; no upload is merged here.
            event_name = f"fragment-{int(time.time() * 1000)}-{len(saved_audio) + len(saved_video) + len(saved_notes) + 1}"
        elif event_name is None:  # fields-before-files is our form's contract;
            event_name = default_event_name()  # foreign clients get a default
        target_dir = event_dir_of(event_name)  # raises UploadBlocked → veto
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / fname
        part_path = dest.with_name(dest.name + ".part")
        part.save_to(part_path)
        os.replace(part_path, dest)
        saved_of[kind].append(fname)
    if fragment_mode and saved_notes and not (saved_audio or saved_video):
        return 201, {"event": None, "audio": [], "video": [], "notes": saved_notes,
                     "message": "笔记已作为本地参考材料保存，不会生成片段事件"}
    event = sanitize_event_name(event_name)
    if event is None:
        return 400, {"error": "事件名无效（仅中文/字母/数字/连字符）"}
    event_dir = event_dir_of(event)
    if not saved_audio and not saved_video and not saved_notes \
            and not any(event_dir.iterdir()):
        return 400, {"error": "没有可保存的音频、视频或笔记文件"}
    if saved_notes:
        rebuild_notes_md(event_dir)
    if output_dir:
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / ".mst-output.json").write_text(
            json.dumps({"output_dir": output_dir}, ensure_ascii=False), encoding="utf-8")
    return 201, {"event": event, "audio": saved_audio, "video": saved_video,
                 "notes": saved_notes}


def upload_is_blocked(event: str | None) -> bool:
    """Spec §3.2: uploading into the event currently being processed would
    race the success-cleanup (appended files would be deleted)."""
    if not event or running_pid() is None:
        return False
    current = current_event_state()
    return bool(current and current.get("event") == event
                and current.get("status") == "running")


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".md": "text/markdown; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".db": "application/octet-stream"}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "MSTConsole/1.0"

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    # -- responses --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str,
              disposition: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._static("index.html")
        if url.path.startswith("/static/"):
            return self._static(url.path[len("/static/"):])
        if url.path == "/api/status":
            return self._json(200, aggregate_status())
        if url.path == "/api/dependencies":
            return self._json(200, dependency_status())
        if url.path == "/api/candidates":
            return self._json(200, candidate_plan())
        if url.path == "/api/fragments":
            return self._json(200, fragment_reading_plan())
        if url.path == "/api/outputs":
            return self._json(200, {"outputs": aggregate_status()["outputs"]})
        if url.path.startswith("/api/outputs/"):
            return self._outputs_route(url)
        if url.path.startswith("/api/events/"):
            return self._events_route(url)
        if url.path == "/api/logs":
            return self._logs_route(parse_qs(url.query))
        self._json(404, {"error": "not found"})

    def _static(self, name: str) -> None:
        path = safe_join(STATIC_DIR, name)
        if path is None or not path.is_file():
            return self._json(404, {"error": "not found"})
        body = path.read_bytes()
        self._send(200, body, CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def _outputs_route(self, url) -> None:
        parts = [p for p in url.path.split("/") if p][2:]  # after api/outputs
        if not parts:
            return self._json(404, {"error": "event required"})
        event = unquote(parts[0])
        base = safe_join(OUTPUTS_DIR, event)
        if base is None or not base.is_dir():
            return self._json(404, {"error": "event not found"})
        query = parse_qs(url.query)
        if len(parts) == 1 or parts[1] == "files":
            files = [{"name": str(p.relative_to(base)), "size": p.stat().st_size}
                     for p in base.rglob("*") if p.is_file()]
            files.sort(key=lambda f: f["name"])
            return self._json(200, {"event": event, "files": files})
        if parts[1] == "raw":
            rel = (query.get("path") or [""])[0]
            path = safe_join(base, rel) if rel else None
            if path is None or not path.is_file():
                return self._json(404, {"error": "file not found"})
            body = path.read_bytes()
            return self._send(200, body, "text/plain; charset=utf-8")
        if parts[1] == "zip":
            return self._zip(base, event)
        self._json(404, {"error": "not found"})

    def _events_route(self, url) -> None:
        """/api/events/<event>/region_preview.png — statically serve the
        detection preview dotfile inside the safe_join guard (spec §3.5);
        404 when the event or the preview does not exist."""
        parts = [p for p in url.path.split("/") if p][2:]  # after api/events
        if len(parts) != 2 or parts[1] != "region_preview.png":
            return self._json(404, {"error": "not found"})
        event_dir = event_input_dir(unquote(parts[0]))
        if event_dir is None or not event_dir.is_dir():
            return self._json(404, {"error": "event not found"})
        path = safe_join(event_dir, ".region-preview.png")
        if path is None or not path.is_file():
            return self._json(404, {"error": "preview not found"})
        self._send(200, path.read_bytes(), CONTENT_TYPES[".png"])

    def _zip(self, base: Path, event: str) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(base))
        body = buffer.getvalue()
        # http.server encodes headers latin-1: a non-ASCII event name in a
        # plain filename= would raise UnicodeEncodeError and kill the
        # connection mid-response. RFC 5987: ASCII fallback + filename*.
        from urllib.parse import quote
        ascii_name = event.encode("latin-1", "ignore").decode("latin-1") or "event"
        disposition = (f'attachment; filename="{ascii_name}.zip"; '
                       f"filename*=UTF-8''{quote(event)}.zip")
        self._send(200, body, "application/zip", disposition=disposition)

    def _logs_route(self, query: dict) -> None:
        which = (query.get("what") or ["progress"])[0]
        tail = int((query.get("tail") or ["40"])[0])
        if which == "pipeline":
            path = PIPELINE_LOG
            if not path.exists():
                return self._json(200, {"lines": []})
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
            return self._json(200, {"lines": lines})
        return self._json(200, {"lines": tail_jsonl(tail)})

    # -- POST ----------------------------------------------------------------
    def _json_body(self, length: int) -> dict | None:
        """Small JSON request body (region endpoints); None when malformed."""
        if length <= 0 or length > 1_048_576:
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/upload":
            return self._upload(length)
        if url.path == "/api/dependencies/install":
            body = self._json_body(length)
            if body is None or not isinstance(body.get("ids"), list):
                return self._json(400, {"error": "请求体需要 ids 数组"})
            code, payload = install_dependencies(body["ids"])
            return self._json(code, payload)
        if url.path == "/api/candidates/confirm":
            body = self._json_body(length)
            if body is None or not isinstance(body.get("order"), list):
                return self._json(400, {"error": "请求体需要 order 数组"})
            code, payload = confirm_candidate_plan(body["order"])
            return self._json(code, payload)
        if url.path == "/api/fragments/confirm":
            code, payload = confirm_fragment_reading_order()
            return self._json(code, payload)
        if url.path == "/api/start":
            # MST_PIPELINE_CMD is an internal test hook.  Real console starts
            # are always held here until the recovery checks are acknowledged.
            if not os.environ.get("MST_PIPELINE_CMD"):
                deps = dependency_status()
                missing = [d["id"] for d in deps["items"] if d["required"] and not d["ready"]]
                if missing:
                    return self._json(409, {"ok": False, "message": "依赖未就绪：" + "、".join(missing) + "。请在网页依赖自检中安装后复检"})
                plan = candidate_plan()
                if plan["ready_to_confirm"] and len(plan["candidates"]) > 1 and not plan["confirmed"]:
                    return self._json(409, {"ok": False, "message": "多会议候选顺序尚未确认；请先确认本地候选分组与顺序"})
            code, msg = start_pipeline()
            payload = {"ok": code == 202, "message": msg}
            if code == 409 and msg.startswith(REGION_GATE_MSG):
                # machine-readable gate list for the frontend toast (spec §3.6)
                payload["awaiting_region"] = awaiting_region_events()
            return self._json(code, payload)
        if url.path == "/api/region/detect":
            body = self._json_body(length)
            if body is None:
                return self._json(400, {"error": "请求体必须是 JSON 对象"})
            try:
                code, payload = region_detect(str(body.get("event") or ""))
            except (OSError, ValueError) as exc:  # fs races etc. (house style)
                return self._json(500, {"error": f"region detect failed: {exc}"})
            return self._json(code, payload)
        if url.path == "/api/region/confirm":
            body = self._json_body(length)
            if body is None:
                return self._json(400, {"error": "请求体必须是 JSON 对象"})
            try:
                code, payload = region_confirm(str(body.get("event") or ""),
                                               body.get("rect"))
            except (OSError, ValueError) as exc:
                return self._json(500, {"error": f"region confirm failed: {exc}"})
            return self._json(code, payload)
        if url.path == "/api/stop":
            code, msg = stop_pipeline()
            return self._json(code, {"ok": code == 202, "message": msg})
        if url.path == "/api/pick-folder":
            return self._pick_folder()
        if url.path == "/api/shutdown":
            # Custom header = CSRF guard: HTML forms cannot set it, and a
            # cross-origin fetch with custom headers fails our no-CORS policy.
            if self.headers.get("X-MST-Shutdown") != "yes":
                return self._json(400, {"error": "missing X-MST-Shutdown header"})
            force = url.query == "force=1"
            code, payload = request_shutdown(force)
            return self._json(code, payload)
        self._json(404, {"error": "not found"})

    def _pick_folder(self) -> None:
        """Native OS folder chooser for the output-dir field.

        Browsers never expose absolute paths, but this console is a LOCAL
        tool: the server can raise the native dialog (osascript on macOS,
        zero new deps) and hand back the POSIX path. Blocking is fine — the
        HTTP server is threaded; the frontend uses a long fetch timeout.
        """
        if platform.system() != "Darwin":
            return self._json(501, {"error": "此系统不支持文件夹选择，请手动输入路径"})
        script = ('POSIX path of (choose folder with prompt '
                  '"选择输出目录（报告包将写入其下）")')
        try:
            proc = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return self._json(501, {"error": "选择超时，请手动输入路径"})
        if proc.returncode != 0:  # user pressed cancel
            return self._json(200, {"cancelled": True})
        path = proc.stdout.strip().rstrip("/")
        if not path.startswith("/"):
            return self._json(501, {"error": f"意外的返回值: {proc.stdout!r}"})
        return self._json(200, {"path": path})

    def _upload(self, length: int) -> None:
        ctype = self.headers.get("Content-Type", "")
        match = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not match:
            return self._json(400, {"error": "multipart/form-data required"})
        parser = MultipartParser(self.rfile, match.group(1), length)

        def event_dir_of(name: str) -> Path:
            if upload_is_blocked(name):
                raise UploadBlocked(name)
            return INPUT_DIR / name

        try:
            code, payload = handle_upload(parser, event_dir_of)
        except UploadBlocked as exc:
            return self._json(409, {"error": "该事件正在处理中，追加文件会被成功清理误删；请换一个事件名",
                                    "event": exc.event})
        except (OSError, ValueError) as exc:
            return self._json(500, {"error": f"upload failed: {exc}"})
        self._json(code, payload)


def create_server(port: int | None = None, open_browser: bool = False) -> ThreadingHTTPServer:
    port = port or int(os.environ.get("MST_WEB_PORT") or DEFAULT_PORT)
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.daemon_threads = True
    _server_ref[:] = [server]   # 页面关机经此引用跨线程干净停机
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{server.server_address[1]}/")
    return server


def serve(port: int | None = None, open_browser: bool = False) -> None:
    server = create_server(port, open_browser)
    print(f"[webapp] http://127.0.0.1:{server.server_address[1]}/ （Ctrl+C 停止）", flush=True)

    def _term_to_interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term_to_interrupt)  # mst --stop 走干净退出路径
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[webapp] 已退出", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Meeting Summary Tool web console")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    serve(args.port, not args.no_browser)
