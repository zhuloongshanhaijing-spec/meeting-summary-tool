"""One-command launcher: preflight the toolchain, then serve the web console.

    python3 start.py [--port N] [--no-browser]

Preflight chain — every failure is collected, then reported together (one
missing piece never hides another); any ✗ aborts before a port is bound:

  a. config.resolve()      required keys, one collective error
  b. ffmpeg                found via PATH (or cfg["ffmpeg"] override)
  c. whisper-cli + ggml    binary executable, model file non-empty
  d. qwen venv python      executable
  e. memory                run_meeting.check_memory(), informational only
  f. Ollama                GET /api/tags; on miss `open -a Ollama` + poll

run_meeting.py resolves config at import time, so it is imported lazily —
only after the config check has passed. Importing this module has no side
effects, and every check is a small injectable function (tests/test_start.py).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent

OLLAMA_PROBE_TIMEOUT = 3      # seconds for the first GET /api/tags
OLLAMA_START_TIMEOUT = 60.0   # total budget after `open -a Ollama`
OLLAMA_POLL_INTERVAL = 2.0    # poll cadence while waiting


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ok(msg: str) -> None:
    log(f"✓ {msg}")


# --------------------------------------------------------------------------
# Individual checks (small + injectable; each returns failure text or None)
# --------------------------------------------------------------------------

def check_config() -> tuple[dict | None, str | None]:
    """(cfg, None) on success; (None, message) when resolve() raises the
    collective SystemExit naming every missing key + its MST_* env var."""
    try:
        return config.resolve(), None
    except SystemExit as exc:
        return None, str(exc)


def check_ffmpeg(cfg: dict | None = None) -> str | None:
    """ffmpeg reachable on PATH (or at the explicit cfg override)."""
    target = (cfg or {}).get("ffmpeg") or "ffmpeg"
    if shutil.which(target):
        return None
    hint = "brew install ffmpeg" if target == "ffmpeg" else "检查该路径是否有效"
    return f"ffmpeg 未找到（{hint}）"


def check_binaries(cfg: dict) -> list[str]:
    """whisper-cli / ggml model / qwen venv python — collect every miss."""
    failures: list[str] = []

    whisper_bin = Path(cfg["whisper_bin"])
    if whisper_bin.is_file() and os.access(whisper_bin, os.X_OK):
        ok(f"whisper-cli: {whisper_bin}")
    else:
        failures.append(f"whisper_bin 不存在或不可执行: {whisper_bin}"
                        "（检查 config.json 或环境变量 MST_WHISPER_BIN）")

    model = Path(cfg["whisper_model"])
    if model.is_file() and model.stat().st_size > 0:
        ok(f"whisper 模型: {model}")
    else:
        failures.append(f"whisper_model 不存在或为空文件: {model}"
                        "（检查 config.json 或环境变量 MST_WHISPER_MODEL）")

    qwen = Path(cfg["qwen_python"])
    if qwen.is_file() and os.access(qwen, os.X_OK):
        ok(f"qwen python: {qwen}")
    else:
        failures.append(f"qwen_python 不存在或不可执行: {qwen}"
                        "（Qwen3-ASR venv 的解释器，环境变量 MST_QWEN_PYTHON）")
    return failures


def memory_info() -> dict:
    """run_meeting.check_memory(), imported lazily: run_meeting resolves
    config at import time, so it must only load after check_config passed.
    Informational by design — never a failure item."""
    try:
        import run_meeting
        return run_meeting.check_memory()
    except Exception:
        return {"total_gb": 0.0, "available_gb": 0.0}


def ollama_alive(url: str, timeout: float) -> bool:
    try:
        urllib.request.urlopen(f"{url}/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def wait_ollama(url: str, timeout: float = OLLAMA_START_TIMEOUT) -> bool:
    """Probe /api/tags; on miss launch the Ollama app and poll every 2s
    until the deadline (seconds). True as soon as the API answers."""
    if ollama_alive(url, OLLAMA_PROBE_TIMEOUT):
        return True
    log("Ollama 未运行，正在拉起（open -a Ollama）…")
    subprocess.Popen(["open", "-a", "Ollama"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(min(OLLAMA_POLL_INTERVAL, max(deadline - time.monotonic(), 0.0)))
        if ollama_alive(url, 2):
            return True
    return False


def run_checks() -> list[str]:
    """The whole chain, in order. Prints ✓ progress lines as it goes and
    returns every failure collected (empty list = ready to serve)."""
    failures: list[str] = []

    cfg, err = check_config()
    if cfg is None:
        failures.append("配置无效（路径检查已跳过）：\n" + (err or "未知错误"))
        ffmpeg_miss = check_ffmpeg(None)
        if ffmpeg_miss:
            failures.append(ffmpeg_miss)
        return failures
    ok("配置（config.json / MST_* 环境变量）")

    ffmpeg_miss = check_ffmpeg(cfg)
    if ffmpeg_miss:
        failures.append(ffmpeg_miss)
    else:
        ok(f"ffmpeg: {shutil.which(cfg.get('ffmpeg') or 'ffmpeg')}")

    failures.extend(check_binaries(cfg))

    mem = memory_info()
    log(f"内存: 共 {mem.get('total_gb', 0):.0f} GB，可用 "
        f"{mem.get('available_gb', 0):.1f} GB（仅供参考）")

    url = cfg.get("ollama_url") or "http://127.0.0.1:11434"
    if wait_ollama(url):
        ok(f"Ollama: {url}")
    else:
        failures.append(f"Ollama 启动超时：{OLLAMA_START_TIMEOUT:.0f}s 内未就绪"
                        f"（{url}/api/tags 无响应，请手动打开 Ollama app 后重试）")
    return failures


def report_failures(failures: list[str]) -> None:
    log(f"预检未通过（{len(failures)} 项），已取消启动。请逐项修复：")
    for failure in failures:
        for i, line in enumerate(str(failure).splitlines()):
            log(("✗ " if i == 0 else "  ") + line)


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

def serve_web(port: int | None, open_browser: bool) -> int:
    """Bring up webapp/server.py. Process exit code: 0 clean stop, 1 when
    the port is already taken."""
    webapp_dir = str(ROOT / "webapp")
    if webapp_dir not in sys.path:
        sys.path.insert(0, webapp_dir)
    import server

    try:
        httpd = server.create_server(port or None, open_browser=open_browser)
    except OSError as exc:
        wanted = port or os.environ.get("MST_WEB_PORT") or server.DEFAULT_PORT
        try:
            hint = int(wanted) + 1
        except (TypeError, ValueError):
            hint = server.DEFAULT_PORT + 1
        log(f"✗ 端口 {wanted} 已被占用（{exc.strerror or exc}），试试 --port {hint}")
        return 1

    log(f"网页控制台已就绪: http://127.0.0.1:{httpd.server_address[1]}/ （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，已停止")
    finally:
        httpd.server_close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="一键启动：预检依赖 → 打开网页控制台（默认 127.0.0.1:8788）")
    parser.add_argument("--port", type=int, default=None,
                        help="网页端口（默认 8788，或环境变量 MST_WEB_PORT）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("预检开始")
    try:
        failures = run_checks()
    except KeyboardInterrupt:
        log("已取消（Ctrl+C）")
        return 130
    if failures:
        report_failures(failures)
        return 1
    ok("预检全部通过，启动网页控制台")
    return serve_web(args.port or None, not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
