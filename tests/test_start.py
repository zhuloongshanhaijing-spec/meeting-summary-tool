"""Unit tests for start.py — the one-command launcher.

Covers the preflight chain (config / ffmpeg / binaries / memory / Ollama),
the lazy run_meeting import, the argparse surface, and the serve path
(port-in-use → friendly hint + exit 1, Ctrl+C → clean stop). Everything is
hermetic: config resolution is pointed at temp paths, fake binaries are
touched + chmod 755 inside a TemporaryDirectory, and urlopen / Popen are
mocked — the real Ollama app is never launched and no real port is bound.
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
for _p in (str(WS), str(WS / "webapp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import start      # noqa: E402
import config     # noqa: E402
import server     # noqa: E402


def mst_cleared_env() -> dict:
    """os.environ copy with every MST_* override stripped."""
    return {k: v for k, v in os.environ.items() if not k.startswith("MST_")}


def fake_toolchain(tmp: Path) -> dict:
    """A fully valid cfg dict backed by temp files (touch + chmod 755)."""
    cfg = {}
    for name in ("whisper_bin", "qwen_python", "ffmpeg"):
        p = tmp / name
        p.write_bytes(b"#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        cfg[name] = str(p)
    model = tmp / "model.bin"
    model.write_bytes(b"ggml")          # non-empty
    cfg["whisper_model"] = str(model)
    cfg["ollama_url"] = "http://127.0.0.1:11434"
    return cfg


class ImportPurityTest(unittest.TestCase):
    def test_import_is_quiet(self):
        """模块 import 无副作用：子进程 import start 不打印、不报错。"""
        proc = subprocess.run([sys.executable, "-c", "import start"],
                              cwd=str(WS), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")


class RunChecksReadyTest(unittest.TestCase):
    def test_all_ready_no_failures(self):
        """全部就绪 → run_checks() 返回空失败列表（Ollama 假 200，不真启 app）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = fake_toolchain(Path(td))
            with mock.patch.object(config, "resolve", return_value=cfg), \
                 mock.patch("urllib.request.urlopen") as urlopen, \
                 mock.patch.object(start, "memory_info",
                                   return_value={"total_gb": 16.0,
                                                 "available_gb": 8.0}), \
                 mock.patch.object(subprocess, "Popen") as popen:
                failures = start.run_checks()
        self.assertEqual(failures, [])
        urlopen.assert_called_once()     # exactly one /api/tags probe
        popen.assert_not_called()        # alive on first probe: no `open -a Ollama`


class CheckConfigTest(unittest.TestCase):
    def test_missing_required_keys(self):
        """config 缺失必填键 → check_config() 把 SystemExit 转成失败项。"""
        with tempfile.TemporaryDirectory() as td:
            nowhere = Path(td) / "config.json"   # never created
            with mock.patch.dict(os.environ, mst_cleared_env(), clear=True), \
                 mock.patch.object(config, "_PRIVATE_FILE", nowhere):
                cfg, err = start.check_config()
        self.assertIsNone(cfg)
        self.assertIsNotNone(err)
        for token in ("whisper_bin", "MST_WHISPER_BIN",
                      "whisper_model", "qwen_python"):
            self.assertIn(token, err)


class CheckFfmpegTest(unittest.TestCase):
    def test_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = fake_toolchain(Path(td))
            self.assertIsNone(start.check_ffmpeg(cfg))      # fake binary ok
            cfg["ffmpeg"] = str(Path(td) / "nope")
            self.assertIsNotNone(start.check_ffmpeg(cfg))   # bad path fails


class CheckBinariesTest(unittest.TestCase):
    def test_missing_whisper_bin(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = fake_toolchain(Path(td))
            missing = str(Path(td) / "no-such-cli")
            cfg["whisper_bin"] = missing
            failures = start.check_binaries(cfg)
        self.assertEqual(len(failures), 1)
        self.assertIn("whisper_bin", failures[0])
        self.assertIn(missing, failures[0])

    def test_empty_model_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = fake_toolchain(tmp)
            empty = tmp / "empty.bin"
            empty.touch()                                  # zero bytes
            cfg["whisper_model"] = str(empty)
            failures = start.check_binaries(cfg)
        self.assertEqual(len(failures), 1)
        self.assertIn("whisper_model", failures[0])


class LazyRunMeetingTest(unittest.TestCase):
    def test_config_failure_never_imports_run_meeting(self):
        """run_meeting 在 import 期解析 config：config 检查没过就绝不能 import。"""
        saved = sys.modules.pop("run_meeting", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                with mock.patch.dict(os.environ, mst_cleared_env(),
                                     clear=True), \
                     mock.patch.object(config, "_PRIVATE_FILE",
                                       Path(td) / "config.json"):
                    failures = start.run_checks()
            self.assertTrue(failures)
            self.assertNotIn("run_meeting", sys.modules)
        finally:
            if saved is not None:
                sys.modules["run_meeting"] = saved

    def test_memory_info_delegates_lazily(self):
        fake = mock.MagicMock(
            check_memory=lambda: {"total_gb": 3.0, "available_gb": 3.0})
        with mock.patch.dict(sys.modules, {"run_meeting": fake}):
            self.assertEqual(start.memory_info(),
                             {"total_gb": 3.0, "available_gb": 3.0})


class WaitOllamaTest(unittest.TestCase):
    def test_timeout_returns_false_fast(self):
        """urlopen 恒抛 + Popen no-op + timeout=0.1 → False 且总耗时 < 2s。"""
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("connection refused")), \
             mock.patch.object(subprocess, "Popen") as popen:
            t0 = time.monotonic()
            got = start.wait_ollama("http://127.0.0.1:11434", timeout=0.1)
            elapsed = time.monotonic() - t0
        self.assertFalse(got)
        self.assertLess(elapsed, 2.0)
        popen.assert_called_once_with(
            ["open", "-a", "Ollama"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ParseArgsTest(unittest.TestCase):
    def test_port_and_no_browser(self):
        args = start.parse_args(["--port", "8790", "--no-browser"])
        self.assertEqual(args.port, 8790)
        self.assertTrue(args.no_browser)

    def test_defaults(self):
        args = start.parse_args([])
        self.assertIsNone(args.port)
        self.assertFalse(args.no_browser)


class ServeWebTest(unittest.TestCase):
    def test_port_in_use_returns_1(self):
        boom = OSError(errno.EADDRINUSE, "address already in use")
        with mock.patch.object(server, "create_server", side_effect=boom):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = start.serve_web(8791, open_browser=False)
        self.assertEqual(rc, 1)
        self.assertIn("8791", out.getvalue())
        self.assertIn("--port 8792", out.getvalue())

    def test_serve_passes_factory_args_and_stops_on_ctrl_c(self):
        httpd = mock.MagicMock()
        httpd.server_address = ("127.0.0.1", 8791)
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(server, "create_server",
                               return_value=httpd) as factory:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = start.serve_web(8791, open_browser=True)
        self.assertEqual(rc, 0)
        factory.assert_called_once_with(8791, open_browser=True)
        httpd.server_close.assert_called_once()


class MainWiringTest(unittest.TestCase):
    def test_failures_abort_before_serving(self):
        with mock.patch.object(start, "run_checks",
                               return_value=["缺 whisper_bin"]), \
             mock.patch.object(start, "serve_web") as serve:
            self.assertEqual(start.main([]), 1)
        serve.assert_not_called()

    def test_green_checks_serve(self):
        with mock.patch.object(start, "run_checks", return_value=[]), \
             mock.patch.object(start, "serve_web", return_value=0) as serve:
            self.assertEqual(start.main(["--port", "8790", "--no-browser"]), 0)
        serve.assert_called_once_with(8790, False)


if __name__ == "__main__":
    unittest.main()
