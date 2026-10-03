"""Web console control-plane tests (docs/web-console-design.md §3.2/§4 +
screen-recording spec §3.5).

Covers: streaming multipart parsing (binary-safe), name/path sanitization,
notes merging, queue scanning (incl. video counts + awaiting_region), status
aggregation from fixture runs/, supervision (single pipeline, stop semantics,
auto-restart drain rule), an end-to-end HTTP upload, the region endpoints
(detect cache/timeout/rc=1, preview serving, confirm clamp/auto-adopt), the
/api/start awaiting_region 409 gate, and the 14-station contract passthrough.

Runs green under plain system python3: cv2/numpy are NEVER imported here —
the real-detector happy path shells out to the T1 fixture generator under
vendor/tools-venv and skips when that venv (or ffmpeg) is absent.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import quote

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "webapp"))
if str(WS) not in sys.path:  # run_meeting / config imports (contract test)
    sys.path.insert(0, str(WS))

import server as webapp  # noqa: E402

try:  # STAGE_ORDER single source of truth (spec §3.4: 14 stations)
    import run_meeting  # noqa: E402
    HAS_RUN_MEETING = True
except (SystemExit, ImportError):  # fresh clone without config.json
    run_meeting = None
    HAS_RUN_MEETING = False

try:  # VIDEO_EXTENSIONS single source of truth (mirror-drift pin, spec §3.7)
    sys.path.insert(0, str(WS / "core" / "scripts"))
    import meeting_pipeline  # noqa: E402
    HAS_MEETING_PIPELINE = True
except (SystemExit, ImportError):
    meeting_pipeline = None
    HAS_MEETING_PIPELINE = False

TOOLS_PY = WS / "vendor" / "tools-venv" / "bin" / "python"
REAL_DETECT = TOOLS_PY.is_file() and shutil.which("ffmpeg") is not None

# Stub detector planted into the patched ROOT: writes the preview dotfile and
# a reliable T2-shaped result JSON — exercises the real subprocess plumbing
# (cache, routes, confirm adoption) without tools-venv/cv2.
STUB_DETECT_SCRIPT = (
    "import json, sys\n"
    "a = sys.argv[1:]\n"
    "prev = a[a.index('--preview-out') + 1]\n"
    "out = a[a.index('--result-out') + 1]\n"
    "open(prev, 'wb').write(b'\\x89PNG fake preview')\n"
    "json.dump({'rect': {'x': 0.1, 'y': 0.05, 'w': 0.66, 'h': 0.82},\n"
    "           'confidence': 0.81, 'reliable': True,\n"
    "           'frame_count': 20}, open(out, 'w'))\n"
)


def config_env(tools_python) -> dict:
    """Env for config.resolve(): region detect consumes ONLY tools_python —
    the other required keys get placeholders so tests stay hermetic."""
    return {"MST_WHISPER_BIN": sys.executable, "MST_WHISPER_MODEL": "dummy",
            "MST_QWEN_PYTHON": sys.executable,
            "MST_TOOLS_PYTHON": str(tools_python)}


def make_fixture_video(dest: Path, duration_s: float = 8.0) -> dict:
    """Generate the T1 synthetic meeting video in a tools-venv SUBPROCESS
    (keeps this module cv2-free). Same pinned params as tests/
    test_detect_slide_region.py → deterministic detector output."""
    code = ("import json, sys;"
            f"sys.path.insert(0, {str(WS / 'tests')!r});"
            "import synth_video;"
            f"meta = synth_video.generate_meeting_video({str(dest)!r},"
            f" duration_s={duration_s}, fps=5, size=(640, 400));"
            "print(json.dumps({'ppt_rect_px': meta['ppt_rect_px'],"
            " 'size': meta['size']}))")
    proc = subprocess.run([str(TOOLS_PY), "-B", "-c", code],
                          capture_output=True,
                          text=True, timeout=300, check=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def rect_iou(rect: dict, other) -> float:
    ax, ay, aw, ah = rect["x"], rect["y"], rect["w"], rect["h"]
    bx, by, bw, bh = other
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    union = aw * ah + bw * bh - ix * iy
    return ix * iy / union if union > 0 else 0.0


def serve_console(testcase: unittest.TestCase) -> str:
    """Ephemeral threaded ConsoleHandler on a random port (house pattern)."""
    import threading
    httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.ConsoleHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    testcase.addCleanup(httpd.server_close)
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def post_json(base: str, path: str, payload: dict | None = None, timeout: int = 10):
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get_url(base: str, path: str, timeout: int = 5):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def make_boundary(body: bytes) -> bytes:
    """Pick a boundary that does not occur in the body."""
    b = b"boundaryXYZ"
    while b in body:
        b += b"Z"
    return b


def build_multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]],
                    boundary: bytes) -> bytes:
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(b"--" + boundary + b"\r\n")
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(value.encode("utf-8") + b"\r\n")
    for name, fname, payload in files:
        out.write(b"--" + boundary + b"\r\n")
        out.write(f'Content-Disposition: form-data; name="{name}"; '
                  f'filename="{fname}"\r\n'.encode())
        out.write(b"Content-Type: application/octet-stream\r\n\r\n")
        out.write(payload + b"\r\n")
    out.write(b"--" + boundary + b"--\r\n")
    return out.getvalue()


class PatchedWorkspace:
    """Redirect every module-level path constant into a temp workspace."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        paths = {
            "ROOT": self.root,
            "INPUT_DIR": self.root / "input",
            "RUNS_DIR": self.root / "runs",
            "OUTPUTS_DIR": self.root / "outputs",
            "PROGRESS_JSONL": self.root / "runs" / "progress.jsonl",
            "WEBAPP_STATE_DIR": self.root / "runs" / ".webapp",
            "PID_FILE": self.root / "runs" / ".webapp" / "pipeline.pid",
            "PIPELINE_LOG": self.root / "runs" / ".webapp" / "pipeline.log",
        }
        self._cm = mock.patch.multiple(webapp, **paths)
        self._cm.__enter__()
        for d in ("input", "runs", "outputs"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._tmp.cleanup()
        return False


class MultipartParserTest(unittest.TestCase):
    def parse(self, fields, files):
        import tempfile
        body = build_multipart(fields, files, b"bnd42")
        stream = io.BytesIO(body)
        mp = webapp.MultipartParser(stream, "bnd42", len(body))
        got_fields, got_files = {}, {}
        for part in mp.parts():
            if part.filename is None:
                got_fields[part.name] = part.read_text()
            else:
                tmp = Path(tempfile.mkstemp()[1])
                try:
                    n = part.save_to(tmp)
                    got_files[part.filename] = (tmp.read_bytes(), n)
                finally:
                    tmp.unlink(missing_ok=True)
        return got_fields, got_files

    def test_fields_and_binary_file(self):
        binary = bytes(range(256)) * 300 + b"\r\n--bnd4" + b"\x00\xff\r\njunk"
        fields, files = self.parse({"event": "讲座A", "output_dir": ""},
                                   [("audio", "talk.m4a", binary)])
        self.assertEqual(fields["event"], "讲座A")
        self.assertEqual(files["talk.m4a"][0], binary)
        self.assertEqual(files["talk.m4a"][1], len(binary))

    def test_no_newline_large_payload(self):
        binary = b"\xa5" * 300_000  # single "line": must stream, not blow up
        _, files = self.parse({}, [("audio", "big.wav", binary)])
        self.assertEqual(files["big.wav"][0], binary)

    def test_empty_file_payload(self):
        _, files = self.parse({}, [("notes", "empty.md", b"")])
        self.assertEqual(files["empty.md"][0], b"")

    def test_malformed_yields_nothing(self):
        mp = webapp.MultipartParser(io.BytesIO(b"garbage-no-boundary"), "bnd42", 20)
        self.assertEqual(list(mp.parts()), [])


class SanitizeTest(unittest.TestCase):
    def test_event_names(self):
        self.assertEqual(webapp.sanitize_event_name(" 讲座-9月 "), "讲座-9月")
        self.assertEqual(webapp.sanitize_event_name("meeting_2026"), "meeting_2026")
        self.assertIsNone(webapp.sanitize_event_name("../etc"))
        self.assertIsNone(webapp.sanitize_event_name("a/b"))
        self.assertIsNone(webapp.sanitize_event_name(".hidden"))
        self.assertIsNone(webapp.sanitize_event_name(""))
        self.assertIsNone(webapp.sanitize_event_name("a b"))

    def test_filenames(self):
        self.assertEqual(webapp.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(webapp.sanitize_filename("talk final.m4a"), "talk final.m4a")
        self.assertIsNone(webapp.sanitize_filename(".DS_Store"))
        self.assertIsNone(webapp.sanitize_filename(""))

    def test_safe_join(self):
        base = Path(tempfile.mkdtemp())
        self.assertIsNotNone(webapp.safe_join(base, "ok/file.md"))
        self.assertIsNone(webapp.safe_join(base, "../escape.md"))


class NotesMergeTest(unittest.TestCase):
    def test_merge_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "lecture.md").write_text("第一场要点", encoding="utf-8")
            (d / "note.md").write_text("第二场备忘", encoding="utf-8")
            webapp.rebuild_notes_md(d)
            merged = (d / "notes.md").read_text(encoding="utf-8")
            self.assertIn("第一场要点", merged)
            self.assertIn("第二场备忘", merged)
            self.assertIn("来源: lecture.md", merged)
            # note.md must no longer collide with the canonical name
            self.assertFalse((d / "note.md").exists())
            self.assertTrue((d / "note.md.orig").exists())

    def test_single_canonical_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "notes.md").write_text("原始笔记", encoding="utf-8")
            webapp.rebuild_notes_md(d)
            self.assertEqual((d / "notes.md").read_text(encoding="utf-8"), "原始笔记")


class ScanQueueTest(unittest.TestCase):
    def test_events_and_loose_misc(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "讲座一"
            ev.mkdir()
            (ev / "a.m4a").write_bytes(b"x")
            (ev / "b.mp3").write_bytes(b"x")
            (ev / "notes.md").write_text("n", encoding="utf-8")
            (pw.root / "input" / "loose.wav").write_bytes(b"x")
            (pw.root / "input" / "loose2.aac").write_bytes(b"x")
            queue = webapp.scan_queue()
            names = [q["name"] for q in queue]
            self.assertEqual(names, ["讲座一", "misc"])  # events sorted, misc last
            misc = queue[1]
            self.assertEqual((misc["audio"], misc["notes"]), (2, 0))
            evq = queue[0]
            self.assertEqual((evq["audio"], evq["notes"], evq["files"]), (2, 1, 3))


class AggregateStatusTest(unittest.TestCase):
    def test_counters_and_totals(self):
        with PatchedWorkspace() as pw:
            run = pw.root / "runs" / "ev1"
            (run / "prepared/artifacts/audio/enhanced").mkdir(parents=True)
            (run / "prepared/artifacts/audio/enhanced/a.wav").write_bytes(b"x")
            (run / "prepared/artifacts/audio/enhanced/b.wav").write_bytes(b"x")
            (run / "asr_windows/flat").mkdir(parents=True)
            for i in range(10):
                (run / "asr_windows/flat" / f"w{i}.wav").write_bytes(b"x")
            (run / "asr_primary").mkdir()
            items = {"items": [{"audio": f"w{i}.wav"} for i in range(5)]}
            (run / "asr_primary/qwen3_asr_candidates.json").write_text(
                json.dumps(items), encoding="utf-8")
            for i in range(2):
                (run / f"whisper_t{i}.json").write_text("{}", encoding="utf-8")
            (run / "manifest.json").write_text(json.dumps(
                {"file_count": 3, "audio_duration_seconds": 7200,
                 "counts": {"audio": 2}}), encoding="utf-8")
            rows = [
                {"ts": 1, "event": "ev1", "kind": "event_start"},
                {"ts": 2, "event": "ev1", "kind": "stage", "stage": "asr.qwen"},
            ]
            with open(webapp.PROGRESS_JSONL, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            (run / ".progress.json").write_text(json.dumps(
                {"event": "ev1", "status": "running", "stage": "asr.qwen",
                 "stage_index": 5, "stage_total": 14, "stage_started": 2.0,
                 "message": "qwen", "updated": 3.0}), encoding="utf-8")
            # no pid alive: running snapshot must be reconciled to stopped
            status = webapp.aggregate_status()
            self.assertFalse(status["running"])
            self.assertEqual(status["current"]["status"], "stopped")
            self.assertEqual(status["totals"]["events_done"], 0)
            # now pretend the event finished: done counters come from manifest
            with open(webapp.PROGRESS_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": 4, "event": "ev1", "kind": "event_done"}) + "\n")
            status = webapp.aggregate_status()
            self.assertEqual(status["totals"]["events_done"], 1)
            self.assertEqual(status["totals"]["files_done"], 3)
            self.assertEqual(status["totals"]["audio_hours"], 2.0)

    def test_substage_counters(self):
        with PatchedWorkspace() as pw:
            run = pw.root / "runs" / "ev"
            (run / "asr_windows/flat").mkdir(parents=True)
            for i in range(8):
                (run / "asr_windows/flat" / f"w{i}.wav").write_bytes(b"x")
            c = webapp.substage_counters("ev", "asr.segment")
            self.assertEqual(c, {"done": 8})
            c = webapp.substage_counters("ev", "reconcile")
            self.assertEqual(c, {"done": 0})
            self.assertIsNone(webapp.substage_counters("ev", "literal"))
            # video_ingest: slide count surfaces once runs/<ev>/slides/ exists
            # (spec §3.4「已识别 n 页」); before that there is nothing to count
            self.assertIsNone(webapp.substage_counters("ev", "video_ingest"))
            slides = run / "slides"
            slides.mkdir()
            for i in range(1, 4):
                (slides / f"P{i}.png").write_bytes(b"x")
            (slides / "ocr.jsonl").write_text("", encoding="utf-8")
            (slides / "slides.json").write_text("{}", encoding="utf-8")
            self.assertEqual(webapp.substage_counters("ev", "video_ingest"),
                             {"done": 3})
            self.assertEqual(webapp.substage_counters("ev", "video_ingest.ocr"),
                             {"done": 3})
            self.assertEqual(webapp.substage_counters("ev", "video_ingest.frames"),
                             {"done": 3})


class SupervisionTest(unittest.TestCase):
    def _cmd(self, code_or_sleep):
        if isinstance(code_or_sleep, str):
            return [sys.executable, "-c", code_or_sleep]
        return [sys.executable, "-c", f"import time; time.sleep({code_or_sleep})"]

    def test_start_stop_and_single_flight(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "ev").mkdir()
            (pw.root / "input" / "ev" / "a.wav").write_bytes(b"x")
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(self._cmd(60))}):
                code, msg = webapp.start_pipeline()
                self.assertEqual(code, 202)
                pid = webapp.running_pid()
                self.assertIsNotNone(pid)
                code2, _ = webapp.start_pipeline()
                self.assertEqual(code2, 409)  # iron rule: never two pipelines
                code3, msg3 = webapp.stop_pipeline()
                self.assertEqual(code3, 202)
                deadline = time.time() + 5
                while webapp.running_pid() is not None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(webapp.running_pid())
                # stopped mid-run must NOT auto-restart despite queued input
                time.sleep(0.5)
                self.assertIsNone(webapp.running_pid())

    def test_stop_does_not_stamp_finished_event(self):
        """spawn-gap guard: during the gap before the pipeline emits the new
        event_start, current_event_state names the PREVIOUS done event —
        stopping then must not rewrite its final state (smoke found this)."""
        with PatchedWorkspace() as pw:
            run = pw.root / "runs" / "ev-done"
            run.mkdir(parents=True)
            (run / ".progress.json").write_text(json.dumps(
                {"event": "ev-done", "status": "done", "stage": "validate",
                 "stage_index": 14, "stage_total": 14, "stage_started": 1.0,
                 "message": "事件完成", "updated": 2.0}), encoding="utf-8")
            with open(webapp.PROGRESS_JSONL, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": 1.0, "event": "ev-done",
                                    "kind": "event_start"}) + "\n")
                f.write(json.dumps({"ts": 2.0, "event": "ev-done",
                                    "kind": "event_done"}) + "\n")
            (pw.root / "input" / "ev-next").mkdir()
            (pw.root / "input" / "ev-next" / "a.wav").write_bytes(b"x")
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(self._cmd(60))}):
                webapp.start_pipeline()
                try:
                    code, _ = webapp.stop_pipeline()
                    self.assertEqual(code, 202)
                finally:
                    webapp._supervision["stop_requested"] = True
                    webapp.stop_pipeline()
                snap = json.loads((run / ".progress.json").read_text(encoding="utf-8"))
                self.assertEqual(snap["status"], "done")  # NOT overwritten
                rows = webapp.tail_jsonl()
                self.assertFalse(any(r.get("kind") == "event_stopped"
                                     and r.get("event") == "ev-done" for r in rows))

    def test_stop_stamps_inflight_event(self):
        """positive counterpart: a genuinely in-flight event (raw snapshot
        says running) must be stamped stopped with a timeline row."""
        with PatchedWorkspace() as pw:
            run = pw.root / "runs" / "ev-live"
            run.mkdir(parents=True)
            (run / ".progress.json").write_text(json.dumps(
                {"event": "ev-live", "status": "running", "stage": "asr.qwen",
                 "stage_index": 5, "stage_total": 14, "stage_started": 1.0,
                 "message": "m", "updated": 2.0}), encoding="utf-8")
            with open(webapp.PROGRESS_JSONL, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": 1.0, "event": "ev-live",
                                    "kind": "event_start"}) + "\n")
            (pw.root / "input" / "ev-live").mkdir()
            (pw.root / "input" / "ev-live" / "a.wav").write_bytes(b"x")
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(self._cmd(60))}):
                webapp.start_pipeline()
                try:
                    code, _ = webapp.stop_pipeline()
                    self.assertEqual(code, 202)
                finally:
                    webapp._supervision["stop_requested"] = True
                    webapp.stop_pipeline()
                snap = json.loads((run / ".progress.json").read_text(encoding="utf-8"))
                self.assertEqual(snap["status"], "stopped")
                self.assertEqual(snap["stage"], "asr.qwen")
                rows = webapp.tail_jsonl()
                self.assertTrue(any(r.get("kind") == "event_stopped"
                                    and r.get("event") == "ev-live" for r in rows))

    def test_clean_exit_drains_queue_failed_exit_does_not(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "ev").mkdir()
            (pw.root / "input" / "ev" / "a.wav").write_bytes(b"x")
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(self._cmd("raise SystemExit(0)"))}):
                webapp.start_pipeline()
                deadline = time.time() + 10
                while (webapp._supervision["auto_restarts"] < 1
                       and time.time() < deadline):
                    time.sleep(0.05)
                self.assertGreaterEqual(webapp._supervision["auto_restarts"], 1)
                webapp._supervision["stop_requested"] = True
                webapp.stop_pipeline()
                webapp._supervision["auto_restarts"] = 0
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(self._cmd("raise SystemExit(1)"))}):
                code, _ = webapp.start_pipeline()
                self.assertEqual(code, 202)
                deadline = time.time() + 5
                while webapp.running_pid() is not None and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(1.0)  # no auto-restart window
                self.assertIsNone(webapp.running_pid())
                self.assertEqual(webapp._supervision["auto_restarts"], 0)


class UploadFlowTest(unittest.TestCase):
    def _server(self):
        httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.ConsoleHandler)
        httpd.daemon_threads = True
        import threading
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_upload_end_to_end(self):
        with PatchedWorkspace() as pw:
            base = self._server()
            audio = b"\x00\x01" * 5000
            body = build_multipart(
                {"event": "网页测试事件", "output_dir": str(pw.root / "custom-out")},
                [("audio", "talk 1.m4a", audio),
                 ("audio", "../evil.wav", audio),
                 ("notes", "a.md", "要点A" .encode("utf-8")),
                 ("notes", "b.md", "要点B".encode("utf-8")),
                 ("notes", "trash.exe", b"MZ")],
                b"bnd7")
            req = urllib.request.Request(
                base + "/api/upload", data=body, method="POST",
                headers={"Content-Type": "multipart/form-data; boundary=bnd7"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 201)
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["event"], "网页测试事件")
            evdir = pw.root / "input" / "网页测试事件"
            self.assertTrue((evdir / "talk 1.m4a").read_bytes() == audio)
            self.assertTrue((evdir / "evil.wav").read_bytes() == audio)  # path stripped
            merged = (evdir / "notes.md").read_text(encoding="utf-8")
            self.assertIn("要点A", merged)
            self.assertIn("要点B", merged)
            self.assertFalse((evdir / "trash.exe").exists())  # rejected ext
            meta = json.loads((evdir / ".mst-output.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["output_dir"], str(pw.root / "custom-out"))
            # status reflects the queue
            with urllib.request.urlopen(base + "/api/status", timeout=5) as resp:
                status = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(status["queue"][0]["name"], "网页测试事件")
            # 2 audio (talk 1.m4a + sanitized evil.wav) + 3 note-kind (a.md,
            # b.md, merged notes.md)
            self.assertEqual(status["queue"][0]["files"], 5)
            self.assertEqual(status["totals"]["files_pending"], 5)

    def test_upload_blocked_while_processing(self):
        with PatchedWorkspace() as pw:
            evdir = pw.root / "input" / "ev"
            evdir.mkdir()
            (evdir / "a.wav").write_bytes(b"x")
            # fabricate the running state the block rule reads
            run = pw.root / "runs" / "ev"
            run.mkdir(parents=True)
            (run / ".progress.json").write_text(json.dumps(
                {"event": "ev", "status": "running", "stage": "asr.qwen",
                 "stage_index": 5, "stage_total": 14, "stage_started": 1.0,
                 "message": "m", "updated": time.time()}), encoding="utf-8")
            with open(webapp.PROGRESS_JSONL, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "event": "ev",
                                    "kind": "event_start"}) + "\n")
            with mock.patch.dict(os.environ,
                                 {"MST_PIPELINE_CMD": json.dumps(
                                     [sys.executable, "-c", "import time; time.sleep(60)"])}):
                webapp.start_pipeline()
                try:
                    self.assertTrue(webapp.upload_is_blocked("ev"))
                    self.assertFalse(webapp.upload_is_blocked("other"))
                    body = build_multipart({"event": "ev"}, [("audio", "x.wav", b"zz")], b"bnd9")

                    def event_dir_of(name):
                        if webapp.upload_is_blocked(name):
                            raise webapp.UploadBlocked(name)
                        return pw.root / "input" / name

                    parser = webapp.MultipartParser(io.BytesIO(body), "bnd9", len(body))
                    with self.assertRaises(webapp.UploadBlocked):
                        webapp.handle_upload(parser, event_dir_of)
                finally:
                    webapp.stop_pipeline()
                # nothing landed
                self.assertEqual(sorted(p.name for p in evdir.iterdir()), ["a.wav"])


class PickFolderRouteTest(unittest.TestCase):
    """POST /api/pick-folder: native chooser for the output-dir field."""

    def _server(self):
        httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.ConsoleHandler)
        httpd.daemon_threads = True
        import threading
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _post(self, base):
        req = urllib.request.Request(base + "/api/pick-folder", data=b"",
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_success_strips_trailing_slash(self):
        base = self._server()
        fake = mock.Mock(returncode=0,
                         stdout="/Users/example/会议存档/\n", stderr="")
        with mock.patch.object(webapp.platform, "system",
                               return_value="Darwin"), \
             mock.patch.object(webapp.subprocess, "run", return_value=fake) as run:
            code, data = self._post(base)
        self.assertEqual(code, 200)
        self.assertEqual(data, {"path": "/Users/example/会议存档"})
        self.assertEqual(run.call_args[0][0][0], "osascript")

    def test_user_cancel_is_not_an_error(self):
        base = self._server()
        fake = mock.Mock(returncode=1, stdout="", stderr="User canceled")
        with mock.patch.object(webapp.platform, "system",
                               return_value="Darwin"), \
             mock.patch.object(webapp.subprocess, "run", return_value=fake):
            code, data = self._post(base)
        self.assertEqual(code, 200)
        self.assertEqual(data, {"cancelled": True})

    def test_non_mac_returns_501_with_hint(self):
        base = self._server()
        with mock.patch.object(webapp.platform, "system",
                               return_value="Linux"):
            code, data = self._post(base)
        self.assertEqual(code, 501)
        self.assertIn("手动输入", data["error"])


if __name__ == "__main__":
    unittest.main()


class ShutdownEndpointTest(unittest.TestCase):
    """POST /api/shutdown：CSRF 头校验、编译中 409、force 停止后退出。"""

    def _post(self, base, query="", header=True):
        req = urllib.request.Request(
            base + "/api/shutdown" + query, data=b"", method="POST",
            headers={"X-MST-Shutdown": "yes"} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _server(self):
        import threading
        httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.ConsoleHandler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def setUp(self):
        webapp._shutting_down = False
        self.addCleanup(setattr, webapp, "_shutting_down", False)
        # 测试进程不能真的收到 SIGINT：把延迟自中断换成 no-op
        patcher = mock.patch.object(webapp, "_delayed_server_shutdown", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_header_rejected(self):
        base = self._server()
        code, body = self._post(base, header=False)
        self.assertEqual(code, 400)
        self.assertIn("header", body["error"])

    def test_idle_shutdown_ok(self):
        with PatchedWorkspace():
            base = self._server()
            code, body = self._post(base)
            self.assertEqual(code, 200)
            self.assertEqual(body["status"], "shutting_down")
            self.assertTrue(webapp._shutting_down)

    def test_running_without_force_conflicts(self):
        with PatchedWorkspace(), \
             mock.patch.object(webapp, "running_pid", return_value=4242):
            base = self._server()
            code, body = self._post(base)
            self.assertEqual(code, 409)
            self.assertEqual(body["status"], "running")
            self.assertFalse(webapp._shutting_down)

    def test_force_stops_pipeline_then_shutdown(self):
        stopped = []
        with PatchedWorkspace(), \
             mock.patch.object(webapp, "running_pid", return_value=4242), \
             mock.patch.object(webapp, "stop_pipeline",
                               side_effect=lambda: stopped.append(1) or (202, "stopped")):
            base = self._server()
            code, body = self._post(base, query="?force=1")
            self.assertEqual(code, 200)
            self.assertEqual(body["status"], "shutting_down")
            self.assertEqual(len(stopped), 1)
            self.assertTrue(webapp._shutting_down)


# --------------------------------------------------------------------------
# Screen-recording region surface (spec §3.5) — queue, upload, endpoints, gate
# --------------------------------------------------------------------------

class ScanQueueVideoTest(unittest.TestCase):
    def test_video_counts_awaiting_and_dotfiles_ignored(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "讲座"
            ev.mkdir(parents=True)
            (ev / "meeting.mov").write_bytes(b"x")
            (ev / "talk.m4a").write_bytes(b"x")
            (ev / "notes.md").write_text("n", encoding="utf-8")
            (ev / ".region-preview.png").write_bytes(b"png")
            (ev / ".region-detect.json").write_text("{}", encoding="utf-8")
            queue = webapp.scan_queue()
            self.assertEqual(len(queue), 1)
            q = queue[0]
            self.assertEqual((q["audio"], q["notes"], q["video"]), (1, 1, 1))
            self.assertEqual(q["files"], 3)  # dotfiles never count
            self.assertTrue(q["awaiting_region"])
            # region.json clears the gate but is a config, not a source file
            (ev / "region.json").write_text("{}", encoding="utf-8")
            q = webapp.scan_queue()[0]
            self.assertFalse(q["awaiting_region"])
            self.assertEqual(q["files"], 3)

    def test_video_only_event_and_loose_video_misc(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "仅录屏").mkdir()
            (pw.root / "input" / "仅录屏" / "a.mp4").write_bytes(b"x")
            (pw.root / "input" / "loose.mkv").write_bytes(b"x")
            (pw.root / "input" / "loose.m4v").write_bytes(b"x")
            queue = webapp.scan_queue()
            self.assertEqual([q["name"] for q in queue], ["仅录屏", "misc"])
            self.assertEqual(queue[0]["video"], 1)
            self.assertTrue(queue[0]["awaiting_region"])
            self.assertEqual(queue[1]["video"], 2)
            self.assertTrue(queue[1]["awaiting_region"])
            self.assertEqual(queue[1]["files"], 2)

    def test_audio_only_event_not_awaiting(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "纯音频").mkdir()
            (pw.root / "input" / "纯音频" / "a.wav").write_bytes(b"x")
            q = webapp.scan_queue()[0]
            self.assertEqual(q["video"], 0)
            self.assertFalse(q["awaiting_region"])


class UploadVideoWhitelistTest(unittest.TestCase):
    def _upload(self, root, files, boundary):
        body = build_multipart({"event": "vid"}, files, boundary)
        parser = webapp.MultipartParser(io.BytesIO(body), boundary.decode(),
                                        len(body))
        return webapp.handle_upload(parser, lambda n: root / "input" / n)

    def test_mp4_mov_accepted_unknown_ext_rejected(self):
        with PatchedWorkspace() as pw:
            code, payload = self._upload(pw.root, [
                ("video", "rec.mp4", b"mp4data"),
                ("video", "rec2.mov", b"movdata"),
                ("video", "old.avi", b"avidata"),
                ("video", "script.py", b"import os"),
                ("notes", "x.exe", b"MZ")], b"bndV")
            self.assertEqual(code, 201)
            self.assertEqual(sorted(payload["video"]), ["rec.mp4", "rec2.mov"])
            evdir = pw.root / "input" / "vid"
            self.assertEqual((evdir / "rec.mp4").read_bytes(), b"mp4data")
            self.assertEqual((evdir / "rec2.mov").read_bytes(), b"movdata")
            for rejected in ("old.avi", "script.py", "x.exe"):
                self.assertFalse((evdir / rejected).exists())
            # the uploaded video puts the event into awaiting_region
            self.assertTrue(webapp.scan_queue()[0]["awaiting_region"])

    def test_all_five_whitelist_extensions(self):
        with PatchedWorkspace() as pw:
            # mirror of meeting_pipeline.VIDEO_EXTENSIONS (single source, §3.7)
            self.assertEqual(sorted(webapp.VIDEO_EXTS),
                             [".m4v", ".mkv", ".mov", ".mp4", ".webm"])
            files = [("video", f"v{i}{e}", b"x")
                     for i, e in enumerate(sorted(webapp.VIDEO_EXTS))]
            code, payload = self._upload(pw.root, files, b"bndW")
            self.assertEqual(code, 201)
            self.assertEqual(len(payload["video"]), 5)

    @unittest.skipUnless(HAS_MEETING_PIPELINE, "meeting_pipeline 不可导入")
    def test_mirror_of_single_source_of_truth(self):
        # T-d pin: server's mirror must never drift from the §3.7 source
        self.assertEqual(webapp.VIDEO_EXTS,
                         meeting_pipeline.VIDEO_EXTENSIONS)


class RegionDetectErrorBranchTest(unittest.TestCase):
    """Fast fake-path branches: a stub detector planted into the patched ROOT
    exercises the real subprocess plumbing without tools-venv/cv2."""

    def _event_with_video(self, pw, name="ev", videos=("a.mp4",)):
        d = pw.root / "input" / name
        d.mkdir(parents=True)
        for v in videos:
            (d / v).write_bytes(b"not a real video")
        return d

    def _plant_script(self, pw, source: str) -> Path:
        script = pw.root / "core" / "scripts" / "detect_slide_region.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(source, encoding="utf-8")
        return script

    def test_invalid_event_400_and_missing_event_404(self):
        with PatchedWorkspace():
            code, payload = webapp.region_detect("../evil")
            self.assertEqual(code, 400)
            self.assertIn("事件名", payload["error"])
            code, _ = webapp.region_detect("不存在")
            self.assertEqual(code, 404)

    def test_event_without_video_400(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "ev").mkdir(parents=True)
            (pw.root / "input" / "ev" / "a.m4a").write_bytes(b"x")
            code, payload = webapp.region_detect("ev")
            self.assertEqual(code, 400)
            self.assertIn("视频", payload["error"])

    def test_rc1_hard_failure_friendly_500(self):
        with PatchedWorkspace() as pw:
            ev = self._event_with_video(pw)
            self._plant_script(pw, "import sys\n"
                                   "sys.stderr.write('boom: decode failed')\n"
                                   "sys.exit(1)\n")
            with mock.patch.dict(os.environ, config_env(sys.executable)):
                code, payload = webapp.region_detect("ev")
            self.assertEqual(code, 500)
            self.assertIn("检测失败", payload["error"])
            self.assertIn("boom", payload["detail"])
            # rc=1 writes NO result JSON (T2 contract) — no cache left behind
            self.assertFalse((ev / ".region-detect.json").exists())
            self.assertFalse((ev / ".region-preview.png").exists())

    def test_timeout_504_kills_whole_process_group(self):
        with PatchedWorkspace() as pw:
            ev = self._event_with_video(pw)
            marker = pw.root / "group-child-survived.txt"
            # timing margins: kill fires at ~2s, the grandchild would write its
            # marker at ~2.5s, and we check at ~5.5s — a leaked child cannot
            # slip through under load (no stall-race vacuous pass)
            inner = (f"import time; time.sleep(2.5); "
                     f"open({json.dumps(str(marker))}, 'w').write('x')")
            # the stub mimics the detector's write_json_atomic: it creates
            # <result-out>.tmp BEFORE the long sleep, so the SIGKILL lands
            # mid-atomic-write and the tmp orphan is real (N1: without it the
            # cleanup assert below would pass vacuously — mutation-verified)
            self._plant_script(pw,
                               "import subprocess, sys, time\n"
                               "a = sys.argv[1:]\n"
                               "out = a[a.index('--result-out') + 1]\n"
                               "open(out + '.tmp', 'w').write('{')\n"
                               f"subprocess.Popen([sys.executable, '-c',"
                               f" {json.dumps(inner)}])\n"
                               "time.sleep(30)\n")
            with mock.patch.dict(os.environ, config_env(sys.executable)), \
                 mock.patch.object(webapp, "REGION_DETECT_TIMEOUT_S", 2.0):
                started = time.time()
                code, payload = webapp.region_detect("ev")
                elapsed = time.time() - started
            self.assertEqual(code, 504)
            self.assertIn("超时", payload["error"])
            self.assertLess(elapsed, 15)
            # the stub-created atomic-write tmp must not survive the group kill
            # (server-side unlink, server.py timeout branch)
            self.assertFalse((ev / ".region-result.part.json.tmp").exists())
            time.sleep(3.5)  # the grandchild writes this if group-kill leaked
            self.assertFalse(marker.exists())

    def test_non_dict_cache_falls_back_to_fresh_detect(self):
        """Forged/corrupt .region-detect.json (JSON string/list/number) must
        never reach cached.get — the isinstance gate falls back to a fresh
        detection run instead of dropping the connection (M-a)."""
        with PatchedWorkspace() as pw:
            ev = self._event_with_video(pw)
            self._plant_script(pw, STUB_DETECT_SCRIPT)
            (ev / ".region-preview.png").write_bytes(b"\x89PNG stale")
            for forged in ('"a rect string"', '["rect"]', '5'):
                (ev / ".region-detect.json").write_text(forged,
                                                        encoding="utf-8")
                with mock.patch.dict(os.environ, config_env(sys.executable)):
                    code, payload = webapp.region_detect("ev")
                self.assertEqual(code, 200, forged)
                self.assertFalse(payload["cached"], forged)
                cache = json.loads(
                    (ev / ".region-detect.json").read_text(encoding="utf-8"))
                self.assertIsInstance(cache, dict)  # stub replaced the forgery

    def test_broken_tools_env_friendly_500(self):
        with PatchedWorkspace() as pw:
            self._event_with_video(pw)
            self._plant_script(pw, "print('unused')\n")
            missing = pw.root / "no-such-venv" / "bin" / "python"
            with mock.patch.dict(os.environ, config_env(missing)):
                code, payload = webapp.region_detect("ev")
            self.assertEqual(code, 500)
            self.assertIn("setup.sh", payload["error"])

    def test_incomplete_config_friendly_500(self):
        import config
        with PatchedWorkspace() as pw:
            self._event_with_video(pw)
            self._plant_script(pw, "print('unused')\n")
            blank = {k: "" for k in ("MST_WHISPER_BIN", "MST_WHISPER_MODEL",
                                     "MST_QWEN_PYTHON", "MST_TOOLS_PYTHON")}
            with mock.patch.dict(os.environ, blank), \
                 mock.patch.object(config, "_PRIVATE_FILE",
                                   pw.root / "no-config.json"):
                code, payload = webapp.region_detect("ev")
            self.assertEqual(code, 500)
            self.assertIn("工具环境未就绪", payload["error"])


class RegionHttpFlowTest(unittest.TestCase):
    """HTTP wiring with a stub detector: detect → preview serving → cache hit
    → confirm(auto-adopt) → gate cleared — no tools-venv needed."""

    def test_full_confirm_flow_over_http(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "ev"
            ev.mkdir(parents=True)
            (ev / "meeting.mp4").write_bytes(b"fake but present")
            script = pw.root / "core" / "scripts" / "detect_slide_region.py"
            script.parent.mkdir(parents=True)
            script.write_text(STUB_DETECT_SCRIPT, encoding="utf-8")
            base = serve_console(self)
            self.assertTrue(webapp.scan_queue()[0]["awaiting_region"])
            with mock.patch.dict(os.environ, config_env(sys.executable)):
                code, payload = post_json(base, "/api/region/detect",
                                          {"event": "ev"})
                self.assertEqual(code, 200)
                self.assertTrue(payload["reliable"])
                self.assertFalse(payload["cached"])
                self.assertEqual(payload["rect"]["w"], 0.66)
                self.assertEqual(payload["preview_url"],
                                 "/api/events/ev/region_preview.png")
                code, ctype, body = get_url(base, payload["preview_url"])
                self.assertEqual(code, 200)
                self.assertEqual(ctype, "image/png")
                self.assertEqual(body, b"\x89PNG fake preview")
                # second call → cache hit, detector NOT re-run
                cache1 = json.loads(
                    (ev / ".region-detect.json").read_text(encoding="utf-8"))
                code, payload2 = post_json(base, "/api/region/detect",
                                           {"event": "ev"})
                self.assertTrue(payload2["cached"])
                self.assertEqual(payload2["rect"], payload["rect"])
                cache2 = json.loads(
                    (ev / ".region-detect.json").read_text(encoding="utf-8"))
                self.assertEqual(cache2["detected_ts"], cache1["detected_ts"])
            # malformed JSON body → 400
            req = urllib.request.Request(base + "/api/region/detect",
                                         data=b"{oops", method="POST",
                                         headers={"Content-Type":
                                                  "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("expected 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
            # confirm without rect → adopt the auto result (§3.5), gate clears
            code, _ = post_json(base, "/api/region/confirm", {"event": "ev"})
            self.assertEqual(code, 200)
            region = json.loads((ev / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(region["source"], "auto")
            self.assertEqual(region["confidence"], 0.81)
            self.assertEqual(region["video"], "meeting.mp4")
            self.assertFalse(webapp.scan_queue()[0]["awaiting_region"])


class RegionPreviewRouteTest(unittest.TestCase):
    def test_preview_404_then_200_with_chinese_event(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "讲座V"
            ev.mkdir(parents=True)
            base = serve_console(self)
            url = "/api/events/" + quote("讲座V") + "/region_preview.png"
            code, _ctype, _body = get_url(base, url)
            self.assertEqual(code, 404)
            (ev / ".region-preview.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
            code, ctype, body = get_url(base, url)
            self.assertEqual(code, 200)
            self.assertEqual(ctype, "image/png")
            self.assertTrue(body.startswith(b"\x89PNG"))

    def test_unknown_route_shapes_404(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "ev").mkdir(parents=True)
            base = serve_console(self)
            for path in ("/api/events/ev/region.json",
                         "/api/events/ev",
                         "/api/events//region_preview.png",
                         "/api/events/" + quote("不存在") + "/region_preview.png"):
                code, _ctype, _body = get_url(base, path)
                self.assertEqual(code, 404, path)


class RegionTraversalDefenseTest(unittest.TestCase):
    """Traversal attempts on the region surface are rejected — extends the
    SanitizeTest/upload defenses to the new endpoints (spec §3.5 safe_join)."""

    def test_preview_route_traversal_rejected(self):
        with PatchedWorkspace() as pw:
            (pw.root / "secret.png").write_bytes(b"topsecret")
            (pw.root / "input" / "ev").mkdir(parents=True)
            base = serve_console(self)
            attempts = [
                "/api/events/..%2F..%2Fsecret.png/region_preview.png",
                "/api/events/..%2F..%2F/region_preview.png",
                "/api/events/%2E%2E%2F%2E%2E/region_preview.png",
                "/api/events/../region_preview.png",
                "/api/events/..%2Fev/region_preview.png",
                "/api/events/.hidden/region_preview.png",
                "/api/events/ev/../../secret.png",
            ]
            for path in attempts:
                code, _ctype, body = get_url(base, path)
                self.assertIn(code, (400, 404), path)
                self.assertNotIn(b"topsecret", body)

    def test_detect_confirm_event_traversal_rejected(self):
        with PatchedWorkspace() as pw:
            outside = pw.root / "outside"
            outside.mkdir()
            (outside / "a.mp4").write_bytes(b"x")
            base = serve_console(self)
            rect = {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
            for evil in ("../outside", "..", "/", "/etc/passwd", "a/b",
                         ".hidden", "ev/../../outside", "%2e%2e%2foutside"):
                code, _ = post_json(base, "/api/region/detect", {"event": evil})
                self.assertIn(code, (400, 404), evil)
                code, _ = post_json(base, "/api/region/confirm",
                                    {"event": evil, "rect": rect})
                self.assertIn(code, (400, 404), evil)
            self.assertFalse((outside / "region.json").exists())
            self.assertFalse((outside / ".region-detect.json").exists())
            self.assertFalse((outside / ".region-preview.png").exists())


class RegionConfirmTest(unittest.TestCase):
    def _event(self, pw, videos=("b.mp4", "a.mov"), name="ev"):
        d = pw.root / "input" / name
        d.mkdir(parents=True)
        for v in videos:
            (d / v).write_bytes(b"x")
        return d

    def test_user_rect_clamped_and_region_schema(self):
        with PatchedWorkspace() as pw:
            d = self._event(pw)
            code, payload = webapp.region_confirm(
                "ev", {"x": -0.5, "y": 0.2, "w": 1.5, "h": 0.3})
            self.assertEqual(code, 200)
            region = json.loads((d / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(region["schema_version"], 1)
            self.assertEqual(region["source"], "user")
            self.assertEqual(region["confidence"], 1.0)
            self.assertEqual(region["video"], "a.mov")  # 首个按文件名排序 (§5)
            self.assertEqual(region["rect"],
                             {"x": 0.0, "y": 0.2, "w": 1.0, "h": 0.3})
            self.assertIsInstance(region["created_ts"], float)
            self.assertEqual(payload["region"], region)
            # numeric strings stay accepted (JS clients) — only bools rejected
            code, _ = webapp.region_confirm(
                "ev", {"x": "0.1", "y": "0.1", "w": "0.5", "h": "0.5"})
            self.assertEqual(code, 200)
            coerced = json.loads((d / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(coerced["rect"]["x"], 0.1)
            # atomic write left no partial-file dotfiles behind
            self.assertEqual(sorted(p.name for p in d.iterdir()
                                    if p.name.startswith(".")), [])

    def test_invalid_rects_400_no_region_written(self):
        with PatchedWorkspace() as pw:
            d = self._event(pw)
            bad_rects = [
                {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.5},   # zero width
                {"x": 0.1, "y": 0.1, "w": -0.2, "h": 0.5},  # clamps to 0 → reject
                {"x": 0.1, "y": 0.1, "w": 0.5},             # h missing
                {"x": "a", "y": 0.1, "w": 0.5, "h": 0.5},   # non-numeric
                {"x": True, "y": 0.1, "w": 0.5, "h": 0.5},  # bool ≠ number (M-c)
                {"x": 0.1, "y": 0.1, "w": True, "h": 0.5},
                {"x": 1.0, "y": 0.1, "w": 0.5, "h": 0.5},   # w shrinks to 0
                "not-a-dict",
                [0.1, 0.1, 0.5, 0.5],
            ]
            for bad in bad_rects:
                code, payload = webapp.region_confirm("ev", bad)
                self.assertEqual(code, 400, bad)
                self.assertIn("坐标", payload["error"])
            self.assertIsNone(webapp._clamp_rect(
                {"x": float("nan"), "y": 0.0, "w": 0.5, "h": 0.5}))
            self.assertFalse((d / "region.json").exists())

    def test_overflowing_rect_shrunk_to_downstream_valid_region(self):
        """I1 seam: per-field clamping alone could still WRITE x+w > 1 —
        run_meeting._valid_region_file and extract_video_slides.resolve_crop
        silently discard overflowing regions (auto-redetect fallback throws
        the user's confirmation away). The shrink clamp must keep every
        written region.json downstream-valid."""
        cases = [
            ({"x": 0.5, "y": 0.1, "w": 1.2, "h": 0.5},
             {"x": 0.5, "y": 0.1, "w": 0.5, "h": 0.5}),
            ({"x": 0.999, "y": 0.0, "w": 0.5, "h": 0.5},
             {"x": 0.999, "y": 0.0, "w": 0.001, "h": 0.5}),
            ({"x": 0.05, "y": 0.05, "w": 1.2, "h": 1.3},
             {"x": 0.05, "y": 0.05, "w": 0.95, "h": 0.95}),
            ({"x": -0.5, "y": 0.2, "w": 1.5, "h": 0.3},
             {"x": 0.0, "y": 0.2, "w": 1.0, "h": 0.3}),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw), PatchedWorkspace() as pw:
                d = self._event(pw, videos=("meeting.mp4",))
                code, _ = webapp.region_confirm("ev", raw)
                self.assertEqual(code, 200)
                region = json.loads(
                    (d / "region.json").read_text(encoding="utf-8"))
                rect = region["rect"]
                self.assertEqual(rect, expected)
                # direct §4.1 invariants — pin survives without run_meeting
                self.assertLessEqual(rect["x"] + rect["w"], 1.0 + 1e-9)
                self.assertLessEqual(rect["y"] + rect["h"], 1.0 + 1e-9)
                self.assertLess(rect["x"], 1.0)
                self.assertLess(rect["y"], 1.0)
                self.assertGreater(rect["w"], 0.0)
                self.assertGreater(rect["h"], 0.0)
                # downstream consumer gate (single source of truth)
                if HAS_RUN_MEETING and hasattr(run_meeting,
                                               "_valid_region_file"):
                    self.assertTrue(run_meeting._valid_region_file(
                        d / "region.json", "meeting.mp4"))

    def test_event_without_video_400(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "ev").mkdir(parents=True)
            (pw.root / "input" / "ev" / "a.m4a").write_bytes(b"x")
            code, payload = webapp.region_confirm(
                "ev", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})
            self.assertEqual(code, 400)
            self.assertIn("视频", payload["error"])

    def test_auto_adoption_requires_reliable_cache(self):
        with PatchedWorkspace() as pw:
            d = self._event(pw, videos=("meeting.mov",))
            code, payload = webapp.region_confirm("ev", None)
            self.assertEqual(code, 400)  # no cache at all
            self.assertIn("检测", payload["error"])
            (d / ".region-detect.json").write_text(json.dumps(
                {"rect": None, "confidence": 0.2, "reliable": False,
                 "frame_count": 20}), encoding="utf-8")
            code, _ = webapp.region_confirm("ev", None)
            self.assertEqual(code, 400)  # unreliable → not adoptable
            (d / ".region-detect.json").write_text(json.dumps(
                {"rect": {"x": 0.05, "y": 0.05, "w": 0.62, "h": 0.9},
                 "confidence": 0.734, "reliable": True, "frame_count": 20}),
                encoding="utf-8")
            code, _ = webapp.region_confirm("ev", None)
            self.assertEqual(code, 200)
            region = json.loads((d / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(region["source"], "auto")
            self.assertEqual(region["confidence"], 0.734)
            self.assertEqual(region["rect"]["x"], 0.05)
            self.assertEqual(region["video"], "meeting.mov")
            # dotfiles are kept after confirm (§3.5)
            self.assertTrue((d / ".region-detect.json").exists())

    def test_non_dict_cache_refuses_auto_adopt(self):
        """M-a parity with run_meeting._valid_region_file's isinstance gate:
        a forged non-dict .region-detect.json → friendly 400, never an
        AttributeError through cached.get (dropped connection)."""
        with PatchedWorkspace() as pw:
            d = self._event(pw, videos=("meeting.mov",))
            for forged in ('"a rect string"', '["rect"]', '5', 'null', 'true'):
                (d / ".region-detect.json").write_text(forged,
                                                       encoding="utf-8")
                code, payload = webapp.region_confirm("ev", None)
                self.assertEqual(code, 400, forged)
                self.assertIn("检测", payload["error"])
            self.assertFalse((d / "region.json").exists())

    def test_misc_event_maps_to_input_root(self):
        with PatchedWorkspace() as pw:
            (pw.root / "input" / "loose.mp4").write_bytes(b"x")
            self.assertEqual(webapp.event_input_dir("misc"), pw.root / "input")
            code, _ = webapp.region_confirm(
                "misc", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})
            self.assertEqual(code, 200)
            region = json.loads(
                (pw.root / "input" / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(region["video"], "loose.mp4")
            self.assertFalse(webapp.scan_queue()[0]["awaiting_region"])


class StartGateTest(unittest.TestCase):
    """/api/start whole-batch 409 gate (spec §3.5/§5 v1 simplification)."""

    def test_start_lists_awaiting_events_and_blocks_batch(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "录屏A"
            ev.mkdir(parents=True)
            (ev / "v.mp4").write_bytes(b"x")
            (pw.root / "input" / "音频B").mkdir()
            (pw.root / "input" / "音频B" / "a.wav").write_bytes(b"x")
            fake = json.dumps([sys.executable, "-c",
                               "import time; time.sleep(60)"])
            with mock.patch.dict(os.environ, {"MST_PIPELINE_CMD": fake}):
                code, msg = webapp.start_pipeline()
                self.assertEqual(code, 409)
                self.assertIn("录屏A", msg)
                self.assertNotIn("音频B", msg)
                base = serve_console(self)
                code, body = post_json(base, "/api/start")
                self.assertEqual(code, 409)
                self.assertFalse(body["ok"])
                self.assertEqual(body["awaiting_region"], ["录屏A"])
                self.assertIn("录屏A", body["message"])
                try:
                    self.assertIsNone(webapp.running_pid())  # nothing spawned
                finally:
                    if webapp.running_pid() is not None:
                        webapp._supervision["stop_requested"] = True
                        webapp.stop_pipeline()

    def test_start_proceeds_once_region_confirmed(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "录屏A"
            ev.mkdir(parents=True)
            (ev / "v.mp4").write_bytes(b"x")
            code, _ = webapp.region_confirm(
                "录屏A", {"x": 0.05, "y": 0.05, "w": 0.6, "h": 0.8})
            self.assertEqual(code, 200)
            fake = json.dumps([sys.executable, "-c",
                               "import time; time.sleep(60)"])
            with mock.patch.dict(os.environ, {"MST_PIPELINE_CMD": fake}):
                code, _ = webapp.start_pipeline()
                self.assertEqual(code, 202)
                try:
                    self.assertIsNotNone(webapp.running_pid())
                finally:
                    webapp._supervision["stop_requested"] = True
                    webapp.stop_pipeline()
                    webapp._supervision["auto_restarts"] = 0


class StatusAwaitingRegionTest(unittest.TestCase):
    def test_status_queue_exposes_video_and_awaiting(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "录屏A"
            ev.mkdir(parents=True)
            (ev / "v.mp4").write_bytes(b"x")
            (ev / "a.wav").write_bytes(b"x")
            (pw.root / "input" / "loose.webm").write_bytes(b"x")
            base = serve_console(self)
            code, _ct, body = get_url(base, "/api/status")
            self.assertEqual(code, 200)
            status = json.loads(body)
            by_name = {q["name"]: q for q in status["queue"]}
            self.assertEqual(by_name["录屏A"]["video"], 1)
            self.assertTrue(by_name["录屏A"]["awaiting_region"])
            self.assertEqual(by_name["misc"]["video"], 1)
            self.assertTrue(by_name["misc"]["awaiting_region"])
            self.assertEqual(status["totals"]["files_pending"], 3)
            # confirming the misc region (input/region.json) clears its badge
            webapp.region_confirm("misc",
                                  {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})
            code, _ct, body = get_url(base, "/api/status")
            by_name = {q["name"]: q for q in json.loads(body)["queue"]}
            self.assertFalse(by_name["misc"]["awaiting_region"])
            self.assertTrue(by_name["录屏A"]["awaiting_region"])


@unittest.skipUnless(HAS_RUN_MEETING, "run_meeting 不可导入（config 不完整）")
class StageContractTest(unittest.TestCase):
    """STAGE_ORDER 12→14 (spec §3.4). The server derives nothing itself —
    stage_total/stage_index flow through the pipeline's .progress.json
    snapshots — so the console fixtures above pin 14 and this test pins the
    producer contract the passthrough depends on."""

    def test_stage_order_is_14_with_new_stations(self):
        keys = [k for k, _ in run_meeting.STAGE_ORDER]
        labels = dict(run_meeting.STAGE_ORDER)
        self.assertEqual(len(keys), 14)
        self.assertEqual(keys[0], "inventory")
        self.assertEqual(keys[1], "video_ingest")  # 紧随 inventory (§3.4)
        self.assertEqual(labels["video_ingest"], "视频分解（音轨/幻灯片）")
        self.assertEqual(keys[keys.index("relevance") + 1], "slide_align")
        self.assertEqual(labels["slide_align"], "幻灯片对齐")

    def test_status_passthrough_stage_total_14(self):
        with PatchedWorkspace() as pw:
            run = pw.root / "runs" / "ev"
            run.mkdir(parents=True)
            (run / ".progress.json").write_text(json.dumps(
                {"event": "ev", "status": "running", "stage": "video_ingest",
                 "stage_index": 2, "stage_total": 14, "stage_started": 1.0,
                 "message": "m", "updated": 2.0}), encoding="utf-8")
            with open(webapp.PROGRESS_JSONL, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": 1, "event": "ev",
                                    "kind": "event_start"}) + "\n")
            status = webapp.aggregate_status()
            self.assertEqual(status["current"]["stage_total"], 14)
            self.assertEqual(status["current"]["stage_index"], 2)
            self.assertEqual(status["current"]["stage"], "video_ingest")


@unittest.skipUnless(REAL_DETECT, "需要 vendor/tools-venv（numpy+cv2）+ ffmpeg")
class RegionDetectRealVideoTest(unittest.TestCase):
    """Plan T7「detect 端点用 T1 小视频实跑」: the REAL detector subprocess
    against the T1 synthetic fixture (deterministic params, IoU ground truth),
    driven through the real HTTP routes. The fixture video is generated in a
    tools-venv subprocess so this module itself never imports cv2."""

    def test_detect_cache_preview_confirm_on_real_fixture(self):
        with PatchedWorkspace() as pw:
            ev = pw.root / "input" / "录屏讲座"
            ev.mkdir(parents=True)
            meta = make_fixture_video(ev / "meeting.mp4", duration_s=8.0)
            base = serve_console(self)
            with mock.patch.object(webapp, "ROOT", WS), \
                 mock.patch.dict(os.environ, config_env(TOOLS_PY)):
                code, payload = post_json(base, "/api/region/detect",
                                          {"event": "录屏讲座"}, timeout=150)
                self.assertEqual(code, 200)
                self.assertFalse(payload["cached"])
                self.assertTrue(payload["reliable"])
                width, height = meta["size"]
                gx, gy, gw, gh = meta["ppt_rect_px"]
                iou = rect_iou(payload["rect"],
                               (gx / width, gy / height,
                                gw / width, gh / height))
                self.assertGreaterEqual(iou, 0.9)
                self.assertTrue((ev / ".region-preview.png").is_file())
                cache1 = json.loads(
                    (ev / ".region-detect.json").read_text(encoding="utf-8"))
                self.assertEqual(cache1["video"], "meeting.mp4")
                self.assertEqual(cache1["reliable"], True)
                # preview served through the GET route (URL-encoded 中文事件名)
                code, ctype, body = get_url(base, payload["preview_url"],
                                            timeout=10)
                self.assertEqual(code, 200)
                self.assertEqual(ctype, "image/png")
                self.assertTrue(body.startswith(b"\x89PNG"))
                # second detect → cache hit, detector NOT re-run
                code2, payload2 = post_json(base, "/api/region/detect",
                                            {"event": "录屏讲座"}, timeout=150)
                self.assertEqual(code2, 200)
                self.assertTrue(payload2["cached"])
                self.assertEqual(payload2["rect"], payload["rect"])
                cache2 = json.loads(
                    (ev / ".region-detect.json").read_text(encoding="utf-8"))
                self.assertEqual(cache2["detected_ts"], cache1["detected_ts"])
                # a video mtime change invalidates the cache → fresh run
                st = (ev / "meeting.mp4").stat()
                os.utime(ev / "meeting.mp4",
                         (st.st_atime + 10, st.st_mtime + 10))
                code3, payload3 = post_json(base, "/api/region/detect",
                                            {"event": "录屏讲座"}, timeout=150)
                self.assertEqual(code3, 200)
                self.assertFalse(payload3["cached"])
            # user confirm with an out-of-range rect → clamped region.json §4.1
            code4, _ = post_json(base, "/api/region/confirm",
                                 {"event": "录屏讲座",
                                  "rect": {"x": -0.2, "y": 0.05,
                                           "w": 1.4, "h": 0.85}})
            self.assertEqual(code4, 200)
            region = json.loads((ev / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(region["schema_version"], 1)
            self.assertEqual(region["source"], "user")
            self.assertEqual(region["confidence"], 1.0)
            self.assertEqual(region["rect"],
                             {"x": 0.0, "y": 0.05, "w": 1.0, "h": 0.85})
            self.assertEqual(region["video"], "meeting.mp4")
            self.assertFalse(webapp.scan_queue()[0]["awaiting_region"])
