#!/usr/bin/env python3
"""一键会议处理：音频 + 录屏视频 + 笔记 → 完整报告包（主题索引、逐句记录、报告、数据库、笔记佐证）

用法：
    python3 run_meeting.py [--name 会议名称]

输入：input/ 文件夹中的 .m4a/.mp3/.wav 录音、.mp4/.mov/.mkv/.webm/.m4v 录屏、notes.md 笔记
输出：outputs/<名称>/ 完整报告包

录屏事件（docs/screen-recording-parsing-design.md）：video_ingest 站把视频分解为
音轨（注入 manifest 后当普通音轨处理，零特判）+ 幻灯片轨（runs/<event>/slides/），
slide_align 站产出真实 relations。v1 决定（§3.4）：reconcile 视图保持 audio-only —
image 证据行（I######）经 runs/<event>/evidence/evidence_audio_only.jsonl 侧车文件
与 relevance_filter / quality_gate 隔离（重跑不再产生 image 行时会清扫侧车防残留），
PPT 内容经 relations/《03_PPT补充信息》层参与；区域未经人工确认或区域降级仅音轨时，
报告标注写入 outputs/<event>/00_使用说明.md。
纯音频/笔记事件不产生任何新工件，输出与 12 站时代逐字节一致。

资源控制：大模型独占顺序执行，light 阶段最多 2 并行，系统预留 2GB。
"""

from __future__ import annotations

import argparse
import importlib.util as _importlib_util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parent
INPUT_DIR = WORKSPACE / "input"
CORE = WORKSPACE / "core"
NOTE_LAYER = WORKSPACE / "note-layer"

# External runtime paths: resolved from config.json / MST_* env (config.py).
# Nothing personal is hardcoded here — see config.example.json / INSTALL.md.
import config as _config

_CFG = _config.resolve()
QWEN_PYTHON = Path(_CFG["qwen_python"]) if _CFG.get("qwen_python") else None
QWEN_SCRIPT = CORE / "scripts" / "run_qwen3_asr.py"
WHISPER_BIN = Path(_CFG["whisper_bin"])
WHISPER_MODEL = Path(_CFG["whisper_model"])
OLLAMA_URL = _CFG["ollama_url"]
OLLAMA_MODEL = _CFG["ollama_model"]
# Interpreter of vendor/tools-venv (numpy/cv2/pypinyin) for the three
# screen-recording tools ONLY (design §3.4). config.resolve() always supplies
# a truthy default (config.py:27), so no local fallback branch is needed.
TOOLS_PYTHON = Path(_CFG["tools_python"])


def _load_core_module(name: str):
    """Import a core/scripts module by path (stdlib-safe modules only).

    meeting_pipeline owns the VIDEO_EXTENSIONS whitelist (design §3.4: import,
    never duplicate); align_audio_slides owns the CJK-compact bigram similarity
    reused by stage_slide_align (design §4.5 "复用 align_audio_slides 词面候选").
    """
    spec = _importlib_util.spec_from_file_location(f"mst_{name}", CORE / "scripts" / f"{name}.py")
    module = _importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_core_module("meeting_pipeline")
_ALIGN = _load_core_module("align_audio_slides")
VIDEO_EXTENSIONS: set[str] = set(_PIPELINE.VIDEO_EXTENSIONS)

# align_audio_slides 现行参数（design §4.5: top-k、minimum-score 现参数）
_LEXICAL_TOP_K = 5
_LEXICAL_MIN_SCORE = 0.03

# extract_video_slides.py 的 fail-fast 文案（T3 AUDIO_MISSING_MESSAGE，spec §5 行1）。
# 镜像字符串而非 import：该脚本在模块级 require numpy/cv2，系统 python3 不可导入。
_AUDIO_MISSING_MESSAGE = "录屏无声且事件内无独立音频文件:需要含声录屏或另配音频"

# Safety margins
RESERVE_MEMORY_GB = 2.0
MODEL_MEMORY_ESTIMATE_GB = 4.5


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list[str], timeout: int = 3600, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, streaming output, return result."""
    merged_env = os.environ.copy()
    merged_env.setdefault("NUMBA_CACHE_DIR", str(WORKSPACE / "runs" / ".numba_cache"))
    if env:
        merged_env.update(env)
    result = subprocess.run(cmd, cwd=cwd or WORKSPACE, env=merged_env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)
    if result.returncode != 0:
        tail = result.stdout[-1000:] if result.stdout else "(no output)"
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd[:4])}...\n{tail}")
    return result


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- Web-console observability (docs/web-console-design.md §4) ---------------
# Canonical stage table for the console step bar; keys are stable contract —
# webapp/static/app.js (STAGE_KEYS) maps them to UI labels; webapp/server.py
# only relays the keys, tests pin the schema.
# 14 stations (screen-recording design §3.4): video_ingest 紧随 inventory、
# slide_align 紧随 relevance；纯音频事件两站零开销跳过。
STAGE_ORDER: list[tuple[str, str]] = [
    ("inventory", "文件清单"),
    ("video_ingest", "视频分解（音轨/幻灯片）"),
    ("audio_prepare", "音频预处理（降噪）"),
    ("lang_probe", "语言探测"),
    ("asr", "语音识别"),
    ("literal", "组装逐句记录"),
    ("evidence", "生成证据"),
    ("relevance", "无关话语过滤"),
    ("slide_align", "幻灯片对齐"),
    ("reconcile", "主题提取与索引"),
    ("audit", "claim 保真审计"),
    ("notes", "笔记佐证"),
    ("package", "构建报告包"),
    ("validate", "验证与质量门禁"),
]

_progress_state: dict[str, dict] = {}  # event -> {"stage": str, "stage_started": float}


def _stage_index(stage: str) -> int:
    base = stage.split(".")[0]  # "asr.qwen" belongs to the "asr" step
    for i, (key, _label) in enumerate(STAGE_ORDER, 1):
        if key == base:
            return i
    return 0


def emit_progress(event: str, kind: str, stage: str = "", message: str = "",
                  status: str = "running", counters: dict | None = None) -> None:
    """Append one line to runs/progress.jsonl and atomically refresh
    runs/<event>/.progress.json. Best-effort by design: observability must
    never break the pipeline."""
    try:
        ts = time.time()
        runs_dir = WORKSPACE / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        state = _progress_state.setdefault(event, {"stage": "", "stage_started": ts})
        if stage and stage != state["stage"]:
            state["stage"] = stage
            state["stage_started"] = ts
        line = {"ts": round(ts, 3), "event": event, "kind": kind, "stage": stage,
                "message": message, "status": status, "counters": counters or {}}
        with (runs_dir / "progress.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        atomic_json(runs_dir / event / ".progress.json", {
            "event": event, "status": status, "stage": state["stage"],
            "stage_index": _stage_index(state["stage"]),
            "stage_total": len(STAGE_ORDER),
            "stage_started": round(state["stage_started"], 3),
            "message": message, "updated": round(ts, 3)})
    except Exception:
        pass


def resolve_output_dir(event: dict) -> Path:
    """Default outputs/<name>; overridable per-event via a web-console-written
    .mst-output.json next to the inputs (dot-file: invisible to
    find_input_events, removed with the event dir on success)."""
    try:
        override = json.loads((event["dir"] / ".mst-output.json")
                              .read_text(encoding="utf-8")).get("output_dir")
        if override:
            return Path(override).expanduser()
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return WORKSPACE / "outputs" / event["name"]


def check_memory() -> dict:
    """Quick memory snapshot."""
    try:
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        vm = subprocess.check_output(["vm_stat"], text=True)
        page_match = __import__("re").search(r"page size of (\d+) bytes", vm)
        ps = int(page_match.group(1)) if page_match else 16384
        pages = {n: int(v.replace(".", "")) for n, v in __import__("re").findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+\.)", vm)}
        avail = sum(pages.get(n, 0) for n in ("free", "inactive", "speculative", "purgeable")) * ps / 1024**3
        return {"total_gb": total / 1024**3, "available_gb": round(avail, 2)}
    except Exception:
        return {"total_gb": 16.0, "available_gb": 4.0}


def ensure_ollama() -> None:
    """Make sure Ollama is running."""
    import urllib.request
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
        log("Ollama 已运行")
    except Exception:
        log("启动 Ollama...")
        subprocess.Popen(["open", "-a", "Ollama"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(2)
            try:
                urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
                log("Ollama 已就绪")
                return
            except Exception:
                pass
        raise RuntimeError("Ollama 启动超时")


def unload_ollama() -> None:
    """Free Ollama model memory."""
    import urllib.request
    try:
        for model in [OLLAMA_MODEL, "qwen3-vl:8b", "qwen3-vl:4b-instruct", "bge-m3:latest"]:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/generate",
                data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def find_input_events() -> list[dict]:
    """Group input/ contents into events.

    Each first-level subdirectory is one event named after the directory.
    Loose audio/video/notes files directly under input/ form one shared event
    named "misc". This lets a user drop several lectures at once and have each
    processed as its own meeting package.

    Screen-recording design §3.4: an event forms when ANY of audio / notes /
    video is present; video files join the event dict as "video" (sorted, so
    multi-video events deterministically process the first — spec §5). The
    extension whitelist is imported from meeting_pipeline, never duplicated.
    """
    audio_exts = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".caf"}
    note_names = {"notes.md", "note.md"}
    video_exts = VIDEO_EXTENSIONS
    events: list[dict] = []
    loose_audio, loose_video, loose_notes = [], [], None
    INPUT_DIR.mkdir(parents=True, exist_ok=True)  # fresh clone: input/ not in git
    for entry in sorted(INPUT_DIR.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            audio = sorted(p for p in entry.iterdir() if p.suffix.lower() in audio_exts and not p.name.startswith("."))
            video = sorted(p for p in entry.iterdir() if p.suffix.lower() in video_exts and not p.name.startswith("."))
            notes = next((p for p in entry.iterdir() if p.name.lower() in note_names), None)
            if audio or video or notes:
                events.append({"name": entry.name, "dir": entry, "audio": audio,
                               "video": video, "notes": notes})
        elif entry.suffix.lower() in audio_exts:
            loose_audio.append(entry)
        elif entry.suffix.lower() in video_exts:
            loose_video.append(entry)
        elif entry.name.lower() in note_names:
            loose_notes = entry
    if loose_audio or loose_video or loose_notes:
        events.append({"name": "misc", "dir": INPUT_DIR, "audio": loose_audio,
                       "video": loose_video, "notes": loose_notes})
    # The local console may save a user-confirmed candidate order for several
    # independently processed meetings.  Invalid/stale plans are ignored, so
    # no input is merged or fabricated merely to satisfy an ordering hint.
    plan_path = INPUT_DIR / ".meeting-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        order = plan.get("confirmed") if isinstance(plan, dict) else None
        names = [e["name"] for e in events]
        if isinstance(order, list) and sorted(order) == sorted(names):
            rank = {name: i for i, name in enumerate(order)}
            events.sort(key=lambda e: rank[e["name"]])
    except (OSError, ValueError, TypeError):
        pass
    if not events:
        raise FileNotFoundError(
            f"input/ 中没有任何可处理内容（音频: {', '.join(sorted(audio_exts))}，"
            f"视频: {', '.join(sorted(video_exts))}，笔记: notes.md）")
    return events


def stage_inventory(run_dir: Path, source_dir: Path) -> Path:
    log("Stage 1/9: 文件清单...")
    manifest_path = run_dir / "manifest.json"
    run(["python3", "-B", str(CORE / "scripts" / "meeting_pipeline.py"), "inventory",
         "--source", str(source_dir), "--output", str(manifest_path)])
    return manifest_path


# --- 录屏摄取（docs/screen-recording-parsing-design.md §3.4） ----------------
# 薄编排原则：算法全部在 core/scripts（detect_slide_region / extract_video_slides /
# run_vision_ocr / suggest_ocr_corrections），本文件只做接线、决策表与降级语义。

def _probe_duration(path: Path) -> float | None:
    """ffprobe duration in seconds (None when unprobeable); mirrors the
    detect_language probe pattern."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        return float(proc.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def _extract_audio_only(video: Path, out: Path) -> None:
    """Degraded-branch audio extraction (ffmpeg -vn, copy 优先、aac 兜底).

    Only used when the slide track is skipped (region unreliable) — the normal
    path delegates audio to extract_video_slides.resolve_audio. Same decision-
    table row and same fail-fast message as T3 (spec §2/§5).
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    tail = ""
    for codec in (["-c:a", "copy"], ["-c:a", "aac"]):
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(video), "-vn",
             *codec, str(out)],
            capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return
        tail = proc.stderr.strip()[-300:]
        out.unlink(missing_ok=True)  # never leave a partial audio artifact behind
    raise RuntimeError(f"{_AUDIO_MISSING_MESSAGE}（ffmpeg 抽音轨失败: {tail}）")


def _valid_region_file(path: Path, video_name: str) -> bool:
    """region.json per design §4.1: object with an in-range relative rect bound
    to THIS video's filename. Anything else (missing, malformed, out-of-range,
    stale video binding) → CLI auto-detect fallback (spec §5 末行)."""
    try:
        region = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(region, dict):
        return False
    if Path(str(region.get("video") or "")).name != video_name:
        return False
    rect = region.get("rect")
    if not isinstance(rect, dict):
        return False
    try:
        x, y, w, h = (float(rect[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False
    return (0.0 <= x < 1.0 and 0.0 <= y < 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0
            and x + w <= 1.0 + 1e-6 and y + h <= 1.0 + 1e-6)


def _next_source_index(files: list[dict]) -> int:
    """First free F###### index after the inventory-issued source ids."""
    highest = 0
    for item in files:
        match = re.fullmatch(r"F(\d{6})", str(item.get("source_id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _derived_entry(source_id: str, kind: str, video_name: str, path: Path,
                   relative_path: str, extra: dict | None = None) -> dict:
    """One derived manifest entry (design §4.3).

    `path` is ABSOLUTE by contract: derived media lives in runs/, not under
    source_root, and prepare_audio resolves the override verbatim (T4).
    """
    import datetime as dt
    entry = {
        "source_id": source_id,
        "relative_path": relative_path,
        "kind": kind,
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "modified_at": dt.datetime.fromtimestamp(
            path.stat().st_mtime, dt.timezone.utc).astimezone().isoformat(),
        "sha256": _PIPELINE.sha256_file(path),
        "eligible_source": True,
        "derived_from": video_name,
        "path": str(path.resolve()),
    }
    entry.update(extra or {})
    return entry


def _inject_derived_entries(manifest_path: Path, run_dir: Path, video_name: str,
                            audio_file: Path | None, audio_duration: float | None,
                            slides: list[dict]) -> list[dict]:
    """Append derived manifest entries (design §4.3), idempotently.

    Retries reuse runs/<event>: previously injected derived entries are dropped
    first and every aggregate field is recomputed from surviving originals +
    fresh derived entries, so a second pass can never duplicate (spec §6
    「manifest 派生条目追加幂等」). Returns the newly appended entries.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = [item for item in manifest.get("files", []) if "derived_from" not in item]
    next_index = _next_source_index(files)
    derived: list[dict] = []
    if audio_file is not None and audio_file.is_file():
        derived.append(_derived_entry(
            f"F{next_index:06d}", "audio", video_name, audio_file, audio_file.name,
            extra={"media": {"duration_seconds": audio_duration}}))
        next_index += 1
    for slide in slides:
        image_path = run_dir / "slides" / str(slide.get("image") or "")
        if not image_path.is_file():
            continue  # retracted/missing page: slides.json 与磁盘不一致时以磁盘为准
        derived.append(_derived_entry(
            f"F{next_index:06d}", "image", video_name, image_path,
            f"slides/{image_path.name}",
            extra={"page_id": slide.get("page_id"),
                   "time_ranges": slide.get("time_ranges") or []}))
        next_index += 1
    files.extend(derived)
    counts: dict[str, int] = {}
    for item in files:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    manifest["files"] = files
    manifest["file_count"] = len(files)
    manifest["total_bytes"] = sum(item.get("size_bytes", 0) for item in files)
    manifest["counts"] = counts
    manifest["audio_duration_seconds"] = sum(
        float((item.get("media") or {}).get("duration_seconds") or 0)
        for item in files if item["kind"] == "audio")
    atomic_json(manifest_path, manifest)
    return derived


def stage_video_ingest(run_dir: Path, manifest_path: Path, event: dict) -> dict | None:
    """视频分解站（design §3.4）：区域 → 音轨 → 幻灯片 → OCR → manifest 派生条目.

    Pure audio/notes events skip with zero artifacts and zero subprocess calls
    (兼容边界 §7：纯音频路径行为与产物逐字节不变)。Returns the ingest receipt
    dict, or None when the event has no video.

    音轨决策表（§2，确定性）：
      事件有独立音频        → --has-external-audio，视频音轨忽略（用户决策③分轨）
      无独立音频 & 视频含声  → extracted_audio.m4a 注入 manifest 当普通音轨
      无独立音频 & 视频无声  → fail-fast（§5 行1，源文件不动，输入保留）
    """
    ev = run_dir.name
    videos = sorted(event.get("video") or [])
    if not videos:
        emit_progress(ev, "stage", "video_ingest", "无视频输入，跳过视频分解")
        # Retry hygiene: the event's video was removed between runs. Run-1's
        # artifacts (ingest_receipt slide_track=true, slides/, extracted audio)
        # would misdirect later stations — the fresh inventory carries no
        # derived entries, so _slide_image_evidence would raise a phantom
        # 「缺少 manifest 派生条目（video_ingest 接线错误）」 on EVERY retry.
        # Sweep them: a video-removed retry must behave exactly like a fresh
        # pure-audio run (stage_slide_align then also sweeps stale relations).
        stale_audio = run_dir / "extracted_audio.m4a"
        if stale_audio.exists():
            stale_audio.unlink()
        for stale_dir in (run_dir / "video", run_dir / "slides"):
            if stale_dir.is_dir():
                shutil.rmtree(stale_dir, ignore_errors=True)
        return None

    warnings: list[str] = []
    if len(videos) > 1:
        # spec §5: v1 只处理按文件名排序的第一个视频
        warnings.append("multiple_videos_first_only")
    video_input = videos[0]
    video_name = video_input.name
    if len(videos) > 1:
        emit_progress(ev, "stage", "video_ingest",
                      f"多视频事件仅处理首个 {video_name}（其余 {len(videos) - 1} 个忽略）")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    video_entry = next(
        (item for item in manifest.get("files", [])
         if item.get("kind") == "video"
         and Path(str(item.get("relative_path") or "")).name == video_name), None)
    has_audio = ((video_entry or {}).get("media") or {}).get("has_audio")
    external_audio = bool(event.get("audio"))

    # spec §5 行1 检测点（video_ingest 开头 ffprobe）：inventory 的 media 元数据
    # 就是该 ffprobe 结果。无声且无独立音频 → 在产生任何工件之前 fail-fast。
    if not external_audio and has_audio is False:
        raise RuntimeError(_AUDIO_MISSING_MESSAGE)

    source_copy = run_dir / "source" / video_name
    video_path = source_copy if source_copy.is_file() else video_input
    video_dir = run_dir / "video"

    # -- 区域解析（§3.4 条目1 / §4.1 / §5 末两行） ---------------------------
    emit_progress(ev, "stage", "video_ingest.probe", f"探测幻灯片区域（{video_name}）")
    region_path = Path(event["dir"]) / "region.json"
    region_confirmed = _valid_region_file(region_path, video_name)
    if not region_confirmed:
        if region_path.exists():
            warnings.append("region_config_invalid_redetected")
            log("  region.json 失效（视频绑定不匹配或格式非法）→ 重新自动检测")
        auto_region = video_dir / "region.json"
        proc = subprocess.run(
            [str(TOOLS_PYTHON), "-B", str(CORE / "scripts" / "detect_slide_region.py"),
             "--video", str(video_path),
             "--preview-out", str(video_dir / "region-preview.png"),
             "--result-out", str(video_dir / "detect_receipt.json"),
             "--write-region", str(auto_region)],
            capture_output=True, text=True, timeout=900, cwd=str(WORKSPACE))
        if proc.returncode == 2:
            # 未检出可信区域 → 幻灯片轨降级跳过；音频轨照常（spec §5 行2）
            return _ingest_audio_only_degraded(
                run_dir, manifest_path, video_path, video_name, video_entry,
                external_audio, warnings)
        if proc.returncode != 0:
            raise RuntimeError(
                f"detect_slide_region 失败 (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout)[-500:]}")
        region_path = auto_region
        warnings.append("region_auto_not_user_confirmed")
        log("  ⚠ 幻灯片区域为自动检测（未经人工确认）——已标注进 receipt 与幻灯片元数据")
    try:
        region_source = str(json.loads(region_path.read_text(encoding="utf-8")).get("source") or "auto")
    except (OSError, json.JSONDecodeError):
        region_source = "auto"

    # -- 视频分解（T3：音轨决策 + 流式抽帧 + 分段 + 去重，一次完成） ----------
    emit_progress(ev, "stage", "video_ingest.audio",
                  "音轨决策：事件含独立音频，视频音轨忽略（分轨处理）" if external_audio
                  else "音轨决策：从视频抽取音轨（extracted_audio.m4a）")
    emit_progress(ev, "stage", "video_ingest.frames", "流式抽帧 + 帧差分段（fps=1，内存有界）")
    extract_cmd = [str(TOOLS_PYTHON), "-B", str(CORE / "scripts" / "extract_video_slides.py"),
                   "--video", str(video_path),
                   "--region-json", str(region_path),
                   "--run-dir", str(run_dir)]
    if external_audio:
        # §2 决策表：独立音频优先，视频音轨忽略
        extract_cmd.append("--has-external-audio")
    # exit 2（无声且无独立音频）经 run() 的 RuntimeError 尾行浮出 §5 中文提示；
    # T3 保证该路径零工件（run-dir 内不产生 slides/extracted_audio/receipt）。
    run(extract_cmd, timeout=7200)

    slides_doc = json.loads((run_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
    extract_receipt = json.loads((run_dir / "video" / "extract_receipt.json").read_text(encoding="utf-8"))
    slides = slides_doc.get("slides") or []
    for token in extract_receipt.get("warnings") or []:
        if token not in warnings:
            warnings.append(token)
    slide_track = bool(slides)
    audio_route = extract_receipt.get("audio_route")
    audio_file = Path(extract_receipt["audio_extracted"]) if extract_receipt.get("audio_extracted") else None
    audio_duration = extract_receipt.get("video_duration_s") if audio_route == "extracted" else None
    emit_progress(ev, "stage", "video_ingest.segment",
                  f"分段完成：{len(slides)} 页" + ("" if slide_track else "（幻灯片轨降级，见 warnings）"))
    log(f"  视频分解完成: 音轨={audio_route}, 幻灯片={len(slides)} 页, 警告={warnings or '无'}")

    # -- Vision OCR（幻灯片轨成立时；降级不致命，spec §5「OCR 单页失败」的工具级推广）
    ocr_status = "skipped"
    if slide_track:
        emit_progress(ev, "stage", "video_ingest.ocr", "Apple Vision OCR（幻灯片代表帧）")
        try:
            run(["python3", "-B", str(CORE / "scripts" / "run_vision_ocr.py"),
                 "--input-dir", str(run_dir / "slides"),
                 "--output", str(run_dir / "slides" / "ocr.jsonl"),
                 "--build-dir", str(video_dir / "ocr-build"),
                 "--receipt", str(video_dir / "ocr_receipt.json")], timeout=3600)
            ocr_status = "complete"
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            # degrade-never-kill (§5): non-zero exit (RuntimeError from run()),
            # a hung swiftc/vision subprocess (TimeoutExpired), or a missing
            # interpreter/binary (FileNotFoundError/OSError) all skip the OCR
            # layer instead of killing an otherwise-good run.
            ocr_status = "unavailable"
            warnings.append("ocr_unavailable")
            log(f"  ⚠ Vision OCR 失败，降级跳过（证据行文本置空并标注）: {str(exc)[:200]}")

    derived = _inject_derived_entries(
        manifest_path, run_dir, video_name,
        audio_file if audio_route == "extracted" else None, audio_duration, slides)
    receipt = {
        "schema_version": 1,
        "status": "complete" if not warnings else "degraded",
        "video": video_name,
        "video_source_id": (video_entry or {}).get("source_id"),
        "region_source": region_source,
        "region_confirmed_by_user": region_confirmed,
        "audio_route": audio_route,
        "audio_extracted": str(audio_file) if audio_file else None,
        "slide_track": slide_track,
        "pages": len(slides),
        "ocr": ocr_status,
        "derived_source_ids": [item["source_id"] for item in derived],
        "warnings": warnings,
    }
    atomic_json(video_dir / "ingest_receipt.json", receipt)
    return receipt


def _ingest_audio_only_degraded(run_dir: Path, manifest_path: Path,
                                video_path: Path, video_name: str,
                                video_entry: dict | None, external_audio: bool,
                                warnings: list[str]) -> dict:
    """Region unreliable branch (spec §5 行2): slide track skipped, audio track
    continues when possible. extract_video_slides requires a region, so this
    one branch extracts audio directly (thin ffmpeg glue, same decision-table
    row and fail-fast message as T3's resolve_audio)."""
    ev = run_dir.name
    warnings.append("region_unreliable_slide_track_skipped")
    log("  ⚠ 未识别到可信幻灯片区域 → 幻灯片轨降级跳过（音频轨不受影响）")
    audio_file: Path | None = None
    audio_duration: float | None = None
    if not external_audio:
        emit_progress(ev, "stage", "video_ingest.audio", "未识别到幻灯片区域：跳过幻灯片轨，仅抽取音轨")
        audio_file = run_dir / "extracted_audio.m4a"
        _extract_audio_only(video_path, audio_file)
        audio_duration = ((video_entry or {}).get("media") or {}).get("duration_seconds")
        if audio_duration is None:
            audio_duration = _probe_duration(audio_file)
    else:
        emit_progress(ev, "stage", "video_ingest.audio", "未识别到幻灯片区域：跳过幻灯片轨（事件已有独立音频）")
    derived = _inject_derived_entries(manifest_path, run_dir, video_name,
                                      audio_file, audio_duration, [])
    receipt = {
        "schema_version": 1,
        "status": "degraded",
        "video": video_name,
        "video_source_id": (video_entry or {}).get("source_id"),
        "region_source": "auto",
        "region_confirmed_by_user": False,
        "audio_route": "external" if external_audio else "extracted",
        "audio_extracted": str(audio_file) if audio_file else None,
        "slide_track": False,
        "pages": 0,
        "ocr": "skipped",
        "derived_source_ids": [item["source_id"] for item in derived],
        "warnings": warnings,
    }
    atomic_json(run_dir / "video" / "ingest_receipt.json", receipt)
    return receipt


def stage_audio_prepare(run_dir: Path, manifest: Path) -> None:
    log("Stage 2/9: 音频预处理 (arnndn RNN 降噪)...")
    run(["python3", "-B", str(CORE / "scripts/prepare_audio.py"),
         "--manifest", str(manifest),
         "--run-dir", str(run_dir / "prepared"),
         "--enhanced"])


def detect_language(run_dir: Path) -> list[str]:
    """Probe the enhanced audio at three positions and report detected languages.

    Uses 30-second whisper.cpp probes at 10%/50%/90% of duration. A single
    probe can miss code-switching, so disagreement between probes is itself
    signal: the caller should then prefer the code-switching-capable engine.
    """
    if not WHISPER_BIN.exists():
        return []
    detections: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for wav in (run_dir / "prepared" / "artifacts" / "audio" / "enhanced").glob("*.wav"):
            duration = None
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                capture_output=True, text=True, timeout=30, check=False)
            try:
                duration = float(probe.stdout.strip())
            except ValueError:
                duration = None
            if not duration or duration < 60:
                offsets = [0.0]
            else:
                offsets = [duration * 0.1, duration * 0.5, duration * 0.9]
            for offset in offsets:
                sample = Path(tmp) / f"probe_{int(offset)}.wav"
                seg = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.0f}", "-t", "30",
                     "-i", str(wav), "-c:a", "pcm_s16le", str(sample)],
                    capture_output=True, text=True, timeout=60, check=False)
                if seg.returncode != 0 or not sample.exists():
                    continue
                out = subprocess.run(
                    [str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(sample),
                     "-otxt", "-of", str(sample.with_suffix("")), "--detect-language"],
                    capture_output=True, text=True, timeout=300, check=False)
                match = re.search(r"auto-detected language: (\w+)", out.stderr + out.stdout)
                if match:
                    detections.append(match.group(1))
    return detections


def stage_asr(run_dir: Path, manifest: Path) -> tuple[Path | None, Path | None]:
    """Language-adaptive ASR.

    Strategy (protects mixed Chinese/English speech — mixing is valid content,
    never contamination):
      - probes agree on 'en'  -> whisper.cpp full-file --language en (fast, exact)
      - probes agree on 'zh'  -> Qwen3-ASR 'auto' over segmented windows
      - probes disagree/other -> Qwen3-ASR 'auto' (code-switching capable)
    Returns (qwen_json_path | None, whisper_json_path | None).
    """
    ev = run_dir.name
    emit_progress(ev, "stage", "lang_probe", "语言探测（3 点采样 whisper 探针）")
    probes = detect_language(run_dir)
    log(f"Stage 3/9: 语音识别（语言探测: {probes or '未知'}）...")
    unique = set(probes)
    if unique == {"en"} and WHISPER_BIN.exists():
        log("  英语主导 → whisper.cpp 全文件转录")
        emit_progress(ev, "stage", "asr.whisper", "whisper.cpp 全文件转录（英语）")
        whisper_json = _whisper_full_file(run_dir, "en")
        return None, whisper_json
    if unique and WHISPER_BIN.exists() and "en" not in unique:
        log("  非英语主导 → Qwen3-ASR(Chinese) + whisper auto 交叉校验/混说基础")
        # auto (unforced) lets whisper render code-switched speech natively;
        # record assembly decides per transcript whether whisper is the base.
        emit_progress(ev, "stage", "asr.whisper", "whisper.cpp 交叉校验转录（auto）")
        whisper_json = _whisper_full_file(run_dir, None)
    elif not probes and WHISPER_BIN.exists():
        log("  探测失败 → whisper.cpp 全文件转录（默认英语）")
        emit_progress(ev, "stage", "asr.whisper", "whisper.cpp 全文件转录（探测失败默认英语）")
        whisper_json = _whisper_full_file(run_dir, "en")
        return None, whisper_json
    else:
        log("  混合语言/探测分歧 → Qwen3-ASR auto（支持中英混说）")
        if WHISPER_BIN.exists():
            emit_progress(ev, "stage", "asr.whisper", "whisper.cpp 交叉校验转录（auto）")
        whisper_json = _whisper_full_file(run_dir, None) if WHISPER_BIN.exists() else None

    # Qwen3-ASR is an optional local enhancement.  Preserve a usable Whisper
    # baseline when it is absent; never send audio elsewhere to compensate.
    if QWEN_PYTHON is None or not QWEN_PYTHON.is_file():
        if whisper_json is not None:
            log("  ⚠ Qwen3-ASR 未安装：中文/混合语音使用 Whisper 基础模式，建议安装增强组件")
            emit_progress(ev, "stage", "asr.qwen", "Qwen3-ASR 未安装，已降级为 Whisper 基础模式")
            return None, whisper_json
        raise RuntimeError("Qwen3-ASR 未安装，且 Whisper 基础转写不可用")

    # Qwen3-ASR segmented path (zh or mixed)
    log("  音频分段 (20s 窗口 × 3 音轨)...")
    emit_progress(ev, "stage", "asr.segment", "音频分段（20s 窗口 × 3 音轨）")
    run(["python3", "-B", str(CORE / "scripts/segment_asr_windows.py"),
         "--manifest", str(manifest),
         "--prepared-run", str(run_dir / "prepared"),
         "--output-dir", str(run_dir / "asr_windows"),
         "--resume"])
    mem = check_memory()
    if mem["available_gb"] < RESERVE_MEMORY_GB + 3.2:
        log(f"  ⚠ 内存不足 ({mem['available_gb']:.1f}GB)，先释放 Ollama...")
        unload_ollama()
        time.sleep(5)
    emit_progress(ev, "stage", "asr.qwen", "Qwen3-ASR 窗口转录（MPS）")
    run([str(QWEN_PYTHON), "-B", str(QWEN_SCRIPT),
         "--input", str(run_dir / "asr_windows" / "flat"),
         "--output-dir", str(run_dir / "asr_primary"),
         "--model", "Qwen/Qwen3-ASR-1.7B",
         # qwen_asr validates explicit names (no "auto"); Chinese natively
         # covers zh<->en code-switching, so any zh probe selects it.
         "--language", "Chinese" if "zh" in probes else "English",
         "--device", "mps",
         "--resume"],
        # windowed zh ASR needs ~8-15s per 20s window; scale the cap to the
        # workload instead of the fixed 3600s that killed a 418-window batch
        timeout=max(3600, len(list((run_dir / "asr_windows" / "flat").glob("*.wav"))) * 25),
        env={"NUMBA_CACHE_DIR": str(run_dir / ".numba_cache"),
             "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    return run_dir / "asr_primary" / "qwen3_asr_candidates.json", whisper_json


def _whisper_full_file(run_dir: Path, language: str | None) -> Path | None:
    """Transcribe every enhanced WAV with whisper.cpp; returns the last JSON path."""
    last: Path | None = None
    for wav in sorted((run_dir / "prepared" / "artifacts" / "audio" / "enhanced").glob("*.wav")):
        stem = run_dir / f"whisper_{wav.stem}"
        target = Path(str(stem) + ".json")
        if target.is_file() and target.stat().st_size > 100:
            last = target  # idempotent: keep cached transcript, never re-burn
            continue
        cmd = [str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(wav), "-oj", "-of", str(stem)]
        if language:
            cmd += ["--language", language]
        run(cmd, timeout=3600)
        last = target
    return last


def _ascii_ratio(text: str) -> float:
    """Fraction of latin letters among CJK+latin characters."""
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return latin / max(latin + cjk, 1)


def _ts(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    return round(sum(float(p) * m for p, m in zip(reversed(parts), (1, 60, 3600))), 3)


def _whisper_hallucination_score(texts: list[str]) -> float:
    """0..1 hallucination signature strength in a whisper transcript.

    Repetition loops ("Thank you." x N) and generic filler phrases are the
    classic whisper failure mode on audio whose language it misdetected.
    """
    from collections import Counter
    norm = [re.sub(r"\s+", " ", t or "").strip().lower().rstrip(".!?，。！？") for t in texts]
    norm = [t for t in norm if t]
    if not norm:
        return 1.0
    _, top_count = Counter(norm).most_common(1)[0]
    generic = {"thank you", "thanks for watching", "thank you for watching",
               "you", "[music]", "[applause]", "please subscribe", "amara"}
    dup = top_count / len(norm) if len(norm) >= 3 else 0.0
    gen = sum(1 for t in norm if t in generic) / len(norm)
    uniq = 1 - len(set(norm)) / len(norm)
    return min(max(dup, gen, uniq), 1.0)


def _arbitrate_track_engine(whisper_text: str, qwen_text: str, url: str = OLLAMA_URL,
                            model: str = OLLAMA_MODEL) -> str:
    """When whisper and Qwen disagree on script, one of them is hallucinating
    (whisper en-loops on zh audio) or transliterating (Qwen zh on en audio).
    A local LLM looks at both transcripts and picks the faithful engine.
    Defaults to qwen (zh-safe) on any failure."""
    import urllib.request
    prompt = (
        "下面是同一段音频由两个引擎给出的转写片段。其中一个是忠实的语音转写，"
        "另一个是失效输出（在音频语言误判下的幻听，或把外语音译成无意义本地文字）。\n\n"
        f"【转写1】（拉丁字母）\n{whisper_text[:700]}\n\n"
        f"【转写2】（中文）\n{qwen_text[:700]}\n\n"
        "请分别评估每段转写：是否为通顺、语义连贯、信息量正常的自然语句？\n"
        "失效特征：词汇堆砌无语法、语义荒诞、音译串（如把外语发音硬写成本地字）、"
        "无意义重复、内容空洞。\n"
        "判断哪一段更可能是忠实转写。若两段都失效或无法判断，选 content_vacant。\n"
        '只输出 JSON：{"faithful": "1"（转写1忠实）|"2"（转写2忠实）|"none"}'
    )
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False,
                          "keep_alive": "5m", "think": False,
                          "options": {"temperature": 0.1, "num_predict": 64}}).encode("utf-8")
    for _ in range(3):
        try:
            request = urllib.request.Request(url.rstrip("/") + "/api/generate", data=payload,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = json.loads(response.read().decode("utf-8")).get("response", "")
            match = re.search(r'"(1|2|none)"', raw)
            if match:
                picked = match.group(1)
                if picked == "1":
                    return "whisper"
                if picked == "2":
                    return "qwen"
                return "qwen"  # both failed or unclear: zh-safe default
        except (OSError, ValueError, RuntimeError):
            time.sleep(2)
    return "qwen"


def _decide_track_engine(tag: str, whisper_paths: list[Path], whisper_texts: dict[str, str],
                          qwen_track_text: dict[str, str], qwen_available: bool) -> str:
    """Per-track ASR engine choice.

    Routing table (verified against real hallucination/transliteration
    artifacts; see tests/test_engine_arbitration.py):
      no whisper transcript        -> qwen if available else whisper
      whisper agrees audio is zh   -> qwen (zh quality)
      script conflict (whisper claims latin, qwen produced zh):
          obvious hallucination loop  -> qwen (no LLM needed)
          otherwise                   -> local-LLM arbitration
      both engines see latin       -> whisper
    """
    ratio = _ascii_ratio(whisper_texts.get(tag, ""))
    q_ratio = _ascii_ratio(qwen_track_text.get(tag, ""))
    if tag not in whisper_texts:
        return "qwen" if qwen_available else "whisper"
    if not qwen_track_text.get(tag):
        return "whisper"  # no cross-check available; whisper is all we have
    if ratio <= 0.15:
        return "qwen" if qwen_available else "whisper"
    if qwen_available and q_ratio <= 0.15:
        seg_texts = [seg.get("text", "") for seg in json.loads(
            next(p for p in whisper_paths if p.stem == f"whisper_{tag}")
            .read_text(encoding="utf-8")).get("transcription", [])]
        if _whisper_hallucination_score(seg_texts) >= 0.5:
            return "qwen"
        return _arbitrate_track_engine(whisper_texts[tag], qwen_track_text.get(tag, ""))
    return "whisper"


def stage_literal_records(run_dir: Path, qwen_json: Path | None, whisper_json: Path | None) -> Path:
    """Build literal records.

    Whisper path (English-dominant): one record per whisper segment with
    precise timestamps — this is the foundation transcript.
    Qwen path (Chinese/mixed): one record per 20s window with route
    agreement and overlap dedup; whisper output stays as a cross-check
    artifact only and never rewrites the literal layer.
    """
    log("Stage 4/9: 组装逐句记录...")
    import json as _json

    records: list[dict] = []
    # Always read EVERY whisper transcript in the run dir: the caller may
    # pass only one path (full-ASR returns the last, --skip-asr an arbitrary
    # glob entry), which would blind per-track engine selection to the rest.
    whisper_paths = sorted(Path(run_dir).glob("whisper_*.json"))
    qwen_available = qwen_json is not None and Path(qwen_json).exists()
    # Per-track engine selection: probes can mislabel a whole track (a pure
    # zh track probed 'en' self-corrected only by luck) and an aggregate
    # ratio hides per-track splits, so each track's engine is chosen from
    # its OWN whisper transcript when one exists:
    #   latin ratio > 0.15  -> en-dominant or code-switching: whisper
    #   latin ratio <= 0.15 -> zh-dominant: Qwen windows (better zh quality)
    whisper_texts: dict[str, str] = {}
    for path in whisper_paths:
        data = _json.loads(path.read_text(encoding="utf-8"))
        tag = path.stem.removeprefix("whisper_")  # prefix-only: round-trips with f"whisper_{tag}" lookups
        whisper_texts[tag] = " ".join(
            seg.get("text", "") for seg in data.get("transcription", []))
    qwen_items: list[dict] = []
    qwen_tags: set[str] = set()
    if qwen_available:
        qwen_items = _json.loads(Path(qwen_json).read_text(encoding="utf-8")).get("items", [])
        qwen_tags = {Path(item["audio"]).stem.split("_")[0] for item in qwen_items}
    qwen_track_text: dict[str, str] = {}
    for item in qwen_items:
        item_tag = Path(item["audio"]).stem.split("_")[0]
        qwen_track_text[item_tag] = qwen_track_text.get(item_tag, "") + " " + (item.get("text") or "")
    track_engine: dict[str, str] = {}
    for tag in sorted(set(whisper_texts) | qwen_tags):
        track_engine[tag] = _decide_track_engine(tag, whisper_paths, whisper_texts,
                                                 qwen_track_text, qwen_available)
        log(f"    轨 {tag}: whisper拉丁比 {_ascii_ratio(whisper_texts.get(tag, '')):.2f}"
            f" / qwen拉丁比 {_ascii_ratio(qwen_track_text.get(tag, '')):.2f} → {track_engine[tag]}")

    from collections import defaultdict
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for item in qwen_items:
        stem = Path(item["audio"]).stem
        window_id, route = stem.split("__", 1)
        grouped[window_id][route] = item.get("text", "").strip()
    import difflib

    def compact(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    ordinal = 0
    track_counts: dict[str, int] = {}
    for tag in sorted(track_engine):
        if track_engine[tag] == "qwen" and not any(w.split("_")[0] == tag for w in grouped):
            # literal-layer completeness invariant: a track routed to qwen
            # whose window set is missing (partial --resume, filtered output)
            # falls back to its whisper transcript — never vanish silently
            log(f"    轨 {tag}: qwen 窗口缺失 → 回退 whisper（防整轨丢失）")
            track_engine[tag] = "whisper"
        track_counts[tag] = 0
        previous_text = ""  # overlap dedup never crosses a track boundary
        if track_engine[tag] == "whisper":
            path = next(p for p in whisper_paths if p.stem == f"whisper_{tag}")
            wdata = _json.loads(path.read_text(encoding="utf-8"))
            for seg in wdata.get("transcription", []):
                t0, t1 = _ts(seg["timestamps"]["from"]), _ts(seg["timestamps"]["to"])
                if t1 <= t0:  # zero/negative-length whisper artifact; carries no timing
                    continue
                text = seg.get("text", "").strip()
                if not text:
                    continue
                ordinal += 1
                track_counts[tag] += 1
                records.append({
                    "record_id": f"R{ordinal:06d}",
                    "source_id": f"{tag}_{t0*1000:.0f}_{t1*1000:.0f}",
                    "start_seconds": t0, "end_seconds": t1,
                    "clean_literal": text, "raw_text": text,
                    "certainty": "medium", "uncertainty": None,
                    "route_agreement_min": 1.0,
                    "engine": "whisper-auto (per-track en/code-switching)",
                    "evidence_ids": [f"A{ordinal:06d}"],
                })
        else:
            for idx, (window_id, routes) in enumerate(sorted(
                    (w, r) for w, r in grouped.items() if w.split("_")[0] == tag)):
                preferred = routes.get("normalized") or routes.get("impulse_noise_reduced") or next(iter(routes.values()), "")
                values = list(routes.values())
                pairs = [difflib.SequenceMatcher(None, compact(a), compact(b), autojunk=False).ratio()
                         for i, a in enumerate(values) for j, b in enumerate(values) if i < j]
                agreement = min(pairs) if pairs else 0.0
                clean = preferred.strip()
                if previous_text and clean:
                    left, right = compact(previous_text), compact(clean)
                    for size in range(min(80, len(left), len(right)), 3, -1):
                        if left[-size:] == right[:size]:
                            # map compacted match length back to a RAW offset:
                            # mixed-script text carries spaces, so a raw slice
                            # at [size+cut:] would cut too little and leave
                            # duplicated fragments at window joins
                            idx, counted, start = 0, 0, len(preferred) - len(preferred.lstrip())
                            while idx + start < len(preferred) and counted < size:
                                if not preferred[idx + start].isspace():
                                    counted += 1
                                idx += 1
                            clean = preferred[start + idx:].strip() or clean
                            break
                if not clean:
                    continue
                parts = window_id.split("_")
                # window ids carry zero-padded milliseconds (segment_asr_windows)
                start_s = float(parts[1]) / 1000 if len(parts) > 1 else idx * 15
                end_s = float(parts[2]) / 1000 if len(parts) > 2 else start_s + 20
                certainty = "medium" if agreement >= 0.93 else "low"
                uncertainty = None if agreement >= 0.93 else "acoustic_route_disagreement; note text must not repair this transcript"
                ordinal += 1
                track_counts[tag] += 1
                records.append({
                    "record_id": f"R{ordinal:06d}", "source_id": window_id,
                    "start_seconds": round(start_s, 3), "end_seconds": round(end_s, 3),
                    "clean_literal": clean, "raw_text": preferred,
                    "certainty": certainty, "uncertainty": uncertainty,
                    "route_agreement_min": round(agreement, 4), "engine": "qwen-window-zh",
                    "evidence_ids": [f"A{ordinal:06d}"],
                })
                previous_text = clean
    w_count = sum(1 for t in track_engine.values() if t == "whisper")
    q_count = len(track_engine) - w_count
    engine = f"per-track hybrid (whisper {w_count} / qwen-zh {q_count})"
    if not records:
        raise FileNotFoundError("no ASR output available for literal record assembly")

    output = run_dir / "literal_records.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        for r in records:
            f.write(_json.dumps(r, ensure_ascii=False) + "\n")
    atomic_json(run_dir / "literal_receipt.json",
                {"status": "complete", "record_count": len(records), "engine": engine,
                 "tracks": {t: {"engine": track_engine[t], "records": track_counts[t]}
                            for t in sorted(track_engine)}})
    log(f"  生成 {len(records)} 条逐句记录 ({engine})")
    return output

def _slide_image_evidence(run_dir: Path) -> list[dict]:
    """Build I###### image evidence rows from video_ingest artifacts (design §4.4).

    Returns [] when the slide track never stood up (pure-audio event, region
    unreliable, segmentation-anomaly retraction) — stage_evidence output then
    stays byte-identical to the classic audio-only behavior. OCR failures are
    honest, never fatal: page text goes empty with an uncertainty annotation
    (spec §5「OCR 单页失败」), and the coverage law downstream still holds.
    """
    receipt_path = run_dir / "video" / "ingest_receipt.json"
    slides_path = run_dir / "slides" / "slides.json"
    if not receipt_path.is_file() or not slides_path.is_file():
        return []
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        slides_doc = json.loads(slides_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not receipt.get("slide_track"):
        return []
    slides = slides_doc.get("slides") or []
    if not slides:
        return []
    video_name = slides_doc.get("video") or receipt.get("video")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    page_source = {item.get("page_id"): item.get("source_id")
                   for item in manifest.get("files", [])
                   if item.get("kind") == "image" and "derived_from" in item}
    ocr_rows: dict[str, dict] = {}
    ocr_path = run_dir / "slides" / "ocr.jsonl"
    if ocr_path.is_file():
        for line in ocr_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("file") or ""):
                ocr_rows[Path(str(row["file"])).stem] = row
    rows: list[dict] = []
    for index, slide in enumerate(slides, start=1):
        page_id = slide.get("page_id") or f"P{index}"
        source_id = page_source.get(page_id)
        if not source_id:
            # manifest injection is part of video_ingest; a missing mapping is
            # an internal wiring bug — fail loud, never emit a dangling row.
            raise RuntimeError(f"幻灯片页 {page_id} 缺少 manifest 派生条目（video_ingest 接线错误）")
        stem = Path(str(slide.get("image") or f"{page_id}.png")).stem
        ocr_row = ocr_rows.get(stem)
        if ocr_row is None:
            text = ""
            confidence = {"route": "unavailable", "quality": "low"}
            uncertainty = "ocr_unavailable: 幻灯片图像保留，无识别文本"
        elif ocr_row.get("error"):
            text = ""
            confidence = {"route": "apple_vision", "quality": "low"}
            uncertainty = f"ocr_page_failed: {ocr_row['error']}"
        else:
            text = "\n".join(
                str(item.get("text") or "").strip()
                for item in (ocr_row.get("items") or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip())
            confidence = {"route": "apple_vision", "quality": "medium"}
            uncertainty = None
        rows.append({
            "evidence_id": f"I{index:06d}",
            "source_id": source_id,
            "kind": "image",
            "locator": {"page_id": page_id,
                        "time_ranges": slide.get("time_ranges") or [],
                        "video": video_name},
            "literal_text": text,
            "confidence": confidence,
            "uncertainty": uncertainty,
        })
    return rows


def stage_evidence(run_dir: Path, records_path: Path) -> Path:
    """Generate evidence JSONL aligned 1:1 with literal records.

    Evidence IDs reuse each record's own evidence_ids so downstream coverage
    checks (build_package_v3) can never see an ID mismatch.

    Screen-recording extension (design §4.4): when video_ingest established a
    slide track, I###### image rows are appended AFTER the audio rows, and an
    audio-only sidecar (evidence/evidence_audio_only.jsonl) is written. v1
    decision (§3.4): image rows never enter the reconcile view — relevance_filter
    and quality_gate consume the sidecar instead. Pure-audio events write no
    sidecar and their evidence.jsonl/receipt stay byte-identical to before.
    """
    log("Stage 5/9: 生成证据文件...")
    records = load_jsonl(records_path)
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    receipt = run_dir / "literal_receipt.json"
    engine = "unknown"
    if receipt.exists():
        try:
            engine = json.loads(receipt.read_text(encoding="utf-8")).get("engine", engine)
        except json.JSONDecodeError:
            pass

    seen: set[str] = set()
    evidence = []
    for r in records:
        eid = (r.get("evidence_ids") or [f"A{len(evidence)+1:06d}"])[0]
        if eid in seen:
            # silent renumbering would desync record.evidence_ids from the
            # emitted evidence row — the exact mismatch this stage must prevent
            raise ValueError(f"duplicate evidence id {eid}: literal assembly emitted colliding ids")
        seen.add(eid)
        ev = {
            "evidence_id": eid,
            "source_id": r.get("source_id", "F000001"),
            "kind": "audio",
            "locator": {"start_seconds": r["start_seconds"], "end_seconds": r["end_seconds"], "window_id": r.get("source_id")},
            "literal_text": r["clean_literal"],
            "confidence": {"route": engine, "quality": r.get("certainty", "medium"), "agreement": r.get("route_agreement_min")},
            "uncertainty": r.get("uncertainty"),
        }
        evidence.append(ev)

    image_rows = _slide_image_evidence(run_dir)
    audio_rows = evidence
    evidence = evidence + image_rows

    output = evidence_dir / "evidence.jsonl"
    with open(output, "w", encoding="utf-8") as f:
        for e in evidence:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    sidecar = evidence_dir / "evidence_audio_only.jsonl"
    if image_rows:
        # audio-only view for relevance_filter / quality_gate (v1 决定，§3.4)
        with sidecar.open("w", encoding="utf-8") as f:
            for e in audio_rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    elif sidecar.exists():
        # Retry hygiene: an earlier run established the slide track and this
        # rerun degraded to zero image rows. Downstream consumes the sidecar
        # "when present" and build_package_v3 would later SystemExit on the
        # stale disposition coverage — so sweep it. Pure-audio runs never
        # create it in the first place (byte-identity law).
        sidecar.unlink()

    receipt_payload: dict[str, Any] = {"status": "complete", "evidence_count": len(evidence), "engine": engine}
    if image_rows:
        receipt_payload["image_evidence_count"] = len(image_rows)
    atomic_json(evidence_dir / "evidence_receipt.json", receipt_payload)
    return output


def stage_relevance(run_dir: Path, records_path: Path, evidence_path: Path) -> tuple[Path, Path]:
    """Annotate records with relevance labels; build the reconcile evidence view.

    The canonical literal_records.jsonl and evidence.jsonl stay complete
    (讲稿是基础); only the topic-reconcile input excludes logistics chatter.
    """
    log("Stage 5b: 无关话语过滤（词法候选 + LLM 复核，只标注不删除）...")
    relevance_dir = run_dir / "relevance"
    relevance_dir.mkdir(parents=True, exist_ok=True)
    annotated = relevance_dir / "literal_records_annotated.jsonl"
    view = relevance_dir / "evidence_for_reconcile.jsonl"
    receipt = relevance_dir / "receipt.json"
    # v1 决定（design §3.4）：evidence_for_reconcile 视图保持 AUDIO-ONLY。
    # relevance_filter 原样透传输入证据行，故 video 事件在这里改喂 stage_evidence
    # 产出的 audio-only 侧车文件（image 行永不进 reconcile 视图，run_ollama_reconcile
    # 的按证据 ID 覆盖契约与 token 预算不受影响）。纯音频事件无侧车，命令与既往一致。
    sidecar = run_dir / "evidence" / "evidence_audio_only.jsonl"
    evidence_input = sidecar if sidecar.is_file() else evidence_path
    run(["python3", "-B", str(CORE / "scripts" / "relevance_filter.py"),
         "--records", str(records_path),
         "--records-out", str(annotated),
         "--evidence", str(evidence_input),
         "--evidence-for-reconcile", str(view),
         "--receipt", str(receipt),
         "--ollama-url", OLLAMA_URL,
         "--model", OLLAMA_MODEL],
        timeout=1800)
    try:
        summary = json.loads(receipt.read_text(encoding="utf-8"))
        log(f"  标注 {summary['records_total']} 条: {summary['labels']}, 剔出主题层 {summary['excluded_from_reconcile']} 条")
    except (OSError, json.JSONDecodeError):
        pass
    return annotated, view


def _spans_intersect(start: float, end: float, time_ranges: list) -> bool:
    """Closed-interval intersection between one record span and a slide's
    time_ranges (design §4.5: 确定性求交，无阈值调参). Malformed ranges are
    skipped, never fatal."""
    for pair in time_ranges or []:
        try:
            r_start, r_end = float(pair[0]), float(pair[1])
        except (IndexError, TypeError, ValueError):
            continue
        if start <= r_end and end >= r_start:
            return True
    return False


def stage_slide_align(run_dir: Path, evidence_path: Path, records_path: Path) -> Path | None:
    """幻灯片对齐站（design §3.4/§4.5）：产出 relations/final.jsonl.

    Runs only when the slide track stood up; pure-audio events return None and
    stage_build_package's empty-relations placeholder stays in charge (classic
    path byte-identical). v1 relation 一律 "unknown"（未经 LLM 分类，诚实标注，
    铁律 #2/#6）；decision_route 记录候选来源：
      temporal_overlap  音频抽自同一视频（时间轴同源）→ 时间段确定性求交
      lexical           独立音频文件（时间轴不同源）→ align_audio_slides 的
                        CJK-compact bigram 相似度（top-k/minimum-score 现参数）
    COVERAGE LAW (build_package_v3 SystemExit 防线): every image evidence id
    appears in exactly one row — pages without candidates still get a row with
    candidate_audio_records=[].
    """
    ev = run_dir.name
    receipt_path = run_dir / "video" / "ingest_receipt.json"
    receipt: dict = {}
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            receipt = {}
    evidence = load_jsonl(evidence_path)
    image_rows = [row for row in evidence if row.get("kind") == "image"]
    if not receipt.get("slide_track") or not image_rows:
        emit_progress(ev, "stage", "slide_align", "无幻灯片轨，跳过对齐")
        # Retry hygiene (same standard as the stage_evidence sidecar sweep):
        # an established run-1 left REAL relations behind; on a degraded rerun
        # they would ride --relations into build_package_v3 and SystemExit on
        # the coverage mismatch — every retry would fail. Sweep them so the
        # classic empty placeholder in stage_build_package takes over.
        stale_dir = run_dir / "relations"
        for stale in (stale_dir / "final.jsonl", stale_dir / "slide_align_receipt.json"):
            if stale.exists():
                stale.unlink()
        return None
    records = load_jsonl(records_path)
    route = "temporal_overlap" if receipt.get("audio_route") == "extracted" else "lexical"
    log(f"幻灯片对齐: {len(image_rows)} 页 × {len(records)} 条记录（route={route}）...")

    relations: list[dict] = []
    for index, row in enumerate(image_rows, start=1):
        slide_text = str(row.get("literal_text") or "")
        time_ranges = (row.get("locator") or {}).get("time_ranges") or []
        candidates: list[dict] = []
        if route == "temporal_overlap":
            for record in records:
                try:
                    start, end = float(record["start_seconds"]), float(record["end_seconds"])
                except (KeyError, TypeError, ValueError):
                    continue
                if _spans_intersect(start, end, time_ranges):
                    candidates.append({
                        "record_id": record["record_id"],
                        "lexical_score": round(_ALIGN.similarity(
                            slide_text, record.get("clean_literal") or ""), 4),
                    })
            candidates.sort(key=lambda item: item["record_id"])
        else:
            ranked = sorted(
                ((_ALIGN.similarity(slide_text, record.get("clean_literal") or ""), record)
                 for record in records),
                key=lambda pair: (-pair[0], pair[1]["record_id"]))
            candidates = [{"record_id": record["record_id"], "lexical_score": round(score, 4)}
                          for score, record in ranked[:_LEXICAL_TOP_K]
                          if score >= _LEXICAL_MIN_SCORE]
        relations.append({
            "relation_id": f"R{index:06d}",
            "slide_source_id": row["source_id"],
            "slide_evidence_ids": [row["evidence_id"]],
            "candidate_audio_records": candidates,
            "relation": "unknown",
            "decision_route": route,
        })

    all_image_ids = {row["evidence_id"] for row in image_rows}
    union = {eid for relation in relations for eid in relation["slide_evidence_ids"]}
    if union != all_image_ids:
        # coverage law self-check: never hand build_package_v3 a partial set.
        # Regression guard only — relations are constructed 1:1 from
        # image_rows above, so this can fire only if that construction is
        # ever edited without preserving the coverage law.
        raise RuntimeError(
            f"slide_align 覆盖铁律被破坏: relations 覆盖 {len(union)}/{len(all_image_ids)} 个 image 证据 ID")

    relations_dir = run_dir / "relations"
    relations_dir.mkdir(parents=True, exist_ok=True)
    output = relations_dir / "final.jsonl"
    with open(output, "w", encoding="utf-8") as f:
        for relation in relations:
            f.write(json.dumps(relation, ensure_ascii=False) + "\n")
    atomic_json(relations_dir / "slide_align_receipt.json", {
        "schema_version": 1,
        "status": "complete",
        "decision_route": route,
        "relation_count": len(relations),
        "image_evidence_count": len(all_image_ids),
        "candidate_audio_record_count": len({c["record_id"] for r in relations
                                             for c in r["candidate_audio_records"]}),
        "coverage_complete": True,
        "relation_decisions_final": False,  # v1: relation=unknown，未经 LLM 分类
        "content_included": False,
    })
    log(f"  生成 {len(relations)} 行 relations（覆盖全部 image 证据；relation=unknown 未分类）")
    return output


def stage_reconcile(run_dir: Path, evidence_path: Path) -> Path:
    """Run Ollama reconciliation to extract topic units."""
    log("Stage 6/9: 主题提取与索引构建 (Ollama qwen3:8b)...")
    ensure_ollama()
    
    reconciled_dir = run_dir / "reconciled"
    reconciled_dir.mkdir(parents=True, exist_ok=True)
    
    output = reconciled_dir / "reconciled.json"
    run(["python3", "-B", str(CORE / "scripts/run_ollama_reconcile.py"),
         "--evidence", str(evidence_path),
         "--output", str(output),
         "--receipt", str(reconciled_dir / "reconcile_receipt.json"),
         "--model", OLLAMA_MODEL,
         "--keep-alive", "5m",
         "--num-ctx", "8192",
         "--num-predict", "1024",
         "--chunk-size", "5",
         "--min-chunk-size", "5",
         "--max-attempts", "2",
         "--checkpoint-dir", str(reconciled_dir / "checkpoints"),
         "--coverage-policy", "conservative-fill"],
        timeout=7200)
    
    return output


def stage_notes(run_dir: Path, notes_path: Path, manifest_path: Path, records_path: Path) -> Path:
    """Process notes: evidence, retrieval, classification."""
    log("Stage 7/9: 笔记佐证处理...")
    notes_dir = run_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    
    # Build note evidence
    run(["python3", str(NOTE_LAYER / "scripts/build_note_evidence.py"),
         "--manifest", str(manifest_path),
         "--output", str(notes_dir / "note_evidence.jsonl"),
         "--receipt", str(notes_dir / "note_receipt.json")])
    
    # Retrieve links
    run(["python3", str(NOTE_LAYER / "scripts/retrieve_note_links.py"),
         "--notes", str(notes_dir / "note_evidence.jsonl"),
         "--records", str(records_path),
         "--output", str(notes_dir / "note_candidates.jsonl"),
         "--receipt", str(notes_dir / "note_candidates_receipt.json"),
         "--minimum-score", "0.20",
         "--document-minimum-score", "0.02"])
    
    # Classify relations
    ensure_ollama()
    run(["python3", str(NOTE_LAYER / "scripts/classify_note_links.py"),
         "--notes", str(notes_dir / "note_evidence.jsonl"),
         "--records", str(records_path),
         "--relations", str(notes_dir / "note_candidates.jsonl"),
         "--output", str(notes_dir / "note_relations.jsonl"),
         "--receipt", str(notes_dir / "note_relation_receipt.json"),
         "--model", OLLAMA_MODEL],
        timeout=3600)
    
    # Safety sanitize
    if (NOTE_LAYER / "scripts/sanitize_note_relations.py").exists():
        run(["python3", str(NOTE_LAYER / "scripts/sanitize_note_relations.py"),
             "--input", str(notes_dir / "note_relations.jsonl"),
             "--output", str(notes_dir / "note_relations_safe.jsonl"),
             "--receipt", str(notes_dir / "note_relation_safety_receipt.json")])
    
    return notes_dir / "note_relations_safe.jsonl" if (notes_dir / "note_relations_safe.jsonl").exists() else notes_dir / "note_relations.jsonl"


def _suggest_ocr_corrections(run_dir: Path) -> Path | None:
    """OCR 标注式修正建议（design §3.3/§4.6，用户决策⑤：ASR 原文永不静默改写）.

    Runs only when slide OCR exists; 0 corrections is a normal pass (T5 honest
    gate). ANY failure degrades to a warning — the audit station must never
    kill an otherwise-good run (spec §5). The --evidence-map is built from this
    run's own I###### image rows so correction rows carry ocr_evidence_id.
    """
    ocr_jsonl = run_dir / "slides" / "ocr.jsonl"
    records_path = run_dir / "literal_records.jsonl"
    if not ocr_jsonl.is_file() or not records_path.is_file():
        return None
    evidence = load_jsonl(run_dir / "evidence" / "evidence.jsonl")
    map_path = run_dir / "video" / "ocr_evidence_map.jsonl"
    with open(map_path, "w", encoding="utf-8") as f:
        # JSONL rows {page_id, evidence_id} — the shape suggest_ocr_corrections
        # reads (page stem like "P3" → image evidence id).
        for row in evidence:
            if row.get("kind") == "image":
                page_id = (row.get("locator") or {}).get("page_id")
                if page_id:
                    f.write(json.dumps({"page_id": page_id,
                                        "evidence_id": row["evidence_id"]},
                                       ensure_ascii=False) + "\n")
    output = run_dir / "ocr_corrections.jsonl"
    try:
        run([str(TOOLS_PYTHON), "-B", str(CORE / "scripts" / "suggest_ocr_corrections.py"),
             "--records", str(records_path),
             "--slides-ocr", str(ocr_jsonl),
             "--output", str(output),
             "--receipt", str(run_dir / "video" / "ocr_corrections_receipt.json"),
             "--evidence-map", str(map_path)], timeout=1800)
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        # degrade-never-kill (§5): covers a deleted tools-venv
        # (FileNotFoundError) and a hung subprocess (TimeoutExpired) too.
        log(f"  ⚠ OCR 修正建议生成失败，降级跳过（不影响审计结果）: {str(exc)[:200]}")
        return None
    return output


def stage_audit_claims(run_dir: Path, reconciled_path: Path) -> Path:
    """Claim-fidelity audit + repair (antonym reversals, silent garble fixes,
    invented intent, citation gaps). Repairs claims strictly from cited
    evidence; residual failures downgrade to low certainty so the
    uncertainty view surfaces them."""
    log("Stage 6b: claim 保真审计（反义误译/乱码改写/意图强加/引证缺口）...")
    output = run_dir / "reconciled/reconciled_audited.json"
    receipt = run_dir / "reconciled/claim_audit_receipt.json"
    run(["python3", "-B", str(CORE / "scripts/audit_claims.py"),
         "--reconciled", str(reconciled_path),
         "--evidence", str(run_dir / "evidence/evidence.jsonl"),
         "--output", str(output),
         "--receipt", str(receipt),
         "--model", OLLAMA_MODEL, "--keep-alive", "5m"], timeout=7200)
    summary = json.loads(receipt.read_text(encoding="utf-8"))
    log(f"  审计 {summary['audited']} 条: 首轮忠实 {summary['faithful_first_pass']}, "
        f"修复 {summary['repaired']}, 残留 {summary['residual']}")
    corrections = _suggest_ocr_corrections(run_dir)
    if corrections is not None:
        count = sum(1 for line in corrections.read_text(encoding="utf-8").splitlines() if line.strip())
        log(f"  OCR 修正建议: {count} 条（标注式证据，ASR 原文不改写）")
    return output


def merge_excluded_dispositions(run_dir: Path, reconciled_path: Path, annotated_path: Path,
                                evidence_path: Path) -> Path:
    """Give logistics-excluded evidence AND slide image rows an explicit disposition.

    Real constraints that force this merge (all committed behavior):
    (a) build_package_v3.py:70-71 SystemExits unless the disposition id-set
        equals the full evidence id-set — the reconcile ran on the filtered
        AUDIO-ONLY view, so ids excluded from it (or never in it) would crash
        the package build if left undisposed.
    (b) build_package_v3.py:150 renders ONLY dispositions with status in
        {uncertain, conflict} into 《05_不确定与冲突》 — a status outside
        that set (e.g. a hypothetical "slide_layer") would silently hide the
        row from every human-facing surface (铁律 #6).
    (c) the committed disposition enum is covered/duplicate/nonsemantic/
        uncertain/conflict (run_ollama_reconcile.py:43), and that schema
        REQUIRES a 'reason' string — image rows therefore carry both
        'reason' (what 05 renders) and 'note' (same text, machine-facing).

    Screen-recording v1 (§3.4): image rows (I######) are outside the
    reconcile view by design; they are marked 'uncertain' with the honest
    explanation rendered in 05 — slide-audio correspondence is unclassified
    in v1, which belongs on the uncertainty surface. Logistics-excluded ids
    keep the classic 'excluded_logistics' entries byte-identical to the
    12-station behavior (golden-pinned). Pure-audio events with no
    exclusions keep the classic early-return path.
    """
    receipt = run_dir / "relevance" / "receipt.json"
    if not receipt.is_file():
        return reconciled_path
    try:
        excluded_records = set(json.loads(receipt.read_text(encoding="utf-8"))["excluded_ids"])
    except (OSError, json.JSONDecodeError, KeyError):
        return reconciled_path
    evidence = load_jsonl(evidence_path)
    image_ids = {e["evidence_id"] for e in evidence if e.get("kind") == "image"}
    if not excluded_records and not image_ids:
        return reconciled_path
    annotated = load_jsonl(annotated_path)
    covered_by_records = {e for rec in annotated if rec["record_id"] in excluded_records
                          for e in rec.get("evidence_ids", [])}
    reconciled = json.loads(Path(reconciled_path).read_text(encoding="utf-8"))
    have = {d.get("evidence_id") for d in reconciled.get("dispositions", [])}
    all_ids = {e["evidence_id"] for e in evidence}
    for eid in sorted(all_ids - have):
        if eid in image_ids:
            explanation = ("slide image evidence: audio-only reconcile view by design (v1); "
                           "participates via relations / 03_PPT补充信息")
            reconciled.setdefault("dispositions", []).append({
                "evidence_id": eid,
                "status": "uncertain",
                "reason": explanation,   # rendered in 05 (build_package_v3.py:150)
                "note": explanation,
            })
        else:
            reconciled.setdefault("dispositions", []).append({
                "evidence_id": eid,
                "status": "excluded_logistics" if eid in covered_by_records else "unreconciled",
                "note": "excluded from topic reconcile by relevance filter" if eid in covered_by_records
                        else "not covered by reconcile chunks",
            })
    merged = run_dir / "reconciled" / "reconciled_merged.json"
    merged.write_text(json.dumps(reconciled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def stage_build_package(run_dir: Path, evidence_path: Path, records_path: Path, reconciled_path: Path, note_relations_path: Path, output_dir: Path) -> None:
    """Build the full package: index, report, database."""
    log("Stage 8/9: 构建完整报告包...")
    
    # Create empty slide relations if none exist. Screen-recording events:
    # stage_slide_align already wrote the REAL relations/final.jsonl, so this
    # placeholder only ever triggers for pure-audio events (design §7 退休路径).
    relations_dir = run_dir / "relations"
    relations_dir.mkdir(parents=True, exist_ok=True)
    slide_relations = relations_dir / "final.jsonl"
    if not slide_relations.exists():
        # build_package_v3.read_jsonl parses line-delimited JSON: an empty
        # JSONL (zero lines) is the contract; never write a JSON object here.
        slide_relations.write_text("", encoding="utf-8")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Build v3 package. Slide track status (design §3.4) gates the optional
    # --slides-dir/--ocr-corrections flags; pure-audio events pass neither and
    # build_package_v3's default output stays byte-identical (T4 pins).
    slide_track = False
    ingest_doc: dict = {}
    try:
        ingest_doc = json.loads(
            (run_dir / "video" / "ingest_receipt.json").read_text(encoding="utf-8"))
        slide_track = bool(ingest_doc.get("slide_track")) \
            and (run_dir / "slides" / "slides.json").is_file()
    except (OSError, json.JSONDecodeError):
        slide_track = False
    cmd = ["python3", "-B", str(CORE / "scripts/build_package_v3.py"),
           "--evidence", str(evidence_path),
           "--literal-record", str(records_path),
           "--relations", str(slide_relations),
           "--reconciled", str(reconciled_path),
           "--output-dir", str(output_dir)]
    if slide_track:
        cmd += ["--slides-dir", str(run_dir / "slides")]
    corrections = run_dir / "ocr_corrections.jsonl"
    if slide_track and corrections.is_file():
        cmd += ["--ocr-corrections", str(corrections)]
    run(cmd, timeout=600)
    
    # Also render note corroboration
    if note_relations_path.exists():
        run(["python3", str(NOTE_LAYER / "scripts/render_note_test.py"),
             "--records", str(records_path),
             "--notes", str(run_dir / "notes/note_evidence.jsonl"),
             "--relations", str(note_relations_path),
             "--output-dir", str(output_dir),
             "--receipt", str(output_dir / "note_render_receipt.json")])
    
    # Generate usage guide
    guide_lines = [
        "# 使用说明",
        "",
        "- 问演讲者具体说了什么：查《02_逐句会议记录》。",
        "- 快速定位主题：先查《01_主题索引》。",
        "- 阅读结论：查《04_会议报告》。",
        "- 低置信度内容：查《05_不确定与冲突.md》。",
        "- 笔记佐证：查《06_笔记佐证与冲突.md》。",
        "- AI 检索：使用 meeting.db 或 query_meeting.py。",
    ]
    if slide_track:
        # design §3.4: one navigation line, ONLY when the slide layer exists.
        # It lives here (not in build_package_v3.py) because this override is
        # the console package's final 00_使用说明.md; the pure-audio list above
        # stays byte-identical to the classic guide (golden-pinned in
        # tests/test_run_meeting_video.py).
        guide_lines.insert(3, "- 问 PPT 页面内容/对应发言：查《03_PPT补充信息》与 幻灯片/ 目录。")
    # 报告标注 (design §3.4 item1 / §5 行2): the unconfirmed-auto-region and
    # degraded-no-region cases must be marked inside outputs/. Placed after
    # the nav line (end of the guide). Pure-audio events have no ingest
    # receipt → no annotation → guide bytes identical to the classic golden.
    if slide_track and "region_auto_not_user_confirmed" in (ingest_doc.get("warnings") or []):
        # Keyed on the ingest WARNING TOKEN (not region_source): the token is
        # emitted only when the CLI-fallback detect actually ran, so an
        # off-schema hand-made region.json lacking "source" can never mistrigger.
        guide_lines.append("幻灯片区域为自动检测，未经人工确认。")
    elif not slide_track and "region_unreliable_slide_track_skipped" in (ingest_doc.get("warnings") or []):
        guide_lines.append("未识别到幻灯片区域，本次仅处理音轨。")
    guide = output_dir / "00_使用说明.md"
    guide.write_text("\n".join(guide_lines), encoding="utf-8")


def stage_validate(package_dir: Path, run_dir: Path) -> bool:
    """Validate the package deterministically, then run the quality gate."""
    log("Stage 9/9: 验证报告包...")
    result = subprocess.run(
        ["python3", "-B", str(CORE / "scripts/validate_package_v3.py"),
         "--manifest", str(package_dir.parent.parent / "manifest.json"),
         "--literal-record", str(package_dir.parent.parent / "literal_records.jsonl"),
         "--relations", str(package_dir.parent.parent / "relations/final.jsonl"),
         "--package-dir", str(package_dir)],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        log("  ✅ 确定性验证通过")
    else:
        log(f"  ⚠ 确定性验证退出码 {result.returncode}（存在不确定项属预期，不阻断；通过与否以质量门禁为准）: {result.stdout[-300:]}")

    gate_script = CORE / "scripts" / "quality_gate.py"
    if gate_script.exists():
        gate_report = package_dir / "quality_gate_report.json"
        gate_cmd = ["python3", "-B", str(gate_script),
                    "--run-dir", str(run_dir),
                    "--package-dir", str(package_dir),
                    "--output", str(gate_report)]
        audio_only = run_dir / "evidence" / "evidence_audio_only.jsonl"
        if audio_only.is_file():
            # Screen-recording v1 (§3.4): image rows (I######) are outside the
            # literal-record citation graph by design; the gate's alignment
            # check must see the same audio-only evidence layer the reconcile
            # saw, or it would flag every slide row as an orphan. Pure-audio
            # events have no sidecar — the classic invocation is unchanged.
            gate_cmd += ["--evidence", str(audio_only)]
        gate = subprocess.run(
            gate_cmd,
            capture_output=True, text=True, timeout=180
        )
        if gate.returncode == 0:
            log("  ✅ 质量门禁: PASS")
            return True
        if gate.returncode == 2:
            log("  ⚠ 质量门禁: WARN（见 quality_gate_report.json）")
            return True
        log(f"  ❌ 质量门禁: FAIL（exit {gate.returncode}）")
        try:
            failures = json.loads(gate_report.read_text(encoding="utf-8")).get("errors", [])
            for item in failures[:5]:
                log(f"     - {item}")
        except (OSError, json.JSONDecodeError):
            log(f"     {gate.stdout[-400:]}")
        return False
    log("  （quality_gate.py 不存在，跳过质量门禁）")
    return result.returncode == 0


def cleanup_input() -> None:
    """Clear input folder for next use."""
    for f in INPUT_DIR.iterdir():
        if f.is_file():
            f.unlink()
        elif f.is_dir() and f.name not in (".", ".."):
            shutil.rmtree(f)
    log("input/ 已清空，就绪下次投放")


def process_event(event: dict, args) -> bool:
    """Run the full pipeline for one event (audio files + optional notes)."""
    name = event["name"]
    run_dir = WORKSPACE / "runs" / name
    output_dir = resolve_output_dir(event)
    log(f"=== 事件处理开始: {name} ===")
    emit_progress(name, "stage", "inventory", "文件清单")

    source_dir = run_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for af in event["audio"]:
        dest = source_dir / af.name
        if not dest.exists():
            shutil.copy2(af, dest)
    for vf in event.get("video") or []:
        dest = source_dir / vf.name
        if not dest.exists():
            shutil.copy2(vf, dest)
    if event["notes"]:
        shutil.copy2(event["notes"], source_dir / event["notes"].name)

    manifest = stage_inventory(run_dir, source_dir)
    emit_progress(name, "stage", "video_ingest", "视频分解（音轨/幻灯片）")
    stage_video_ingest(run_dir, manifest, event)
    emit_progress(name, "stage", "audio_prepare", "音频预处理（arnndn RNN 降噪）")
    stage_audio_prepare(run_dir, manifest)

    if not args.skip_asr:
        qwen_json, whisper_json = stage_asr(run_dir, manifest)
    else:
        qwen_json = run_dir / "asr_primary/qwen3_asr_candidates.json"
        whisper_json = next(iter(run_dir.glob("whisper_*.json")), None)

    emit_progress(name, "stage", "literal", "组装逐句记录（逐轨引擎仲裁）")
    records_path = stage_literal_records(run_dir, qwen_json, whisper_json)
    emit_progress(name, "stage", "evidence", "生成证据文件")
    evidence_path = stage_evidence(run_dir, records_path)
    emit_progress(name, "stage", "relevance", "无关话语过滤（只标注不删除）")
    annotated_path, reconcile_view = stage_relevance(run_dir, records_path, evidence_path)
    emit_progress(name, "stage", "slide_align", "幻灯片对齐")
    stage_slide_align(run_dir, evidence_path, annotated_path)
    emit_progress(name, "stage", "reconcile", "主题提取与索引构建（Ollama）")
    reconciled_path = stage_reconcile(run_dir, reconcile_view)
    emit_progress(name, "stage", "audit", "claim 保真审计")
    reconciled_path = stage_audit_claims(run_dir, reconciled_path)

    if not args.skip_notes and event["notes"]:
        emit_progress(name, "stage", "notes", "笔记佐证处理")
        note_relations_path = stage_notes(run_dir, event["notes"], manifest, records_path)
    else:
        note_relations_path = run_dir / "notes/note_relations.jsonl"

    reconciled_path = merge_excluded_dispositions(run_dir, reconciled_path, annotated_path, evidence_path)
    if output_dir.exists():
        # Replace policy (user decision 2026-09-19): rerunning the same
        # event name supersedes the old package, so cross-event search
        # never serves stale duplicates. runs/ keeps rebuild artifacts.
        shutil.rmtree(output_dir)
    emit_progress(name, "stage", "package", f"构建完整报告包 → {output_dir}")
    stage_build_package(run_dir, evidence_path, annotated_path, reconciled_path, note_relations_path, output_dir)
    emit_progress(name, "stage", "validate", "验证报告包 + 质量门禁")
    ok = stage_validate(output_dir, run_dir)

    log(f"=== 事件完成: {name} -> {output_dir} ===")
    log("  01_主题索引.md / 02_逐句会议记录.md / 04_会议报告.md / meeting.db")
    return ok


def main():
    parser = argparse.ArgumentParser(description="一键会议处理（支持一次投放多个宣讲）")
    parser.add_argument("--name", default=None, help="会议名称（单事件时使用；默认目录名/时间戳）")
    parser.add_argument("--skip-asr", action="store_true", help="跳过 ASR（使用已有转录）")
    parser.add_argument("--skip-notes", action="store_true", help="跳过笔记处理")
    args = parser.parse_args()

    events = find_input_events()
    mem = check_memory()
    log(f"发现 {len(events)} 个事件: {[e['name'] for e in events]}")
    log(f"内存: {mem['available_gb']:.1f}GB 可用 / {mem['total_gb']:.1f}GB 总量")

    if args.name and len(events) == 1:
        events[0]["name"] = args.name

    failures = []
    try:
        for event in events:
            video_note = f"，{len(event['video'])} 个视频" if event.get("video") else ""
            emit_progress(event["name"], "event_start",
                          message=f"开始处理（{len(event['audio'])} 个音频{video_note}，笔记{'有' if event['notes'] else '无'}）")
            try:
                if not process_event(event, args):
                    failures.append(event["name"])
                    emit_progress(event["name"], "event_failed", status="failed", message="质量门禁未通过")
                else:
                    emit_progress(event["name"], "event_done", status="done", message="事件完成")
            except Exception as exc:  # one bad event must not block the rest
                log(f"❌ 事件 {event['name']} 失败: {exc}")
                emit_progress(event["name"], "event_failed", status="failed", message=str(exc)[:300])
                import traceback
                traceback.print_exc()
                failures.append(event["name"])
        unload_ollama()
        if failures:
            # Never wipe inputs of failed events: keep them in input/ for retry.
            keep = {e["name"]: e for e in events if e["name"] in failures}
            for event in events:
                if event["name"] in failures:
                    continue
                for item in event["audio"] + (event.get("video") or []) + ([event["notes"]] if event["notes"] else []):
                    try:
                        item.unlink(missing_ok=True) if item.is_file() else None
                    except OSError:
                        pass
                if event["dir"].is_dir() and event["name"] != "misc":
                    shutil.rmtree(event["dir"], ignore_errors=True)
            log(f"完成，但 {len(failures)} 个事件失败: {failures}（其输入已保留在 input/ 以便重试）")
            return 1
        cleanup_input()
        return 0
    except Exception as e:
        log(f"❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
