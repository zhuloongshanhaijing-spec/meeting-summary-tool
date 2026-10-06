#!/usr/bin/env python3
"""End-to-end smoke for the web console (plan T6, docs/web-console-design.md §6).

Real-model chain on the synthetic demo event:
  make_demo_event -> HTTP upload (audio + 2 notes) -> /api/start -> poll
  /api/status until done -> assert stages/subcounters/outputs -> zip download
  -> second event stopped mid-run -> assert inputs retained + stopped snapshot.

Run: python3 scripts/smoke_webapp.py [--keep]   (~4-6 min, fully local)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

INPUT = WS / "input"
OUTPUTS = WS / "outputs"
RUNS = WS / "runs"
EVENT_PREFIX = "smoke"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(base: str, path: str, method: str = "GET", data: bytes | None = None,
        headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def api_json(base: str, path: str, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, timeout: int = 30):
    code, body = api(base, path, method=method, data=data, headers=headers,
                     timeout=timeout)
    return code, json.loads(body.decode("utf-8"))


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
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(base: str, proc: subprocess.Popen, deadline_s: float = 20) -> bool:
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


def main() -> int:
    keep = "--keep" in sys.argv
    failures: list[str] = []
    base = None
    server = None
    staging = Path(tempfile.mkdtemp(prefix="mst-smoke-"))
    stop_event = f"{EVENT_PREFIX}-stop-{int(time.time())}"
    main_event = f"{EVENT_PREFIX}-main-{int(time.time())}"

    try:
        # -- 1. materials: synthetic demo event, notes split in two ---------
        log("生成合成 demo 事件材料…")
        if subprocess.run([sys.executable, "scripts/make_demo_event.py"], cwd=WS,
                          capture_output=True, text=True).returncode != 0:
            raise RuntimeError("make_demo_event.py failed")
        src = INPUT / "demo-event"
        audio_files = sorted(p for p in src.iterdir() if p.suffix == ".wav")
        if not audio_files:
            raise RuntimeError("demo generator produced no wav")
        # copy BEFORE rmtree: upload payloads read from staging, not the
        # already-deleted source dir
        for af in audio_files:
            shutil.copy2(af, staging / af.name)
        notes_text = (src / "notes.md").read_text(encoding="utf-8")
        half = len(notes_text) // 2
        (staging / "notes-a.md").write_text(notes_text[:half], encoding="utf-8")
        (staging / "notes-b.md").write_text(notes_text[half:], encoding="utf-8")
        shutil.rmtree(src)
        audio_main = staging / audio_files[0].name

        # -- 2. boot the real entry point -----------------------------------
        port = free_port()
        log(f"启动 start.py（端口 {port}）…")
        server = subprocess.Popen(
            [sys.executable, "start.py", "--port", str(port), "--no-browser"],
            cwd=WS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        base = f"http://127.0.0.1:{port}"
        if not wait_http(base, server):
            out = server.stdout.read() if server.stdout else ""
            raise RuntimeError(f"server did not come up: {out[-500:]}")
        log("服务就绪")

        # -- 2.5 new-surface checks (no models needed) -----------------------
        log("新面自检：依赖字段 / 装配面 / 治理器快照…")
        code, deps = api_json(base, "/api/dependencies")
        if code != 200:
            failures.append(f"/api/dependencies -> {code}")
        else:
            for item in deps.get("items", []):
                for key in ("detail", "consequence", "required", "official_url", "purpose"):
                    if key not in item:
                        failures.append(f"dependency item {item.get('id')} missing {key}")
        code, install_log = api_json(base, "/api/dependencies/install-log")
        if code != 200 or not {"installing", "ids", "error", "tail"} <= set(install_log):
            failures.append(f"/api/dependencies/install-log shape wrong: {install_log}")
        code, asm = api_json(base, "/api/assembly")
        if code != 200 or "status" not in asm:
            failures.append(f"/api/assembly -> {code} {asm}")
        code, payload = api_json(base, "/api/assembly/build", "POST", b"")
        has_fragments = RUNS.exists() and any(
            p.is_dir() and p.name.startswith("fragment-") for p in RUNS.iterdir())
        if not has_fragments and code != 409:
            failures.append(f"assembly/build without fragments should 409, got {code}")
        code, status0 = api_json(base, "/api/status")
        if "governor" not in status0:
            failures.append("/api/status missing governor snapshot field")
        if "assembly" not in status0:
            failures.append("/api/status missing assembly field")
        log("新面自检完成")

        # -- 3. upload via HTTP ---------------------------------------------
        payload = multipart(
            {"event": main_event, "output_dir": ""},
            [("audio", audio_main.name, audio_main.read_bytes()),
             ("notes", "notes-a.md", (staging / "notes-a.md").read_bytes()),
             ("notes", "notes-b.md", (staging / "notes-b.md").read_bytes())],
            "smokebnd")
        code, body = api_json(base, "/api/upload", "POST", payload, {
            "Content-Type": "multipart/form-data; boundary=smokebnd"})
        if code != 201:
            failures.append(f"upload failed: {code} {body}")
            raise RuntimeError("upload failed")
        if not (INPUT / main_event / "notes.md").exists():
            failures.append("merged notes.md missing after upload")
        log(f"上传完成: {body}")

        # -- 4. start + poll -------------------------------------------------
        code, body = api_json(base, "/api/start", "POST", b"")
        if code != 202:
            failures.append(f"start failed: {code} {body}")
            raise RuntimeError("start failed")
        log("编译已开始，轮询进度…")

        stages_seen: set[str] = set()
        substage_samples: list[dict] = []
        deadline = time.time() + 8 * 60
        started_at = time.time()
        saw_main = False
        status = None
        while time.time() < deadline:
            code, status = api_json(base, "/api/status", timeout=10)
            cur = status.get("current") or {}
            # only trust snapshot rows naming OUR event: /api/status also
            # surfaces the latest event from history, which may be stale
            if cur.get("event") == main_event:
                saw_main = True
                if cur.get("stage"):
                    stages_seen.add(cur["stage"].split(".")[0])
                if not status.get("running") and cur.get("status") in (
                        "done", "failed", "stopped"):
                    break
            if status.get("substage"):
                substage_samples.append(status["substage"])
            if not saw_main and time.time() - started_at > 90 \
                    and not status.get("running"):
                break  # pipeline never picked the event up
            time.sleep(2)

        cur = (status or {}).get("current") or {}
        log(f"轮询结束: 状态={cur.get('status')}，轮询采样阶段={sorted(stages_seen)}")
        if cur.get("status") != "done":
            failures.append(f"main event status={cur.get('status')} (want done)")
            if cur.get("status") == "failed":
                code, lg = api_json(base, "/api/logs?what=pipeline&tail=15")
                for line in lg.get("lines", [])[-15:]:
                    log(f"  | {line}")

        # authoritative stage coverage from the timeline (2s polling misses
        # sub-2s stages on the 39s demo event; emit_progress writes every
        # transition to progress.jsonl)
        expected_stages = {"inventory", "audio_prepare", "lang_probe", "asr",
                           "literal", "evidence", "relevance", "reconcile",
                           "audit", "notes", "package", "validate"}
        code, lg = api_json(base, "/api/logs?what=progress&tail=800")
        tl_stages = {r.get("stage", "").split(".")[0]
                     for r in lg.get("lines", [])
                     if r.get("event") == main_event
                     and r.get("kind") == "stage" and r.get("stage")}
        missing = expected_stages - tl_stages
        if missing:
            failures.append(f"timeline missing stages: {sorted(missing)} "
                            f"(saw {sorted(tl_stages)})")
        if not any(s.get("total", 0) > 0 for s in substage_samples):
            failures.append(f"no substage counters with total>0 seen: {substage_samples[-5:]}")

        # -- 5. outputs + zip -------------------------------------------------
        outdir = OUTPUTS / main_event
        if not outdir.is_dir():
            failures.append("outputs/<event> missing")
        else:
            names = [p.name for p in outdir.iterdir()]
            mds = [n for n in names if n.endswith(".md")]
            if not mds:
                failures.append(f"no markdown reports in output: {names}")
            if "meeting.db" not in names:
                failures.append(f"meeting.db missing: {names}")
            code, zbody = api(base, f"/api/outputs/{main_event}/zip", timeout=60)
            if code != 200:
                failures.append("zip route failed")
            else:
                try:
                    zf = zipfile.ZipFile(io.BytesIO(zbody))
                    if len(zf.namelist()) < 3:
                        failures.append(f"zip suspiciously small: {zf.namelist()}")
                except zipfile.BadZipFile:
                    failures.append("zip body not a valid zip")

        # -- 5.5 audio Range streaming (junction audition backend) -----------
        src_dir = RUNS / main_event / "source"
        wavs = sorted(src_dir.glob("*.wav")) if src_dir.is_dir() else []
        if not wavs:
            failures.append(f"no source audio under {src_dir} for audio route")
        else:
            import urllib.error
            url = (base + "/api/audio/" + urllib.request.quote(main_event)
                   + "/" + urllib.request.quote(wavs[0].name))
            req = urllib.request.Request(url, headers={"Range": "bytes=0-99"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                    cr = resp.headers.get("Content-Range", "")
                if resp.status != 206 or len(body) != 100 or "/".encode() not in cr.encode():
                    failures.append(f"audio Range: status={resp.status} len={len(body)} cr={cr!r}")
            except urllib.error.HTTPError as exc:
                failures.append(f"audio Range request failed: {exc.code}")
            # traversal defense
            bad = base + "/api/audio/" + urllib.request.quote(main_event) + "/..%2F..%2Fconfig.json"
            try:
                with urllib.request.urlopen(bad, timeout=10) as resp:
                    if resp.status == 200:
                        failures.append("audio route traversal NOT blocked")
            except urllib.error.HTTPError:
                pass  # 4xx is the expectation

        # -- 6. stop semantics on a second event -------------------------------
        log("第二事件：验证停止语义…")
        payload = multipart({"event": stop_event},
                            [("audio", audio_main.name, audio_main.read_bytes())],
                            "smokebnd")
        code, body = api_json(base, "/api/upload", "POST", payload, {
            "Content-Type": "multipart/form-data; boundary=smokebnd"})
        if code != 201:
            failures.append(f"second upload failed: {code} {body}")
        else:
            code, _ = api_json(base, "/api/start", "POST", b"")
            if code != 202:
                failures.append(f"second start failed: {code}")
            else:
                # wait until the SECOND event is genuinely current+running
                # (pidfile alone is true during the spawn gap, when
                # current_event_state still names the previous done event)
                became_current = False
                end = time.time() + 60
                while time.time() < end:
                    code, st = api_json(base, "/api/status", timeout=10)
                    cur2 = st.get("current") or {}
                    if st.get("running") and cur2.get("event") == stop_event:
                        became_current = True
                        break
                    time.sleep(1)
                if not became_current:
                    failures.append("second event never became current+running")
                code, _ = api_json(base, "/api/stop", "POST", b"")
                if code != 202:
                    failures.append(f"stop returned {code}")
                time.sleep(1.5)
                code, st = api_json(base, "/api/status", timeout=10)
                if st.get("running"):
                    failures.append("still running after stop")
                snap = RUNS / stop_event / ".progress.json"
                if not snap.exists() or json.loads(
                        snap.read_text(encoding="utf-8")).get("status") != "stopped":
                    failures.append("stopped snapshot missing/wrong")
                if not any((INPUT / stop_event).iterdir()):
                    failures.append("stopped event inputs were wiped (must be retained)")
                # regression (spawn-gap stamping bug): the DONE main event
                # must keep its final state, not be turned "stopped"
                main_snap = RUNS / main_event / ".progress.json"
                if main_snap.exists():
                    main_status = json.loads(
                        main_snap.read_text(encoding="utf-8")).get("status")
                    if main_status != "done":
                        failures.append(f"main event state corrupted: {main_status}")

        # -- verdict -----------------------------------------------------------
        print("\n" + "=" * 60)
        if failures:
            log(f"SMOKE FAIL — {len(failures)} 项未过:")
            for f in failures:
                print(f"  ✗ {f}")
            return 1
        log("SMOKE PASS — 上传/进度/输出/zip/停止 全链路通过")
        return 0
    finally:
        if server and server.poll() is None:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
        if not keep:
            shutil.rmtree(staging, ignore_errors=True)
            for d in (INPUT / main_event, INPUT / stop_event, INPUT / "demo-event"):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            # smoke artifacts out of the user's real outputs/runs namespaces
            for d in (OUTPUTS / main_event, RUNS / main_event,
                      RUNS / stop_event, RUNS / "demo-event"):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
