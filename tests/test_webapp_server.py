"""Web console control-plane tests (docs/web-console-design.md §3.2/§4).

Covers: streaming multipart parsing (binary-safe), name/path sanitization,
notes merging, queue scanning, status aggregation from fixture runs/,
supervision (single pipeline, stop semantics, auto-restart drain rule), and
an end-to-end HTTP upload.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "webapp"))

import server as webapp  # noqa: E402


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
                 "stage_index": 4, "stage_total": 12, "stage_started": 2.0,
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
                 "stage_index": 12, "stage_total": 12, "stage_started": 1.0,
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
                 "stage_index": 4, "stage_total": 12, "stage_started": 1.0,
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
                 "stage_index": 4, "stage_total": 12, "stage_started": 1.0,
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
                         stdout="/Users/demo/会议存档/\n", stderr="")
        with mock.patch.object(webapp.platform, "system",
                               return_value="Darwin"), \
             mock.patch.object(webapp.subprocess, "run", return_value=fake) as run:
            code, data = self._post(base)
        self.assertEqual(code, 200)
        self.assertEqual(data, {"path": "/Users/demo/会议存档"})
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
