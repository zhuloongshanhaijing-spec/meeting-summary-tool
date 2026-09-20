#!/usr/bin/env python3
"""一键会议处理：音频 + 笔记 → 完整报告包（主题索引、逐句记录、报告、数据库、笔记佐证）

用法：
    python3 run_meeting.py [--name 会议名称]

输入：input/ 文件夹中的 .m4a/.mp3/.wav 录音 + notes.md 笔记
输出：outputs/<名称>/ 完整报告包

资源控制：大模型独占顺序执行，light 阶段最多 2 并行，系统预留 2GB。
"""

from __future__ import annotations

import argparse
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
QWEN_PYTHON = Path(_CFG["qwen_python"])
QWEN_SCRIPT = CORE / "scripts" / "run_qwen3_asr.py"
WHISPER_BIN = Path(_CFG["whisper_bin"])
WHISPER_MODEL = Path(_CFG["whisper_model"])
OLLAMA_URL = _CFG["ollama_url"]
OLLAMA_MODEL = _CFG["ollama_model"]

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
    Loose audio/notes files directly under input/ form one shared event named
    "misc". This lets a user drop several lectures at once and have each
    processed as its own meeting package.
    """
    audio_exts = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".caf"}
    note_names = {"notes.md", "note.md"}
    events: list[dict] = []
    loose_audio, loose_notes = [], None
    for entry in sorted(INPUT_DIR.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            audio = sorted(p for p in entry.iterdir() if p.suffix.lower() in audio_exts and not p.name.startswith("."))
            notes = next((p for p in entry.iterdir() if p.name.lower() in note_names), None)
            if audio:
                events.append({"name": entry.name, "dir": entry, "audio": audio, "notes": notes})
            elif notes:
                events.append({"name": entry.name, "dir": entry, "audio": [], "notes": notes})
        elif entry.suffix.lower() in audio_exts:
            loose_audio.append(entry)
        elif entry.name.lower() in note_names:
            loose_notes = entry
    if loose_audio or loose_notes:
        events.append({"name": "misc", "dir": INPUT_DIR, "audio": loose_audio, "notes": loose_notes})
    if not events:
        raise FileNotFoundError(f"input/ 中没有任何可处理内容（音频: {', '.join(sorted(audio_exts))}，笔记: notes.md）")
    return events


def stage_inventory(run_dir: Path, source_dir: Path) -> Path:
    log("Stage 1/9: 文件清单...")
    manifest_path = run_dir / "manifest.json"
    run(["python3", "-B", str(CORE / "scripts" / "meeting_pipeline.py"), "inventory",
         "--source", str(source_dir), "--output", str(manifest_path)])
    return manifest_path


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
    probes = detect_language(run_dir)
    log(f"Stage 3/9: 语音识别（语言探测: {probes or '未知'}）...")
    unique = set(probes)
    if unique == {"en"} and WHISPER_BIN.exists():
        log("  英语主导 → whisper.cpp 全文件转录")
        whisper_json = _whisper_full_file(run_dir, "en")
        return None, whisper_json
    if unique and WHISPER_BIN.exists() and "en" not in unique:
        log("  非英语主导 → Qwen3-ASR(Chinese) + whisper auto 交叉校验/混说基础")
        # auto (unforced) lets whisper render code-switched speech natively;
        # record assembly decides per transcript whether whisper is the base.
        whisper_json = _whisper_full_file(run_dir, None)
    elif not probes and WHISPER_BIN.exists():
        log("  探测失败 → whisper.cpp 全文件转录（默认英语）")
        whisper_json = _whisper_full_file(run_dir, "en")
        return None, whisper_json
    else:
        log("  混合语言/探测分歧 → Qwen3-ASR auto（支持中英混说）")
        whisper_json = _whisper_full_file(run_dir, None) if WHISPER_BIN.exists() else None

    # Qwen3-ASR segmented path (zh or mixed)
    log("  音频分段 (20s 窗口 × 3 音轨)...")
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

def stage_evidence(run_dir: Path, records_path: Path) -> Path:
    """Generate evidence JSONL aligned 1:1 with literal records.

    Evidence IDs reuse each record's own evidence_ids so downstream coverage
    checks (build_package_v3) can never see an ID mismatch.
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

    output = evidence_dir / "evidence.jsonl"
    with open(output, "w", encoding="utf-8") as f:
        for e in evidence:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    atomic_json(evidence_dir / "evidence_receipt.json", {"status": "complete", "evidence_count": len(evidence), "engine": engine})
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
    run(["python3", "-B", str(CORE / "scripts" / "relevance_filter.py"),
         "--records", str(records_path),
         "--records-out", str(annotated),
         "--evidence", str(evidence_path),
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
    return output


def merge_excluded_dispositions(run_dir: Path, reconciled_path: Path, annotated_path: Path,
                                evidence_path: Path) -> Path:
    """Give logistics-excluded evidence an explicit disposition.

    build_package_v3 requires dispositions to cover every evidence id; the
    reconcile ran on the filtered view, so excluded ids are added here with
    status 'excluded_logistics' — documented in 05, never silently dropped.
    """
    receipt = run_dir / "relevance" / "receipt.json"
    if not receipt.is_file():
        return reconciled_path
    try:
        excluded_records = set(json.loads(receipt.read_text(encoding="utf-8"))["excluded_ids"])
    except (OSError, json.JSONDecodeError, KeyError):
        return reconciled_path
    if not excluded_records:
        return reconciled_path
    annotated = load_jsonl(annotated_path)
    evidence = load_jsonl(evidence_path)
    covered_by_records = {e for rec in annotated if rec["record_id"] in excluded_records
                          for e in rec.get("evidence_ids", [])}
    reconciled = json.loads(Path(reconciled_path).read_text(encoding="utf-8"))
    have = {d.get("evidence_id") for d in reconciled.get("dispositions", [])}
    all_ids = {e["evidence_id"] for e in evidence}
    for eid in sorted(all_ids - have):
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
    
    # Create empty slide relations if none exist
    relations_dir = run_dir / "relations"
    relations_dir.mkdir(parents=True, exist_ok=True)
    slide_relations = relations_dir / "final.jsonl"
    if not slide_relations.exists():
        # build_package_v3.read_jsonl parses line-delimited JSON: an empty
        # JSONL (zero lines) is the contract; never write a JSON object here.
        slide_relations.write_text("", encoding="utf-8")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Build v3 package
    run(["python3", "-B", str(CORE / "scripts/build_package_v3.py"),
         "--evidence", str(evidence_path),
         "--literal-record", str(records_path),
         "--relations", str(slide_relations),
         "--reconciled", str(reconciled_path),
         "--output-dir", str(output_dir)],
        timeout=600)
    
    # Also render note corroboration
    if note_relations_path.exists():
        run(["python3", str(NOTE_LAYER / "scripts/render_note_test.py"),
             "--records", str(records_path),
             "--notes", str(run_dir / "notes/note_evidence.jsonl"),
             "--relations", str(note_relations_path),
             "--output-dir", str(output_dir),
             "--receipt", str(output_dir / "note_render_receipt.json")])
    
    # Generate usage guide
    guide = output_dir / "00_使用说明.md"
    guide.write_text("\n".join([
        "# 使用说明",
        "",
        "- 问演讲者具体说了什么：查《02_逐句会议记录》。",
        "- 快速定位主题：先查《01_主题索引》。",
        "- 阅读结论：查《04_会议报告》。",
        "- 低置信度内容：查《05_不确定与冲突.md》。",
        "- 笔记佐证：查《06_笔记佐证与冲突.md》。",
        "- AI 检索：使用 meeting.db 或 query_meeting.py。",
    ]), encoding="utf-8")


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
        gate = subprocess.run(
            ["python3", "-B", str(gate_script),
             "--run-dir", str(run_dir),
             "--package-dir", str(package_dir),
             "--output", str(gate_report)],
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
    output_dir = WORKSPACE / "outputs" / name
    log(f"=== 事件处理开始: {name} ===")

    source_dir = run_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for af in event["audio"]:
        dest = source_dir / af.name
        if not dest.exists():
            shutil.copy2(af, dest)
    if event["notes"]:
        shutil.copy2(event["notes"], source_dir / event["notes"].name)

    manifest = stage_inventory(run_dir, source_dir)
    stage_audio_prepare(run_dir, manifest)

    if not args.skip_asr:
        qwen_json, whisper_json = stage_asr(run_dir, manifest)
    else:
        qwen_json = run_dir / "asr_primary/qwen3_asr_candidates.json"
        whisper_json = next(iter(run_dir.glob("whisper_*.json")), None)

    records_path = stage_literal_records(run_dir, qwen_json, whisper_json)
    evidence_path = stage_evidence(run_dir, records_path)
    annotated_path, reconcile_view = stage_relevance(run_dir, records_path, evidence_path)
    reconciled_path = stage_reconcile(run_dir, reconcile_view)
    reconciled_path = stage_audit_claims(run_dir, reconciled_path)

    if not args.skip_notes and event["notes"]:
        note_relations_path = stage_notes(run_dir, event["notes"], manifest, records_path)
    else:
        note_relations_path = run_dir / "notes/note_relations.jsonl"

    reconciled_path = merge_excluded_dispositions(run_dir, reconciled_path, annotated_path, evidence_path)
    if output_dir.exists():
        # Replace policy (user decision 2026-09-19): rerunning the same
        # event name supersedes the old package, so cross-event search
        # never serves stale duplicates. runs/ keeps rebuild artifacts.
        shutil.rmtree(output_dir)
    stage_build_package(run_dir, evidence_path, annotated_path, reconciled_path, note_relations_path, output_dir)
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
            try:
                if not process_event(event, args):
                    failures.append(event["name"])
            except Exception as exc:  # one bad event must not block the rest
                log(f"❌ 事件 {event['name']} 失败: {exc}")
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
                for item in event["audio"] + ([event["notes"]] if event["notes"] else []):
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