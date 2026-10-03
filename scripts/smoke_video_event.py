#!/usr/bin/env python3
"""T9 全链路 LIVE 验收（录屏解析模块，计划卡 T9 / 设计规格 §6.4）.

真实链路、零 mock：真 ffmpeg、真 detect_slide_region、真 extract_video_slides、
真 Apple Vision OCR（沙箱下允许降级，见下）、真 ASR/LLM（whisper.cpp +
Qwen3-ASR + Ollama qwen3:8b，走 run_meeting 房屋管线本尊）。

两条路径（T9 卡原文）：
  ① WEB 路径：T1 短合成含声视频 → HTTP 上传 → /api/region/detect（IoU≥0.9）
     → /api/region/confirm（nudge 后的 rect，source=user）→ /api/start →
     轮询 /api/status 至 done → 14 站全部出现（stage_total==14、含
     video_ingest/slide_align、video_ingest.* 子站）→ 包铁律断言 →
     validate_package_v3 直接调用 PASS（控制台 stage_validate 的怪异 root
     为既有冻结行为，仅记录不修复）。
  ② CLI 兜底路径：input/ 直投视频（无 region.json）→ 直接跑 run_meeting.py →
     自动检测兜底（ingest_receipt warnings 含 region_auto_not_user_confirmed、
     runs/<ev>/video/region.json source=auto）→ 00_使用说明.md 末行含
     「幻灯片区域为自动检测，未经人工确认。」→ 同一套包铁律。

音频场景（诚实处理合成音轨 vs 真 ASR）：
  - TONE 场景：T1 生成器原生 440Hz 正弦音轨（audio=True）。若 whisper 对正弦
    音产出零记录，管线在 literal 站按既有契约 fail-fast（run_meeting.py
    「no ASR output available for literal record assembly」）——该结果被断言为
    「文档化的零记录 fail-fast」并记录为产品发现，不算 smoke 失败；若 whisper
    幻听出记录而跑完，则按 done 分支跑全套包铁律。
  - VOICE 场景（权威全断言跑）：macOS `say` 生成真实语音（内容点名幻灯片词
    Roadmap/Budget/Timeline/Risks/QA Plan/Metrics/Scope），apad 到整 20.0s 后
    与同参数无声合成视频 mux（视频流 -c:v copy，帧内容与 T1 静音版逐帧一致，
    T3 钉死的 4 页地面真值不变）。视频文件含真实音轨 →「含声视频」满足。

OCR 环境现实：Apple Vision 在 agent 文件沙箱下可能失败（vision_handler_failed
/ swiftc 被拒）。管线按规格 §5 正确降级（ocr_unavailable 警告或逐页 error 行，
证据行低置信、包照常构建）。本脚本的包断言全部是「两种 OCR 结局都成立」的
结构铁律；实际 OCR 路线记录进环境报告（若 OCR 真完成，追加断言 03 含 OCR
文本且 receipt ocr=="complete"）。协调者可在非沙箱终端重跑本脚本复核。

安全约定（与 smoke_webapp.py 同范式）：
  - input/ 中既有事件先隔离到工作区内 runs/.smoke-quarantine-<pid>/（同盘原子
    rename）。退出顺序铁律（C1）：礼貌 /api/stop → 终止全部登记子进程组
    （server、CLI run_meeting）→ 若且仅若本脚本的 server 曾启动（N1 gate），
    终止 runs/.webapp/pipeline.pid 的管线进程组并确认死亡、清理死 pidfile →
    然后才恢复隔离条目 → 确认全部恢复后才销毁隔离目录。server 从未启动时
    任何 pidfile 都属于用户管线，绝不触碰。活的 run_meeting 成功后会清空
    input/，绝不允许它活过恢复时刻。
  - 硬杀恢复（脚本被 SIGKILL/断电打断时；SIGTERM 与 Ctrl-C 已被转成完整清理
    路径，不留孤儿）：用户数据完好保留在
    runs/.smoke-quarantine-<pid>/<事件名>/ ——手工把每个事件目录 mv 回
    input/，再删掉空的隔离目录即可。下次运行 preflight 发现任何残留
    .smoke-quarantine-* 目录会大声报告（路径+内容+恢复步骤）并拒绝开跑（rc=2）。
  - preflight 同时拒绝在 runs/.webapp/pipeline.pid 指示用户管线正在运行时开跑
    （隔离 input/ 会破坏正在跑的编译）；等它结束或 /api/stop 后再跑。
  - 服务用 start.py --port <随机空闲端口> --no-browser 启动（范式机制）；
    boot 失败路径先 SIGKILL 服务进程组再做有界读取（communicate(timeout=5)），
    无响应但活着的服务不能挂死脚本、搁浅隔离数据。
  - 事件名带 PID（smoke-video-web-<pid> 等）避免碰撞；成功跑（rc=0）退出时
    清掉本脚本产生的 input/runs/outputs 目录与 staging；--keep 或任何非零
    退出码（FAIL/BLOCKED/崩溃）自动保留全部 smoke 工件并打印路径供诊断
    （隔离的用户 input 条目无论如何都会先恢复）。

用法：python3 scripts/smoke_video_event.py [--keep] [--skip-tone]
  --keep       成功后也保留 smoke 工件（调试用；隔离的既有 input 事件仍会恢复）
  --skip-tone  跳过 TONE 正弦场景及其 fixture 合成（只跑 VOICE 权威场景 + CLI）
  未知/拼错参数一律被 argparse 拒绝（rc=2，零副作用）——数据安全脚本绝不
  因手滑参数而静默开跑一整轮真实管线。
退出码契约：
  0 = 全部断言通过（工件已清理）
  1 = 存在断言 FAIL（工件自动保留，路径已打印）
  2 = BLOCKED：环境预检未过 / 陈旧隔离目录 / 用户管线在跑 / 用法错误
      （argparse 标准码）——未开跑，零 smoke 工件副作用
  3 = 脚本自身崩溃（含 Ctrl-C/SIGTERM）：打印已累计的部分断言汇总（崩溃绝不
      伪装成断言 FAIL，也绝不静默消失），工件自动保留
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

INPUT = WS / "input"
OUTPUTS = WS / "outputs"
RUNS = WS / "runs"
PROGRESS_JSONL = RUNS / "progress.jsonl"
PIPELINE_LOG = RUNS / ".webapp" / "pipeline.log"
PID = os.getpid()

# T1/T3 钉死的 fixture 形状（tests/test_extract_video_slides.py v1_voiced 同参）：
# 20.0s / 10fps / 640x400 / 默认 schedule（5 段 4 页，page1 翻回）/ 默认鼠标划过。
VIDEO_PARAMS = {"duration_s": 20.0, "fps": 10, "size": (640, 400)}
GT_PPT_RECT = (0.05, 0.05, 0.62, 0.9)     # synth_video 默认 ppt_rect（相对坐标）
EXPECTED_PAGES = ["P1", "P2", "P3", "P4"]  # T3 钉死：首现顺序页号
SPEECH_TEXT = (
    "Good morning everyone, and welcome to the quarterly planning meeting. "
    "Today we walk through the roadmap for the next quarter. First, we review "
    "the budget and the spending plan. Then we look at the timeline, the key "
    "milestones, and the main risks. After a short break, we cover the Q A "
    "plan, the product metrics, and the overall project scope. Thank you all "
    "for joining."
)
GUIDE_NAV_LINE = "- 问 PPT 页面内容/对应发言：查《03_PPT补充信息》与 幻灯片/ 目录。"
GUIDE_AUTO_REGION_LINE = "幻灯片区域为自动检测，未经人工确认。"
NOTES_TEXT = (
    "# 规划会笔记\n"
    "- Roadmap：下季度路线图优先交付\n"
    "- Budget：预算评审关注支出计划\n"
    "- Timeline / Risks：时间线与主要风险跟踪\n"
    "- QA Plan / Metrics / Scope：质量计划、产品指标与项目范围\n"
)
OCR_MARK_02 = "〔OCR建议:"          # build_package_v3.py:87（02 行尾标注）
OCR_MARK_05 = "## OCR 修正建议"      # build_package_v3.py:162（05 新栏）
ZERO_RECORD_SIGNATURE = "no ASR output available"  # run_meeting.py literal 站 fail-fast

RESULTS: list[tuple[str, str, bool, str, str]] = []
FINDINGS: list[str] = []
ENV_NOTES: list[str] = []


class SmokeError(RuntimeError):
    """环境不满足 / 前置条件失败 → BLOCKED（退出码 2），区别于断言 FAIL。"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(section: str, label: str, ok: bool, actual="", expected="") -> bool:
    """记录并打印一条断言（PASS/FAIL + 实际 vs 期望）。返回 ok 供短路。"""
    RESULTS.append((section, label, bool(ok), str(actual), str(expected)))
    mark = "PASS" if ok else "FAIL"
    detail = f"实际={actual}" + (f"｜期望={expected}" if expected != "" else "")
    print(f"  [{mark}] {label}（{detail}）", flush=True)
    return bool(ok)


def note(msg: str) -> None:
    ENV_NOTES.append(msg)
    log(f"  · 环境记录: {msg}")


def finding(msg: str) -> None:
    FINDINGS.append(msg)
    log(f"  ⚠ 产品/代码发现（不修复，只记录）: {msg}")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# --------------------------------------------------------------------------
# 子进程登记与终止（C1：绝不留活口 run_meeting——它成功后会清空 input/，
# 若活过隔离恢复时刻，会吞掉刚恢复的用户数据。main 的 finally 顺序铁律：
# 礼貌 stop → kill_registered_children() → kill_pipeline_from_pidfile()
# （含等待/收割、确认死亡）→ restore_input()。）
# --------------------------------------------------------------------------

_CHILDREN: list[subprocess.Popen] = []


def register_child(proc: subprocess.Popen) -> subprocess.Popen:
    """登记以 start_new_session 启动的子进程；退出时统一组终止并收割。"""
    _CHILDREN.append(proc)
    return proc


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _group_kill(pid: int, sig: int) -> None:
    """优先杀整个进程组；若目标组 == 本进程所在组，降级为只杀单进程——
    绝不自杀式误伤（V3 验证实测：对共享组的 pidfile 目标 killpg 会当场杀死
    脚本自身与测试 shell）。生产路径中管线/子进程都是 start_new_session
    独立组，该守卫是数据安全脚本的防御纵深。全守护，绝不抛出。"""
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return
    try:
        if pgid != os.getpgid(0):
            os.killpg(pgid, sig)
            return
    except (OSError, ProcessLookupError):
        pass
    try:
        os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _kill_proc_group(proc: subprocess.Popen, term_wait: float = 10.0,
                     kill_wait: float = 10.0) -> None:
    """SIGTERM→收割→SIGKILL→收割整个进程组（同组时降级单杀）；全守护不抛出。"""
    if proc.poll() is not None:
        return
    _group_kill(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=term_wait)
        return
    except subprocess.TimeoutExpired:
        pass
    _group_kill(proc.pid, signal.SIGKILL)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=kill_wait)
    except subprocess.TimeoutExpired:
        log(f"  ❌ 子进程 pid={proc.pid} SIGKILL 后仍未退出（异常状态，请人工检查）")


def kill_registered_children() -> None:
    """终止并收割全部登记子进程（server、CLI run_meeting）。"""
    for proc in list(_CHILDREN):
        if proc.poll() is None:
            log(f"  终止登记子进程 pid={proc.pid}…")
        _kill_proc_group(proc)


def _reap_if_child(pid: int) -> None:
    """目标若是本进程的子进程则收割——僵尸会让 os.kill(pid,0) 继续成功、
    骗过 pid_alive 的死亡确认；非子进程由 launchd 收割（生产路径：管线是
    server 的子进程，server 已先被杀+收割，管线成孤儿由 launchd 处理）。"""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def kill_pipeline_from_pidfile(term_wait: float = 12.0) -> None:
    """终止 server 拉起的管线进程组（runs/.webapp/pipeline.pid）并确认死亡。

    管线是独立会话（start_new_session），杀 server 组不会带走它；必须在
    restore_input() 之前确认死亡。
    """
    info = read_json(RUNS / ".webapp" / "pipeline.pid") or {}
    pid = info.get("pid") if isinstance(info, dict) else None
    if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
        return
    log(f"  pidfile 显示管线 pid={pid} 仍存活 → 终止（恢复 input/ 前须确认死亡）…")
    _group_kill(pid, signal.SIGTERM)
    end = time.time() + term_wait
    while time.time() < end:
        _reap_if_child(pid)
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    if pid_alive(pid):
        _group_kill(pid, signal.SIGKILL)
        end = time.time() + 10
        while time.time() < end:
            _reap_if_child(pid)
            if not pid_alive(pid):
                break
            time.sleep(0.5)
    if pid_alive(pid):
        log(f"  ❌ 管线 pid={pid} 终止失败——恢复照常执行，但请立即人工检查该进程！")
    else:
        log(f"  管线 pid={pid} 已确认死亡")
        # N1(a) hygiene: 我们的 server 已先被杀，其 watcher 不会再清 pidfile——
        # 尽力移除死 pidfile 防陈旧残留（调用点 gate 已保证 server 是本脚本的）。
        try:
            (RUNS / ".webapp" / "pipeline.pid").unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# HTTP helpers（smoke_webapp.py 范式原样）
# --------------------------------------------------------------------------

def api(base: str, path: str, method: str = "GET", data: bytes | None = None,
        headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # 预期中的 4xx/5xx（如 awaiting_region 门禁 409）也是断言对象：
        # 转成 (code, body) 而非异常（smoke_webapp 范式只走过 2xx，未覆盖此分支）
        with exc:
            return exc.code, exc.read()


def api_json(base: str, path: str, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, timeout: int = 30):
    code, body = api(base, path, method=method, data=data, headers=headers,
                     timeout=timeout)
    return code, json.loads(body.decode("utf-8"))


def post_json(base: str, path: str, payload: dict, timeout: int = 160):
    return api_json(base, path, "POST", json.dumps(payload).encode("utf-8"),
                    {"Content-Type": "application/json"}, timeout=timeout)


def multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]],
              boundary: str) -> bytes:
    out = io.BytesIO()
    for name, value in fields.items():  # fields first — server contract
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, fname, payload in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="{name}"; filename="{fname}"\r\n'
                  f"Content-Type: application/octet-stream\r\n\r\n".encode())
        out.write(payload + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(base: str, proc: subprocess.Popen, deadline_s: float = 90) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            api(base, "/api/status", timeout=2)
            return True
        except OSError:
            time.sleep(0.4)
    return False


def _iou(rect: dict, gt: tuple[float, float, float, float]) -> float:
    ax1, ay1 = float(rect["x"]), float(rect["y"])
    ax2, ay2 = ax1 + float(rect["w"]), ay1 + float(rect["h"])
    bx1, by1, bx2, by2 = gt[0], gt[1], gt[0] + gt[2], gt[1] + gt[3]
    inter = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def ffprobe_media(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60, check=True)
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"duration": float(info["format"]["duration"]),
            "has_video": video is not None, "has_audio": audio is not None,
            "width": int(video["width"]) if video else None,
            "height": int(video["height"]) if video else None}


def sh(cmd: list[str], timeout: int, log_path: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command; non-zero exit raises SmokeError with the output tail."""
    proc = subprocess.run(cmd, cwd=str(WS), capture_output=True, text=True,
                          timeout=timeout)
    if log_path is not None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(str(c) for c in cmd)}\n{proc.stdout}\n{proc.stderr}\n")
    if proc.returncode != 0:
        raise SmokeError(f"命令失败 (exit {proc.returncode}): {' '.join(str(c) for c in cmd[:6])}\n"
                         f"{(proc.stdout or '')[-800:]}{(proc.stderr or '')[-800:]}")
    return proc


# --------------------------------------------------------------------------
# 环境预检（真实模型可用性——不满足即 BLOCKED，绝不假跑）
# --------------------------------------------------------------------------

def preflight() -> dict:
    problems: list[str] = []
    try:
        import config
        cfg = config.resolve()
    except SystemExit as exc:
        raise SmokeError(f"config.resolve() 失败: {exc}") from None

    for binary in ("ffmpeg", "ffprobe", "say"):
        if shutil.which(binary) is None:
            problems.append(f"PATH 缺少 {binary}")
    note(f"swiftc: {'在 PATH（OCR 可能可用）' if shutil.which('swiftc') else '不在 PATH（OCR 将降级）'}")

    tools = Path(str(cfg["tools_python"]))
    if not tools.is_file():
        problems.append(f"tools_python 不存在: {tools}（先跑 setup.sh）")
    else:
        probe = subprocess.run([str(tools), "-c", "import cv2, numpy"],
                               capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            problems.append(f"tools venv 缺 cv2/numpy: {probe.stderr[-200:]}")
        piny = subprocess.run([str(tools), "-c", "import pypinyin"],
                              capture_output=True, text=True, timeout=60)
        note(f"pypinyin: {'可用（OCR 修正同音路径）' if piny.returncode == 0 else '不可用（近形降级路径）'}")

    whisper_bin, whisper_model = Path(cfg["whisper_bin"]), Path(cfg["whisper_model"])
    if not whisper_bin.is_file() or not os.access(whisper_bin, os.X_OK):
        problems.append(f"whisper_bin 不可执行: {whisper_bin}")
    if not whisper_model.is_file() or whisper_model.stat().st_size == 0:
        problems.append(f"whisper_model 缺失: {whisper_model}")
    if not Path(str(cfg["qwen_python"])).is_file():
        problems.append(f"qwen_python 不存在: {cfg['qwen_python']}")

    try:
        with urllib.request.urlopen(cfg["ollama_url"].rstrip("/") + "/api/tags",
                                    timeout=5) as resp:
            models = {m.get("name") for m in json.loads(resp.read()).get("models", [])}
        if cfg["ollama_model"] not in models:
            problems.append(f"ollama 缺模型 {cfg['ollama_model']}（现有: {sorted(models)[:6]}）")
    except (OSError, ValueError) as exc:
        problems.append(f"ollama 不可达 {cfg['ollama_url']}: {exc}")

    for rel in ("run_meeting.py", "start.py",
                "core/scripts/detect_slide_region.py",
                "core/scripts/extract_video_slides.py",
                "core/scripts/suggest_ocr_corrections.py",
                "core/scripts/build_package_v3.py",
                "core/scripts/validate_package_v3.py",
                "core/scripts/run_vision_ocr.py",
                "tests/synth_video.py"):
        if not (WS / rel).is_file():
            problems.append(f"缺少 {rel}")

    # I3: 陈旧隔离目录 = 上次 smoke 被硬杀（SIGKILL/断电）的痕迹；用户 input
    # 数据安全地留在里面。大声报告（路径+内容+恢复步骤）并拒绝开跑（rc=2）。
    stale = sorted(RUNS.glob(".smoke-quarantine-*"))
    if stale:
        detail = []
        for entry in stale:
            try:
                names = sorted(p.name for p in entry.iterdir()) if entry.is_dir() else ["<非目录>"]
            except OSError:
                names = ["<目录不可读>"]
            detail.append(f"{entry} → {names}")
        problems.append(
            "发现陈旧隔离目录（上次 smoke 可能被硬杀；用户 input 数据在其中，安全）: "
            + "; ".join(detail)
            + "。恢复步骤: 把其中每个事件目录 mv 回 input/，删掉空的隔离目录，再重跑本脚本")

    # preflight add: runs/.webapp/pipeline.pid 指示用户管线正在运行 → 隔离
    # input/ 会破坏正在跑的编译，拒绝开跑（陈旧 pidfile（进程已死）不拦）。
    pid_info = read_json(RUNS / ".webapp" / "pipeline.pid")
    if isinstance(pid_info, dict):
        live_pid = pid_info.get("pid")
        if isinstance(live_pid, int) and live_pid > 0 and pid_alive(live_pid):
            problems.append(
                f"runs/.webapp/pipeline.pid 指示用户管线正在运行 (pid={live_pid})——"
                f"隔离 input/ 会破坏它；请等它结束或经控制台 /api/stop 停止后再跑")

    if problems:
        raise SmokeError("环境预检未过（BLOCKED，未开跑）:\n  - " + "\n  - ".join(problems))

    note(f"模型: whisper={whisper_bin.name} model={whisper_model.name} "
         f"qwen_python={Path(str(cfg['qwen_python'])).parent.parent.name} "
         f"ollama={cfg['ollama_model']} @ {cfg['ollama_url']}")
    return cfg


# --------------------------------------------------------------------------
# fixture 生成（合成视频经 tools-venv 子进程；语音经 macOS say）
# --------------------------------------------------------------------------

def gen_synth_video(dest: Path, audio: bool, fixture_log: Path) -> dict:
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import synth_video; "
            "meta = synth_video.generate_meeting_video(sys.argv[2], duration_s=20.0, "
            "fps=10, size=(640, 400), audio=(sys.argv[3] == '1')); "
            "print(json.dumps(meta))")
    import config
    tools = config.resolve()["tools_python"]
    proc = sh([str(tools), "-B", "-c", code, str(WS / "tests"), str(dest),
               "1" if audio else "0"], timeout=900, log_path=fixture_log)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def make_speech_wav(dest: Path, fixture_log: Path) -> float:
    """macOS say 真实语音 → 16kHz 单声道 wav，apad/atrim 到整 20.0s。"""
    aiff = dest.with_suffix(".aiff")
    rate = 175
    dur = 0.0
    for _attempt in range(3):
        sh(["say", "-r", str(rate), "-o", str(aiff), SPEECH_TEXT],
           timeout=120, log_path=fixture_log)
        dur = ffprobe_media(aiff)["duration"]
        if dur <= 19.7:  # 必须 < 20.0s（mux -t 20 截断会吃字）；apad 补足尾部
            break
        rate = max(150, int(rate * dur / 19.2))  # 超速重生成，避免 mux 截断吃字
        log(f"  语音 {dur:.1f}s 偏长 → 语速提到 {rate} 重试")
    if dur > 19.9:
        raise SmokeError(f"say 语音无法压进 20s（{dur:.2f}s）——请缩短 SPEECH_TEXT")
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(aiff),
        "-af", "apad", "-t", "20.0", "-ar", "16000", "-ac", "1", str(dest)],
       timeout=120, log_path=fixture_log)
    aiff.unlink(missing_ok=True)
    final = ffprobe_media(dest)["duration"]
    if abs(final - 20.0) > 0.3:
        raise SmokeError(f"语音 wav 时长异常: {final}")
    return dur


def mux_voiced(video_silent: Path, wav: Path, dest: Path, fixture_log: Path) -> None:
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(video_silent), "-i", str(wav),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
        "-t", "20.0", str(dest)], timeout=300, log_path=fixture_log)


# （M6：原 write_notes 已删——trivially dead：上传直接内联 NOTES_TEXT 字节，
#  staging 里的 notes.md 从无读者。）


# --------------------------------------------------------------------------
# input/ 既有事件隔离与恢复（run_meeting 成功会清空 input/——绝不冒险）
# --------------------------------------------------------------------------

_QUARANTINE: list[tuple[Path, Path]] = []
_QUARANTINE_DIR: Path | None = None  # main 设置；atexit/restore 兜底扫描用


def quarantine_input(qdir: Path) -> None:
    """把既有 input/ 条目移进工作区内隔离目录（同一文件系统 → os.rename 原子移动；
    绝不移出工作区——staging 销毁永远不会波及用户数据）。"""
    if not INPUT.is_dir():
        return
    for entry in sorted(INPUT.iterdir(), key=lambda p: p.name):
        target = qdir / entry.name
        qdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(target))
        _QUARANTINE.append((target, entry))
        log(f"  隔离既有 input 条目: {entry.name} → {target}")


def restore_input(quarantine_dir: Path | None = None) -> list[tuple[Path, Path]]:
    """把隔离的既有 input 条目移回原位。返回未能恢复的条目（必须为空才可销毁
    staging——顺序铁律：先恢复、后删 staging，否则等于销毁用户数据）。

    兜底扫描（C1 加固）：quarantine_input 的 move 与登记之间存在信号窗口，
    硬中断可能留下「已隔离未登记」的孤儿条目——传入隔离目录即全量扫描，
    同样恢复，堵死数据丢失窗口。
    """
    unrestored: list[tuple[Path, Path]] = []
    while _QUARANTINE:
        src, original = _QUARANTINE.pop()
        try:
            if not src.exists():
                if not original.exists():
                    unrestored.append((src, original))
                    log(f"  ❌ 隔离副本消失且原位无文件: {original}（数据丢失，请检查 {src}）")
                continue
            if original.exists():
                unrestored.append((src, original))
                log(f"  ⚠ 恢复跳过（原路径已被占用）: {original}（隔离副本保留在 {src}）")
                continue
            INPUT.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(original))
            log(f"  恢复既有 input 条目: {original.name}")
        except OSError as exc:
            unrestored.append((src, original))
            log(f"  ❌ 恢复失败 {src} → {original}: {exc}（隔离副本保留，请手工恢复！）")
    if quarantine_dir is not None and quarantine_dir.is_dir():
        try:
            orphans = sorted(p for p in quarantine_dir.iterdir())
        except OSError:
            orphans = []
        for entry in orphans:
            original = INPUT / entry.name
            if original.exists():
                unrestored.append((entry, original))
                log(f"  ⚠ 孤儿条目跳过（原路径已被占用）: {original}（副本保留在 {entry}）")
                continue
            try:
                INPUT.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry), str(original))
                log(f"  恢复孤儿隔离条目（move/登记窗口兜底）: {entry.name}")
            except OSError as exc:
                unrestored.append((entry, original))
                log(f"  ❌ 孤儿条目恢复失败 {entry} → {original}: {exc}（请手工恢复！）")
    return unrestored


def _atexit_restore() -> None:
    unrestored = restore_input(_QUARANTINE_DIR)
    if unrestored:
        log(f"❌ atexit: {len(unrestored)} 个隔离条目未恢复——请勿清理 staging: {unrestored}")
    elif _QUARANTINE_DIR is not None and _QUARANTINE_DIR.is_dir():
        # 全部恢复成功 → 空的隔离目录随即删除，避免下次 preflight 把空壳当
        # 陈旧残留拒跑（I3 误报）。非空则保留（数据可见，拒跑正确）。
        try:
            if not any(_QUARANTINE_DIR.iterdir()):
                _QUARANTINE_DIR.rmdir()
        except OSError:
            pass


atexit.register(_atexit_restore)


def _sigterm_to_interrupt(signum, frame):
    """C1 加固：SIGTERM 转成 KeyboardInterrupt，走完整清理路径（杀登记子进程→
    杀 pidfile 管线组→恢复 input/→rc=3 部分汇总）。SIGTERM 默认处置直接杀
    进程、不跑 finally/atexit——V2 验证实测：父进程被 SIGTERM 后 server+管线
    成孤儿、隔离目录搁浅（数据安全，但需手工恢复）。SIGKILL 仍属硬杀场景
    （I3：下次 preflight 拒跑 + 按 docstring 手工恢复步骤）。"""
    raise KeyboardInterrupt(f"SIGTERM (signal {signum})")


# --------------------------------------------------------------------------
# 时间线 / 日志取证
# --------------------------------------------------------------------------

def timeline_rows(event: str) -> list[dict]:
    return [r for r in read_jsonl(PROGRESS_JSONL) if r.get("event") == event]


def tail_new_text(path: Path, offset: int, limit: int = 400_000) -> str:
    """按字节偏移截取新增内容（M4：rb 打开——file_offset 给的是 st_size 字节数，
    文本模式 seek 任意整数不合法）。"""
    try:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def file_offset(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def grep_validate_line(text: str) -> str:
    for line in text.splitlines():
        if "确定性验证" in line:
            return line.strip()
    return "(日志中未见 确定性验证 行)"


def ocr_route_summary(event: str) -> str:
    receipt = read_json(RUNS / event / "video" / "ingest_receipt.json") or {}
    parts = [f"receipt.ocr={receipt.get('ocr')}"]
    rows = read_jsonl(RUNS / event / "slides" / "ocr.jsonl")
    if rows:
        errs = [r for r in rows if r.get("error")]
        parts.append(f"ocr.jsonl={len(rows)}行/{len(errs)}行error")
        if errs:
            parts.append(f"error样例={str(errs[0].get('error'))[:90]}")
    else:
        parts.append("ocr.jsonl 不存在")
    parts.append(f"warnings={receipt.get('warnings')}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# 包铁律断言（web done 分支与 CLI 路径共用；对两种 OCR 结局都成立）
# --------------------------------------------------------------------------

def assert_package_laws(section: str, event: str, *, region_source: str,
                        auto_region_annotation: bool) -> None:
    outdir = OUTPUTS / event
    rundir = RUNS / event
    if not check(section, f"outputs/{event} 报告包目录存在", outdir.is_dir(),
                 str(outdir), "目录存在"):
        return

    # -- 幻灯片/ 目录与页数一致（卡片断言） ----------------------------------
    slides_dir = outdir / "幻灯片"
    if not check(section, "包内 幻灯片/ 目录存在", slides_dir.is_dir(),
                 str(slides_dir), "目录存在"):
        return
    pkg_pngs = sorted(p.name for p in slides_dir.glob("P*.png"))
    pkg_slides_doc = read_json(slides_dir / "slides.json") or {}
    pkg_slides = pkg_slides_doc.get("slides") or []
    check(section, "幻灯片/ P*.png 数 == slides.json slides 数",
          len(pkg_pngs) == len(pkg_slides) and len(pkg_pngs) > 0,
          f"{len(pkg_pngs)} png vs {len(pkg_slides)} slides", "相等且>0")
    page_ids = [s.get("page_id") for s in pkg_slides]
    check(section, "页号 == 生成器地面真值 P1..P4（默认 schedule，T3 钉死形状）",
          page_ids == EXPECTED_PAGES, str(page_ids), str(EXPECTED_PAGES))
    revisit = next((s for s in pkg_slides if s.get("page_id") == "P2"), {})
    check(section, "P2（翻回页）time_ranges 合并为 2 段",
          len(revisit.get("time_ranges") or []) == 2,
          str(revisit.get("time_ranges")), "2 段")
    check(section, "slides.json region_source 与确认路径一致",
          pkg_slides_doc.get("region_source") == region_source,
          str(pkg_slides_doc.get("region_source")), region_source)

    # -- ingest receipt（音轨路线 / 区域来源 / OCR 路线记录） ------------------
    ingest = read_json(rundir / "video" / "ingest_receipt.json") or {}
    check(section, "ingest_receipt.slide_track == true",
          ingest.get("slide_track") is True, str(ingest.get("slide_track")), "True")
    check(section, "ingest_receipt.audio_route == extracted（视频含声，无独立音频）",
          ingest.get("audio_route") == "extracted",
          str(ingest.get("audio_route")), "extracted")
    check(section, f"ingest_receipt.region_source == {region_source}",
          ingest.get("region_source") == region_source,
          str(ingest.get("region_source")), region_source)
    note(f"[{event}] OCR 路线: {ocr_route_summary(event)}")

    # -- 证据 / relations 覆盖铁律（卡片断言；M1: 显式非空守卫 + 严格 I\d{6}） --
    evidence = read_jsonl(rundir / "evidence" / "evidence.jsonl")
    image_ids = {r["evidence_id"] for r in evidence if r.get("kind") == "image"}
    audio_ids = {r["evidence_id"] for r in evidence if r.get("kind") == "audio"}
    records_rows = read_jsonl(rundir / "literal_records.jsonl")
    check(section, "evidence.jsonl image 行数 == 页数（严格 I###### 形式，非空）",
          len(image_ids) > 0 and len(image_ids) == len(pkg_slides)
          and all(re.fullmatch(r"I\d{6}", str(i)) for i in image_ids),
          f"{len(image_ids)} image 行", f"{len(pkg_slides)} 且全匹配 I\\d{{6}}（非空）")
    # M8（死计算改真铁律）: stage_evidence 对 literal records 1:1 构造 audio
    # 证据行（重复 ID 直接 raise）；done 事件的 literal 站保证 records 非空
    # （零记录在更早的站就 fail-fast）——两条都可安全断言。
    check(section, "literal records 非空（done 事件 literal 站契约）",
          len(records_rows) > 0, f"{len(records_rows)} 行", ">0")
    check(section, "audio 证据行与 literal records 1:1（stage_evidence 不变量）",
          len(audio_ids) == len(records_rows),
          f"audio {len(audio_ids)} 行 vs records {len(records_rows)} 行", "相等")
    relations = read_jsonl(rundir / "relations" / "final.jsonl")
    union = {eid for rel in relations for eid in rel.get("slide_evidence_ids") or []}
    check(section, "relations 覆盖铁律: slide_evidence_ids 并集 == 全部 I###### 证据（非空）",
          len(image_ids) > 0 and len(relations) > 0 and union == image_ids
          and len(relations) == len(image_ids),
          f"并集 {len(union)} / image {len(image_ids)} / 行数 {len(relations)}",
          "并集==image 集合，行数==页数，两者均非空（0==0 空洞不算过）")
    check(section, "relations v1 诚实标注: relation 全为 unknown",
          relations and all(r.get("relation") == "unknown" for r in relations),
          str(sorted({r.get("relation") for r in relations})), "{'unknown'}")
    check(section, "relations decision_route == temporal_overlap（音轨同源）",
          relations and all(r.get("decision_route") == "temporal_overlap" for r in relations),
          str(sorted({r.get("decision_route") for r in relations})), "{'temporal_overlap'}")
    if relations:
        with_cand = sum(1 for r in relations if r.get("candidate_audio_records"))
        note(f"[{event}] temporal_overlap 候选: {with_cand}/{len(relations)} 页有语音候选，"
             f"literal records={len(records_rows)}")

    # -- 03_PPT补充信息.md（卡片断言：非空 + I###### 行） ----------------------
    ppt_md = outdir / "03_PPT补充信息.md"
    ppt_text = ppt_md.read_text(encoding="utf-8") if ppt_md.is_file() else ""
    headings = re.findall(r"^### (I\d{6})", ppt_text, re.MULTILINE)
    check(section, "03_PPT补充信息.md 非空且含全部 I###### 小节",
          len(ppt_text) > 0 and len(image_ids) > 0 and set(headings) == image_ids,
          f"{len(ppt_text)} 字节, 小节={sorted(headings)}", f"I 小节=={sorted(image_ids)}（非空）")
    ocr_texts = [str(r.get("literal_text") or "") for r in evidence
                 if r.get("kind") == "image" and str(r.get("literal_text") or "").strip()]
    if ingest.get("ocr") == "complete":
        # spec-obs-d: 唯一开关是 receipt.ocr=="complete"，不再有 `and ocr_texts`
        # 逃生门——fixture 页面含可见文本，complete 却零文本本身就是 FAIL
        # （正是需要看见的降级）。
        check(section, "OCR complete → image 证据行 literal_text 全非空（fixture 页面有可见文本）",
              len(image_ids) > 0 and len(ocr_texts) == len(image_ids),
              f"{len(ocr_texts)}/{len(image_ids)}", "全部非空")
        if ocr_texts:
            sample = ocr_texts[0].strip().splitlines()[0][:40]
            check(section, "OCR complete → 03 含真实 OCR 文本", sample in ppt_text,
                  f"样例={sample!r}", "出现在 03 中")
    else:
        note(f"[{event}] receipt.ocr={ingest.get('ocr')}（非 complete，route 见上）→ "
             f"OCR 文本断言不适用；证据行低置信标注="
             f"{any('ocr' in str(r.get('uncertainty') or '') for r in evidence if r.get('kind') == 'image')}")

    # -- OCR 修正标注渗入（卡片断言：0 行时 02/05 无标注） ----------------------
    corrections = read_jsonl(rundir / "ocr_corrections.jsonl")
    rec02 = (outdir / "02_逐句会议记录.md")
    rec05 = (outdir / "05_不确定与冲突.md")
    text02 = rec02.read_text(encoding="utf-8") if rec02.is_file() else ""
    text05 = rec05.read_text(encoding="utf-8") if rec05.is_file() else ""
    if not corrections:
        check(section, "ocr_corrections 0 行 → 02 无〔OCR建议:〕渗入",
              OCR_MARK_02 not in text02, f"出现={OCR_MARK_02 in text02}", "False")
        check(section, "ocr_corrections 0 行 → 05 无「OCR 修正建议」栏渗入",
              OCR_MARK_05 not in text05, f"出现={OCR_MARK_05 in text05}", "False")
    else:
        check(section, f"ocr_corrections {len(corrections)} 行 → 05 出现修正建议栏",
              OCR_MARK_05 in text05, f"出现={OCR_MARK_05 in text05}", "True")
        note(f"[{event}] OCR 修正建议 {len(corrections)} 条（真实产出）")

    # -- 00_使用说明.md 导航行 / 区域标注（卡片断言） ---------------------------
    guide = (outdir / "00_使用说明.md")
    guide_text = guide.read_text(encoding="utf-8") if guide.is_file() else ""
    check(section, "00_使用说明.md 含幻灯片导航行", GUIDE_NAV_LINE in guide_text,
          f"出现={GUIDE_NAV_LINE in guide_text}", "True")
    check(section, "00_使用说明.md 自动区域标注与路径一致",
          (GUIDE_AUTO_REGION_LINE in guide_text) == auto_region_annotation,
          f"出现={GUIDE_AUTO_REGION_LINE in guide_text}", str(auto_region_annotation))
    if auto_region_annotation:
        last_line = guide_text.rstrip("\n").splitlines()[-1].strip() if guide_text.strip() else ""
        check(section, f"标注为 guide 末行（逐字 {GUIDE_AUTO_REGION_LINE!r}）",
              last_line == GUIDE_AUTO_REGION_LINE, last_line, GUIDE_AUTO_REGION_LINE)

    # -- 确定性 validate：直接调用（权威断言；控制台 stage_validate 仅记录） ----
    proc = subprocess.run(
        [sys.executable, "-B", str(WS / "core" / "scripts" / "validate_package_v3.py"),
         "--manifest", str(rundir / "manifest.json"),
         "--literal-record", str(rundir / "literal_records.jsonl"),
         "--relations", str(rundir / "relations" / "final.jsonl"),
         "--package-dir", str(outdir)],
        capture_output=True, text=True, timeout=300, cwd=str(WS))
    vreceipt = read_json(outdir / "validation_receipt.json") or {}
    check(section, "validate_package_v3 直接调用 PASS（正确 root=runs/<ev>）",
          proc.returncode == 0 and vreceipt.get("status") == "PASS",
          f"exit={proc.returncode}, receipt.status={vreceipt.get('status')} "
          f"{(proc.stdout or proc.stderr)[-200:] if proc.returncode != 0 else ''}",
          "exit=0, status=PASS")


def assert_stations(section: str, event: str, expected_stations: list[str]) -> dict:
    """14 站覆盖断言（时间线为权威——轮询会漏掉 <2s 的站，范式同 smoke_webapp）。"""
    rows = timeline_rows(event)
    stage_rows = [r.get("stage", "") for r in rows if r.get("kind") == "stage" and r.get("stage")]
    stations = {s.split(".")[0] for s in stage_rows}
    missing = [s for s in expected_stations if s not in stations]
    check(section, f"进度时间线覆盖全部 {len(expected_stations)} 站",
          not missing, f"缺失={missing or '无'}，观测={len(stations)} 站",
          f"{len(expected_stations)} 站全出现")
    check(section, "站表含新站 video_ingest 与 slide_align",
          {"video_ingest", "slide_align"} <= stations,
          f"video_ingest={'video_ingest' in stations}, slide_align={'slide_align' in stations}",
          "均出现")
    video_subs = sorted({s for s in stage_rows if s.startswith("video_ingest.")})
    check(section, "video_ingest.* 子站全部观测（probe/audio/frames/segment/ocr）",
          {"video_ingest.probe", "video_ingest.audio", "video_ingest.frames",
           "video_ingest.segment", "video_ingest.ocr"} <= set(video_subs),
          str(video_subs), "含 probe/audio/frames/segment/ocr")
    snap = read_json(RUNS / event / ".progress.json") or {}
    check(section, "最终快照 stage_total == 14", snap.get("stage_total") == 14,
          str(snap.get("stage_total")), "14")
    check(section, "最终快照 status == done", snap.get("status") == "done",
          str(snap.get("status")), "done")
    return {"stations": sorted(stations), "video_subs": video_subs, "snapshot": snap}


# --------------------------------------------------------------------------
# 路径①：WEB 场景（上传 → detect → confirm → start → 轮询 → 断言）
# --------------------------------------------------------------------------

def web_scenario(section: str, base: str, event: str, video: Path,
                 with_notes: bool, deadline_s: float) -> dict:
    info: dict = {"event": event, "started": time.time()}
    log(f"── {section}: 事件 {event}，视频 {video.name}（{video.stat().st_size / 1e6:.1f}MB）")

    # 上传（fields-before-files 契约；视频走 video 部分，notes 可选）
    files = [("video", video.name, video.read_bytes())]
    if with_notes:
        files.append(("notes", "notes.md", NOTES_TEXT.encode("utf-8")))
    payload = multipart({"event": event, "output_dir": ""}, files, "smokebnd")
    code, body = api_json(base, "/api/upload", "POST", payload,
                          {"Content-Type": "multipart/form-data; boundary=smokebnd"},
                          timeout=120)
    if not check(section, "HTTP 上传 201 且视频入列",
                 code == 201 and video.name in (body.get("video") or []),
                 f"code={code}, body={body}", f"201 且 video 含 {video.name}"):
        raise SmokeError("上传失败，场景中止")

    # awaiting_region 门禁（T7 契约顺带验收）
    code, status = api_json(base, "/api/status", timeout=15)
    qentry = next((q for q in status.get("queue", []) if q.get("name") == event), {})
    check(section, "scan_queue: 有视频无 region.json → awaiting_region",
          qentry.get("awaiting_region") is True, str(qentry), "awaiting_region=True")
    code, body = api_json(base, "/api/start", "POST", b"", timeout=15)
    check(section, "未确认区域时 /api/start 409 且列出事件",
          code == 409 and event in (body.get("awaiting_region") or []),
          f"code={code}, body={body}", f"409 且 awaiting_region 含 {event}")

    # detect：200 + reliable + IoU≥0.9（vs 生成器地面真值）+ 预览帧可取
    code, det = post_json(base, "/api/region/detect", {"event": event})
    rect = det.get("rect") if isinstance(det, dict) else None
    if not check(section, "POST /api/region/detect 200 且 reliable=true",
                 code == 200 and det.get("reliable") is True and isinstance(rect, dict),
                 f"code={code}, body={det}", "200 reliable=true rect 非空"):
        raise SmokeError("region detect 失败，场景中止")
    iou = _iou(rect, GT_PPT_RECT)
    check(section, "检测框 IoU ≥ 0.9（vs 生成器 ppt_rect 地面真值）", iou >= 0.9,
          f"IoU={iou:.4f}, rect={rect}", f"≥0.9, gt={GT_PPT_RECT}")
    note(f"[{event}] detect: confidence={det.get('confidence')}, IoU={iou:.4f}, "
         f"cached={det.get('cached')}")
    try:
        pcode, pbody = api(base, det.get("preview_url") or "/missing", timeout=30)
        check(section, "预览帧 GET 200 且为 PNG",
              pcode == 200 and pbody[:8] == b"\x89PNG\r\n\x1a\n" and len(pbody) > 1000,
              f"code={pcode}, {len(pbody)} 字节", "200 PNG >1KB")
    except OSError as exc:
        check(section, "预览帧 GET 200 且为 PNG", False, f"异常 {exc}", "200 PNG")

    # confirm：nudge 后的 rect → region.json source=user（卡片断言）
    nudged = {"x": round(rect["x"] + 0.001, 6), "y": round(rect["y"] + 0.001, 6),
              "w": round(rect["w"] - 0.002, 6), "h": round(rect["h"] - 0.002, 6)}
    code, conf = post_json(base, "/api/region/confirm", {"event": event, "rect": nudged})
    region_doc = read_json(INPUT / event / "region.json") or {}
    if not check(section, "confirm(nudged rect) → region.json source=user",
                 code == 200 and region_doc.get("source") == "user"
                 and region_doc.get("video") == video.name
                 and abs(float(region_doc.get("rect", {}).get("x", -1)) - nudged["x"]) < 1e-6,
                 f"code={code}, region.json={region_doc}", f"200 且 source=user rect≈{nudged}"):
        raise SmokeError("region confirm 失败，场景中止")

    # start + 轮询（每站打印进度行）
    code, body = api_json(base, "/api/start", "POST", b"", timeout=15)
    if not check(section, "确认后 /api/start 202", code == 202, f"code={code}, body={body}", "202"):
        raise SmokeError("start 失败，场景中止")
    log(f"  编译已开始，轮询 /api/status（deadline {deadline_s / 60:.0f} 分钟）…")
    log_offset = file_offset(PIPELINE_LOG)
    deadline = time.time() + deadline_s
    poll_started = time.time()
    last_stage, saw_main, final_status = None, False, None
    substage_samples: list[tuple[str, dict]] = []
    while time.time() < deadline:
        try:
            code, status = api_json(base, "/api/status", timeout=15)
        except (OSError, ValueError):
            time.sleep(2)
            continue
        cur = status.get("current") or {}
        if cur.get("event") == event:
            saw_main = True
            stage = cur.get("stage") or ""
            if stage != last_stage:
                last_stage = stage
                log(f"    ▶ 站 {cur.get('stage_index')}/{cur.get('stage_total')} "
                    f"{stage} — {cur.get('message', '')}")
            if status.get("substage"):
                substage_samples.append((stage, dict(status["substage"])))
            if not status.get("running") and cur.get("status") in ("done", "failed", "stopped"):
                final_status = cur.get("status")
                break
        elif not saw_main and not status.get("running") \
                and time.time() - poll_started > 60:
            break  # 管线进程已退出却始终没接手本事件（范式守卫）
        time.sleep(1.5)
    info["elapsed_s"] = round(time.time() - info["started"], 1)
    info["final_status"] = final_status
    info["pipeline_log"] = tail_new_text(PIPELINE_LOG, log_offset)
    info["substage_samples"] = substage_samples[-6:]
    if saw_main and substage_samples:
        note(f"[{event}] 轮询采样到 substage 计数器 {len(substage_samples)} 次，"
             f"末尾样例={substage_samples[-3:]}")
    if final_status is None:
        check(section, "轮询在 deadline 内到达终态", False,
              f"saw_main={saw_main}, last_stage={last_stage}", "done/failed/stopped")
        raise SmokeError("轮询超时，场景中止")
    log(f"  轮询结束: status={final_status}，耗时 {info['elapsed_s']}s")
    return info


# --------------------------------------------------------------------------
# 路径②：CLI 兜底场景（直投 input/ → run_meeting.py 本尊）
# --------------------------------------------------------------------------

def cli_scenario(section: str, event: str, video: Path, staging: Path,
                 timeout_s: float) -> dict:
    info: dict = {"event": event, "started": time.time()}
    event_dir = INPUT / event
    event_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, event_dir / "meeting.mp4")
    log(f"── {section}: 直投 {event_dir}（仅视频，无 region.json）")

    queued = sorted(p.name for p in INPUT.iterdir()) if INPUT.is_dir() else []
    if not check(section, "input/ 队列此刻仅含本 smoke 事件", queued == [event],
                 str(queued), str([event])):
        raise SmokeError("input/ 队列不干净，CLI 场景中止（防止误处理真实事件）")

    cli_log = staging / f"cli-{event}.log"
    log_offset = file_offset(PIPELINE_LOG)
    # C1: 登记子进程——main 的 finally 会兜底组终止；本函数自身的 try/finally
    # 保证任何异常路径（含 Ctrl-C）都不留活口 run_meeting（它成功后会清空
    # input/，若活过隔离恢复时刻会吞掉刚恢复的用户数据）。
    proc = register_child(subprocess.Popen(
        [sys.executable, "-B", str(WS / "run_meeting.py")],
        cwd=str(WS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True))
    deadline = time.time() + timeout_s
    lines: list[str] = []
    rc: int | None = None
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        buf = b""
        # select+os.read（而非 for-line 迭代）：子进程静默卡死时 deadline 仍然生效
        while True:
            if time.time() > deadline:
                _kill_proc_group(proc)  # SIGTERM→收割→SIGKILL→收割（全守护）
                raise SmokeError(f"CLI 运行超时（{timeout_s / 60:.0f} 分钟），日志: {cli_log}")
            ready, _, _ = select.select([fd], [], [], 2.0)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break  # EOF
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("utf-8", "replace")
                lines.append(text + "\n")
                if text.startswith("[") or "Stage" in text or "===" in text:
                    log(f"    | {text.rstrip()}")  # 实时透传每站进度行
        if buf:
            lines.append(buf.decode("utf-8", "replace"))
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            # I2: 原 os.getpgid 裸调用在进程恰好已亡时抛 ProcessLookupError——
            # 统一走全守护的 _kill_proc_group。
            _kill_proc_group(proc)
            rc = proc.wait(timeout=30)
    finally:
        # C1: 正常完成时 poll() 非 None，立即返回；Ctrl-C/崩溃/超时路径确保
        # 进程组死透并收割，绝不让 run_meeting 活过本函数。
        _kill_proc_group(proc)
        cli_log.write_text("".join(lines), encoding="utf-8")
    info["elapsed_s"] = round(time.time() - info["started"], 1)
    info["rc"] = rc
    info["pipeline_log"] = "".join(lines) + tail_new_text(PIPELINE_LOG, log_offset)
    check(section, "run_meeting.py 退出码 0", rc == 0, f"rc={rc}, 日志尾={cli_log}", "0")
    if rc != 0:
        log(f"    CLI 日志已存: {cli_log}")
    return info


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    """spec-obs-a: 真 argparse——--help 打印帮助零副作用退出 0；拼错/未知参数
    一律 usage error rc=2（argparse 标准码），绝不静默开跑一整轮真实管线
    （数据安全脚本的底线）。"""
    parser = argparse.ArgumentParser(
        prog="smoke_video_event.py",
        # spec-obs-a 核心：allow_abbrev=False——argparse 默认前缀缩写匹配会让
        # 拼错的旗标（如 --skip-ton）静默匹配到 --skip-tone 并开跑一整轮真实
        # 管线（V2 验证实测触发了该事故）；数据安全脚本必须逐字拒绝。
        allow_abbrev=False,
        description="T9 录屏全链路 LIVE 验收：真实 ffmpeg/检测/抽取/OCR/ASR/LLM，"
                    "路径① Web（TONE+VOICE 双场景）+ 路径② CLI 兜底。详见模块 docstring。",
        epilog="退出码: 0=全过（工件已清理）; 1=断言 FAIL（工件自动保留）; "
               "2=BLOCKED/用法错误（未开跑，零 smoke 副作用）; "
               "3=脚本崩溃/Ctrl-C（打印部分汇总，工件自动保留）。")
    parser.add_argument("--keep", action="store_true",
                        help="成功后也保留 smoke 工件（调试用；隔离的用户 input 事件仍会恢复）")
    parser.add_argument("--skip-tone", action="store_true",
                        help="跳过 TONE 正弦场景及其 fixture 合成（只跑 VOICE + CLI）")
    return parser.parse_args(argv)


def print_summary(title: str) -> None:
    """断言汇总表 + 环境记录 + 发现（verdict 与 崩溃/BLOCKED 部分汇总共用）。"""
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    for section_name, label, ok, actual, expected in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] ({section_name}) {label}")
        print(f"        实际: {actual}")
        if expected:
            print(f"        期望: {expected}")
    print("-" * 72)
    print("环境记录:")
    for line in ENV_NOTES or ["（无）"]:
        print(f"  · {line}")
    print("产品/已提交代码发现（未修复，仅记录）:")
    for line in FINDINGS or ["（无）"]:
        print(f"  ⚠ {line}")
    print("-" * 72)


def main() -> int:
    # spec-obs-a: 用法校验最先执行——argparse 在 staging/隔离/服务任何副作用之前
    # 就以 rc=2 拒绝未知参数；--help 同样零副作用退出 0。
    args = parse_args(sys.argv[1:])
    keep = args.keep
    skip_tone = args.skip_tone
    t0 = time.time()
    exit_code = 3  # I2: 缺省即崩溃码——任何未捕获逃逸路径都算 rc=3，绝不静默退出
    staging = Path(tempfile.mkdtemp(prefix="mst-smoke-video-"))
    quarantine_dir = RUNS / f".smoke-quarantine-{PID}"  # 工作区内隔离（原子 rename）
    global _QUARANTINE_DIR
    _QUARANTINE_DIR = quarantine_dir  # atexit / restore 兜底扫描用
    # C1 加固：SIGTERM 默认处置直接杀进程不跑 finally（V2 事故实测：孤儿
    # server+管线、搁浅隔离目录）——转 KeyboardInterrupt 走完整清理路径。
    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    server = None
    base = None
    tone_event = f"smoke-video-web-tone-{PID}"
    web_event = f"smoke-video-web-{PID}"
    cli_event = f"smoke-video-cli-{PID}"
    smoke_events = [tone_event, web_event, cli_event]
    paths_run: list[str] = []  # M2: 实跑场景动态列表（PASS 横幅不再硬编码）

    try:
        print("=" * 72)
        log("T9 录屏全链路 LIVE 验收开始（真实 ffmpeg/检测/抽取/OCR/ASR/LLM，零 mock）")
        print("=" * 72)

        # -- 0. 环境预检（BLOCKED 语义） --------------------------------------
        preflight()
        try:
            from run_meeting import STAGE_ORDER
        except Exception as exc:  # noqa: BLE001 — 任何导入失败都是环境问题
            raise SmokeError(f"无法导入 run_meeting.STAGE_ORDER: {exc}") from exc
        expected_stations = [key for key, _label in STAGE_ORDER]
        if len(expected_stations) != 14:
            raise SmokeError(f"STAGE_ORDER 应为 14 站，实际 {len(expected_stations)}")
        log(f"环境预检通过；STAGE_ORDER={expected_stations}")

        # -- 1. 隔离既有 input 条目 + 生成 fixture -----------------------------
        quarantine_input(quarantine_dir)
        fixture_log = staging / "fixtures.log"
        log("生成 T1 合成视频 fixture（tools-venv 子进程，20.0s/10fps/640x400）…")
        silent_video = staging / "silent.mp4"
        voiced_video = staging / "voiced.mp4"
        tone_video = staging / "tone.mp4"
        # M2: --skip-tone 连同 TONE fixture 合成一起跳过（诚实跳过，不白跑）；
        # silent 与 tone 生成参数逐字节一致，GT/segments 笔记改用 silent_meta。
        silent_meta = gen_synth_video(silent_video, audio=False, fixture_log=fixture_log)
        if not skip_tone:
            gen_synth_video(tone_video, audio=True, fixture_log=fixture_log)
            tone_info = ffprobe_media(tone_video)
            check("fixture", "TONE 视频含声（T1 原生正弦音轨）且 20s/640x400",
                  tone_info["has_audio"] and abs(tone_info["duration"] - 20.0) < 0.5
                  and (tone_info["width"], tone_info["height"]) == (640, 400),
                  str(tone_info), "has_audio=True duration≈20 640x400")
        speech_dur = make_speech_wav(staging / "speech.wav", fixture_log)
        mux_voiced(silent_video, staging / "speech.wav", voiced_video, fixture_log)
        voiced_info = ffprobe_media(voiced_video)
        check("fixture", "VOICE 视频含真实语音音轨（say 生成，mux 后）",
              voiced_info["has_audio"] and abs(voiced_info["duration"] - 20.0) < 0.5,
              str(voiced_info), "has_audio=True duration≈20")
        note(f"say 语音时长 {speech_dur:.1f}s（apad 至 20.0s）；"
             f"ppt_rect_px={silent_meta.get('ppt_rect_px')}, "
             f"segments={[(s['page'], s['start'], s['end']) for s in silent_meta.get('segments', [])]}")
        note(f"GT 硬编码与 tests/synth_video.py 默认参数在评审中逐项核对一致（ledger 记录，"
             f"不改）: GT_PPT_RECT={GT_PPT_RECT}, EXPECTED_PAGES={EXPECTED_PAGES}, "
             f"VIDEO_PARAMS={VIDEO_PARAMS}")

        # -- 2. 启动服务（smoke_webapp 范式：start.py 随机空闲端口） -------------
        port = free_port()
        log(f"启动 start.py（端口 {port}）…")
        server = register_child(subprocess.Popen(
            [sys.executable, "start.py", "--port", str(port), "--no-browser"],
            cwd=str(WS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True))
        base = f"http://127.0.0.1:{port}"
        if not wait_http(base, server):
            # I1: 先杀进程组，再有界读取——无响应但活着的服务不能挂死本脚本、
            # 搁浅隔离数据的恢复。
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                out, _ = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = "(server 无响应，已 SIGKILL；输出不可得)"
            raise SmokeError(f"服务未就绪: {(out or '')[-800:]}")
        log(f"服务就绪: {base}")

        # -- 3. 路径① TONE 场景（T1 原生含声视频；零记录 fail-fast 为文档化行为） --
        if not skip_tone:
            section = "路径①WEB·TONE"
            paths_run.append("路径①WEB·TONE")
            try:
                info = web_scenario(section, base, tone_event, tone_video,
                                    with_notes=True, deadline_s=18 * 60)
            except SmokeError as exc:
                check(section, "TONE 场景跑通（上传→detect→confirm→start→终态）",
                      False, str(exc), "无致命错误")
                info = {"final_status": "aborted", "pipeline_log": ""}
            note(f"[{tone_event}] 终态={info.get('final_status')}，耗时={info.get('elapsed_s')}s")
            if info.get("final_status") == "done":
                records = read_jsonl(RUNS / tone_event / "literal_records.jsonl")
                # ledger finding-4 措辞校准：门禁实况从 quality_gate_report.json 动态读取
                # （不硬编码 PASS/WARN——status ∈ {PASS, WARN, FAIL}）。
                gate = read_json(OUTPUTS / tone_event / "quality_gate_report.json") or {}
                finding(f"正弦音轨场景跑完: whisper 对 440Hz 正弦产出 {len(records)} 条记录"
                        f"（幻听内容进入包；质量门禁实测 status={gate.get('status')}——"
                        f"幻听文本未被门禁拦截）——literal_receipt.engine="
                        f"{(read_json(RUNS / tone_event / 'literal_receipt.json') or {}).get('engine')}")
                assert_stations(section, tone_event, expected_stations)
                assert_package_laws(section, tone_event, region_source="user",
                                    auto_region_annotation=False)
            elif info.get("final_status") == "failed":
                logtext = info.get("pipeline_log") or ""
                literal_missing = not (RUNS / tone_event / "literal_records.jsonl").exists()
                whisper_files = sorted((RUNS / tone_event).glob("whisper_*.json"))
                seg_total = sum(len((read_json(p) or {}).get("transcription") or [])
                                for p in whisper_files)
                zero_record = ZERO_RECORD_SIGNATURE in logtext and literal_missing
                check(section, "TONE 失败分支: 失败签名 == 文档化零记录 fail-fast",
                      zero_record,
                      f"签名匹配={ZERO_RECORD_SIGNATURE in logtext}, "
                      f"literal_records 缺失={literal_missing}, whisper 段数={seg_total}",
                      "run_meeting literal 站 fail-fast（no ASR output available）")
                if zero_record:
                    finding(f"正弦音轨（T1 audio=True, 440Hz）→ whisper 转录 {seg_total} 段 → "
                            f"literal 站 fail-fast「{ZERO_RECORD_SIGNATURE} …」。"
                            f"含义：无语音内容的含声录屏会以事件失败告终（输入保留可重试），"
                            f"而非降级为幻灯片-only 包——是否为产品缺口请协调者裁决"
                            f"（规格 §5 仅定义了无声视频 fail-fast，未定义有声无语音）。")
                else:
                    tail = "\n".join((info.get("pipeline_log") or "").splitlines()[-25:])
                    finding(f"TONE 场景以非文档化签名失败（疑似真实 bug）:\n{tail}")
            else:
                check(section, "TONE 场景到达终态", False,
                      str(info.get("final_status")), "done/failed")
            # 失败事件的 input 目录会被保留（重试语义）——移走防止污染下一场景队列
            if (INPUT / tone_event).is_dir():
                shutil.rmtree(INPUT / tone_event, ignore_errors=True)
                log(f"  已清理 TONE 事件残留 input/{tone_event}（runs/ 保留供取证）")
        else:
            note("--skip-tone: 跳过 TONE 正弦场景")

        # -- 4. 路径① VOICE 场景（权威全断言跑） --------------------------------
        section = "路径①WEB·VOICE"
        paths_run.append("路径①WEB·VOICE")
        voice_done = False
        try:
            info = web_scenario(section, base, web_event, voiced_video,
                                with_notes=True, deadline_s=20 * 60)
            if info.get("final_status") == "done":
                voice_done = True
                note(f"[{web_event}] 终态=done，耗时={info['elapsed_s']}s；"
                     f"literal_receipt.engine="
                     f"{(read_json(RUNS / web_event / 'literal_receipt.json') or {}).get('engine')}")
            else:
                check(section, "VOICE 事件终态 == done", False,
                      f"{info.get('final_status')}｜日志尾: "
                      + " ⏎ ".join((info.get("pipeline_log") or "").splitlines()[-15:]),
                      "done")
        except SmokeError as exc:
            check(section, "VOICE 场景跑通（上传→detect→confirm→start→终态）",
                  False, str(exc), "无致命错误")
        if voice_done:
            assert_stations(section, web_event, expected_stations)
            assert_package_laws(section, web_event, region_source="user",
                                auto_region_annotation=False)
            vline = grep_validate_line(info.get("pipeline_log") or "")
            note(f"[{web_event}] 控制台 stage_validate 实况（既有冻结怪异 root="
                 f"仓库根，仅记录）: {vline}")
            finding(f"run_meeting.py:1636-1643 stage_validate 以 package_dir.parent.parent"
                    f"（=仓库根）拼 --manifest/--literal-record/--relations，控制台内置"
                    f"验证永远对不上真实工件（本跑实况: {vline}）。协调者已裁定冻结不修；"
                    f"权威 validate 断言以本脚本直接调用（正确 root=runs/<ev>）为准。")

        # -- 5. 路径② CLI 兜底 --------------------------------------------------
        section = "路径②CLI兜底"
        paths_run.append("路径②CLI兜底")
        if (INPUT / web_event).is_dir():  # VOICE 失败残留时防队列污染
            shutil.rmtree(INPUT / web_event, ignore_errors=True)
        try:
            info = cli_scenario(section, cli_event, voiced_video, staging,
                                timeout_s=25 * 60)
        except SmokeError as exc:
            check(section, "CLI 场景跑通（直投→run_meeting→终态）",
                  False, str(exc), "无致命错误")
            info = {"rc": None}
        note(f"[{cli_event}] rc={info.get('rc')}，耗时={info.get('elapsed_s')}s")
        if info.get("rc") == 0:
            receipt = read_json(RUNS / cli_event / "video" / "ingest_receipt.json") or {}
            check(section, "ingest_receipt.warnings 含 region_auto_not_user_confirmed",
                  "region_auto_not_user_confirmed" in (receipt.get("warnings") or []),
                  str(receipt.get("warnings")), "含 region_auto_not_user_confirmed")
            auto_region = read_json(RUNS / cli_event / "video" / "region.json") or {}
            check(section, "runs/<ev>/video/region.json 自动兜底区域 source=auto",
                  auto_region.get("source") == "auto"
                  and auto_region.get("video") == "meeting.mp4"
                  and isinstance(auto_region.get("rect"), dict),
                  str(auto_region), "source=auto, video=meeting.mp4, rect 非空")
            check(section, "detect_receipt.json 存在且 reliable=true（CLI 兜底检测实跑）",
                  (read_json(RUNS / cli_event / "video" / "detect_receipt.json") or {}).get("reliable") is True,
                  str(read_json(RUNS / cli_event / "video" / "detect_receipt.json")),
                  "reliable=true")
            check(section, "成功后 input/<ev> 已被 cleanup_input 清空（既有契约）",
                  not (INPUT / cli_event).exists(), str(INPUT / cli_event), "不存在")
            assert_package_laws(section, cli_event, region_source="auto",
                                auto_region_annotation=True)
            note(f"[{cli_event}] 控制台 stage_validate 实况: "
                 f"{grep_validate_line(info.get('pipeline_log') or '')}")
            note(f"[{cli_event}] OCR 路线: {ocr_route_summary(cli_event)}")

        # -- 判定 ----------------------------------------------------------------
        failed = [r for r in RESULTS if not r[2]]
        exit_code = 1 if failed else 0
        print_summary("T9 SMOKE 断言汇总" if not failed
                      else f"T9 SMOKE 断言汇总（FAIL {len(failed)} 项，工件自动保留）")
        if failed:
            log(f"SMOKE FAIL — {len(failed)}/{len(RESULTS)} 项未过"
                f"（{' + '.join(paths_run)}），总耗时 {time.time() - t0:.0f}s；"
                f"工件自动保留（路径见尾部日志）")
        else:
            # M2: 横幅动态列出实跑场景（--skip-tone 时不再谎称「WEB 双场景」）
            log(f"SMOKE PASS — {len(RESULTS)} 项断言全过（{' + '.join(paths_run)}），"
                f"总耗时 {time.time() - t0:.0f}s")
        return exit_code

    except SmokeError as exc:
        exit_code = 2
        print("\n" + "=" * 72)
        log(f"SMOKE BLOCKED — {exc}")
        if RESULTS:
            failed = [r for r in RESULTS if not r[2]]
            print_summary(f"T9 SMOKE 部分断言汇总（BLOCKED 中止，已累计 {len(RESULTS)} 项，"
                          f"FAIL {len(failed)} 项；rc=2 非断言判定）")
        else:
            for line in ENV_NOTES:
                print(f"  · {line}")
        return 2
    except KeyboardInterrupt:
        # I2: Ctrl-C/SIGTERM 是崩溃（rc=3），绝不伪装成断言 FAIL；finally 会按
        # C1 顺序终止全部子进程组与 pidfile 管线组并恢复 input/。
        exit_code = 3
        log("SMOKE CRASH — Ctrl-C/SIGTERM 中断（rc=3；子进程按 C1 顺序终止，input/ 已恢复）")
        print_summary("T9 SMOKE 部分断言汇总（Ctrl-C/SIGTERM 中止；rc=3 非断言判定）")
        return 3
    except Exception as exc:  # noqa: BLE001 — I2: 崩溃绝不伪装成 FAIL、也绝不静默消失
        exit_code = 3
        traceback.print_exc()
        print_summary(f"T9 SMOKE 部分断言汇总（脚本崩溃: {type(exc).__name__}: {exc}；"
                      f"rc=3 非断言判定）")
        return 3
    finally:
        # C1 顺序铁律：礼貌 /api/stop → 终止全部登记子进程组（server、CLI
        # run_meeting，含收割）→ 若且仅若本脚本的 server 曾启动（N1 gate），
        # 终止 pidfile 管线组并确认死亡 → 然后才恢复隔离的 input 条目。活的
        # run_meeting 成功后会清空 input/，绝不允许它活过恢复时刻（run-#1
        # 事故类的最后一块拼图：Ctrl-C/SIGTERM/崩溃孤儿）。server 从未启动时
        # 任何 pidfile 都属于用户管线，本脚本无权杀它（见下方 N1 不变量注释）。
        # SIGTERM 处理器保持就位到最后一刻：自杀式误伤已由 _group_kill 同组
        # 降级挡住；外部 SIGTERM 转 KeyboardInterrupt → atexit restore 兜底
        # （幂等）——比 SIG_DFL 直杀跳过恢复安全（V3 事故实证）。
        try:
            if base is not None and server is not None and server.poll() is None:
                try:
                    _code, st = api_json(base, "/api/status", timeout=5)
                    if st.get("running"):
                        api_json(base, "/api/stop", "POST", b"", timeout=10)
                        end = time.time() + 25
                        while time.time() < end:
                            _code, st = api_json(base, "/api/status", timeout=5)
                            if not st.get("running"):
                                break
                            time.sleep(1)
                except (OSError, ValueError):
                    pass
            kill_registered_children()    # (a) server + CLI run_meeting
            # N1 不变量：只有本脚本亲手启动的 server 才可能合法写出本脚本可杀的
            # pidfile。server is None（如 preflight 因「用户管线正在运行」拒跑）
            # 时，runs/.webapp/pipeline.pid 里的任何活 pid 都是用户管线——绝不
            # 触碰（否则恰好杀掉拒跑所要保护的对象，还有 PID 复用误杀窗口）。
            if server is not None:
                kill_pipeline_from_pidfile()  # (b) 我们的 server 拉起的管线组（确认死亡）
        finally:
            # 顺序铁律：先把隔离的既有 input 条目恢复回原位（此刻所有本脚本
            # 拉起的进程已死），再决定工件去留。
            unrestored = restore_input(quarantine_dir)
            if unrestored:
                log(f"❌ {len(unrestored)} 个隔离条目未恢复 → 隔离目录保留待手工处理: "
                    f"{quarantine_dir}")
            else:
                shutil.rmtree(quarantine_dir, ignore_errors=True)
            # M3: rc≠0（FAIL/BLOCKED/崩溃）自动保留工件供诊断并打印路径；
            # 全绿且未 --keep 才清理。（隔离目录除外：恢复成功即销毁，用户
            # 数据不做诊断留存。）
            if exit_code == 0 and not keep:
                for event in smoke_events:
                    for root in (INPUT / event, RUNS / event, OUTPUTS / event):
                        if root.is_dir():
                            shutil.rmtree(root, ignore_errors=True)
                shutil.rmtree(staging, ignore_errors=True)
            else:
                survivors = [str(p) for p in [staging] +
                             [d / e for e in smoke_events for d in (INPUT, RUNS, OUTPUTS)]
                             if p.is_dir()]
                reason = "--keep" if (keep and exit_code == 0) else f"exit_code={exit_code} 自动保留"
                log(f"工件保留（{reason}）: {survivors}")


if __name__ == "__main__":
    raise SystemExit(main())
