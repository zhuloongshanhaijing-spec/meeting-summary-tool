"""Web console control plane — zero-dependency (stdlib only).

Owner of the local HTTP surface defined in docs/web-console-design.md §3.2:
static console page, multipart upload into input/<event>/, single-pipeline
supervision (queue = input/, iron rule: never two pipelines), /api/status
aggregation from disk state, outputs browsing + zip, log tailing.

State model (spec §2, single source of truth per concern):
  input/  = queue          runs/   = progress + artifacts
  outputs/= results        No authoritative in-memory state: a restarted
  server re-derives everything from disk (pidfile + jsonl + dir scans).
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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
EVENT_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]*$")
MAX_AUTO_RESTARTS = 50
DEFAULT_PORT = 8788

_supervision: dict = {"proc": None, "thread": None, "stop_requested": False,
                      "auto_restarts": 0}


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


def scan_queue() -> list[dict]:
    """input/ is the queue (same grouping rules as find_input_events)."""
    queue: list[dict] = []
    if not INPUT_DIR.exists():
        return queue
    loose_audio = loose_notes = 0
    for entry in sorted(INPUT_DIR.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            files = [p for p in entry.iterdir()
                     if p.is_file() and not p.name.startswith(".")]
            audio = [p for p in files if p.suffix.lower() in AUDIO_EXTS]
            notes = [p for p in files
                     if p.suffix.lower() in NOTE_EXTS or p.name.lower() in NOTE_CANONICAL]
            if audio or notes:
                queue.append({"name": entry.name, "audio": len(audio),
                              "notes": len(notes), "files": len(audio) + len(notes)})
        elif entry.suffix.lower() in AUDIO_EXTS:
            loose_audio += 1
        elif entry.name.lower() in NOTE_CANONICAL or entry.suffix.lower() in NOTE_EXTS:
            loose_notes += 1
    if loose_audio or loose_notes:  # loose files share one "misc" event
        queue.append({"name": "misc", "audio": loose_audio,
                      "notes": loose_notes, "files": loose_audio + loose_notes})
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
    if not scan_queue():
        return 400, "input/ 中没有可处理事件（先上传录音或笔记）"
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
    output_dir = None
    saved_audio: list[str] = []
    saved_notes: list[str] = []
    for part in parser.parts():
        if part.filename is None:
            text = part.read_text().strip()
            if part.name == "event":
                event_name = text or None
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
        elif ext in NOTE_EXTS or fname.lower() in NOTE_CANONICAL:
            kind = "notes"
        if kind is None:
            part.discard()
            continue
        if event_name is None:  # fields-before-files is our form's contract;
            event_name = default_event_name()  # foreign clients get a default
        target_dir = event_dir_of(event_name)  # raises UploadBlocked → veto
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / fname
        part_path = dest.with_name(dest.name + ".part")
        part.save_to(part_path)
        os.replace(part_path, dest)
        (saved_audio if kind == "audio" else saved_notes).append(fname)
    event = sanitize_event_name(event_name)
    if event is None:
        return 400, {"error": "事件名无效（仅中文/字母/数字/连字符）"}
    event_dir = event_dir_of(event)
    if not saved_audio and not saved_notes and not any(event_dir.iterdir()):
        return 400, {"error": "没有可保存的音频或笔记文件"}
    if saved_notes:
        rebuild_notes_md(event_dir)
    if output_dir:
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / ".mst-output.json").write_text(
            json.dumps({"output_dir": output_dir}, ensure_ascii=False), encoding="utf-8")
    return 201, {"event": event, "audio": saved_audio, "notes": saved_notes}


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
        if url.path == "/api/outputs":
            return self._json(200, {"outputs": aggregate_status()["outputs"]})
        if url.path.startswith("/api/outputs/"):
            return self._outputs_route(url)
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
    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/upload":
            return self._upload(length)
        if url.path == "/api/start":
            code, msg = start_pipeline()
            return self._json(code, {"ok": code == 202, "message": msg})
        if url.path == "/api/stop":
            code, msg = stop_pipeline()
            return self._json(code, {"ok": code == 202, "message": msg})
        if url.path == "/api/pick-folder":
            return self._pick_folder()
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
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{server.server_address[1]}/")
    return server


def serve(port: int | None = None, open_browser: bool = False) -> None:
    server = create_server(port, open_browser)
    print(f"[webapp] http://127.0.0.1:{server.server_address[1]}/ （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Meeting Summary Tool web console")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    serve(args.port, not args.no_browser)
