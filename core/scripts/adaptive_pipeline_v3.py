#!/usr/bin/env python3
"""Adaptive local controller for the v3 meeting workflow.

The controller makes only deterministic process/resource decisions. It never
interprets meeting content. Heavy model stages are exclusive; light stages may
run concurrently when measured memory headroom permits. Ambiguous failures are
written to a compact supervisor packet for the downstream supervisor instead of being looped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command_output(command: list[str], timeout: int = 5) -> str:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout, check=False)
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def resource_snapshot() -> dict[str, Any]:
    total_raw = command_output(["sysctl", "-n", "hw.memsize"])
    total_gb = int(total_raw) / 1024 ** 3 if total_raw.isdigit() else None
    vm = command_output(["vm_stat"])
    page_match = re.search(r"page size of (\d+) bytes", vm)
    page_size = int(page_match.group(1)) if page_match else 16384
    pages = {name: int(value.replace(".", "")) for name, value in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+\.)", vm)}
    available_pages = sum(pages.get(name, 0) for name in ("free", "inactive", "speculative", "purgeable"))
    available_gb = available_pages * page_size / 1024 ** 3 if vm else None
    swap = command_output(["sysctl", "-n", "vm.swapusage"])
    swap_used = re.search(r"used = ([0-9.]+)([MG])", swap)
    swap_used_gb = None
    if swap_used:
        swap_used_gb = float(swap_used.group(1)) / (1024 if swap_used.group(2) == "M" else 1)
    load = os.getloadavg()
    return {
        "captured_at": now_iso(), "total_memory_gb": round(total_gb, 3) if total_gb else None,
        "available_memory_gb": round(available_gb, 3) if available_gb is not None else None,
        "swap_used_gb": round(swap_used_gb, 3) if swap_used_gb is not None else None,
        "load_average": [round(value, 3) for value in load],
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def alive(pid: int) -> bool:
    # Reap children owned by this controller.  Without this, a finished child
    # can remain a zombie and os.kill(pid, 0) incorrectly reports it as alive.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        # After a controller restart the process is no longer our child.
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def newest_mtime(run_dir: Path, definition: dict[str, Any], started_at: float) -> float:
    values = [started_at]
    for pattern in definition.get("progress_globs") or []:
        for path in run_dir.glob(pattern):
            try:
                values.append(path.stat().st_mtime)
            except OSError:
                pass
    log = definition.get("active_log")
    if log:
        path = run_dir / log
        if path.exists():
            values.append(path.stat().st_mtime)
    return max(values)


def validate_outputs(run_dir: Path, definition: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for item in definition.get("artifacts") or []:
        path = run_dir / item["path"]
        if not path.is_file() or path.stat().st_size < int(item.get("min_bytes", 1)):
            errors.append(f"missing_or_small:{item['path']}")
            continue
        if item.get("type") == "json":
            try:
                payload = load_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError):
                errors.append(f"invalid_json:{item['path']}")
                continue
            expected = item.get("status_in")
            if expected and payload.get("status") not in expected:
                errors.append(f"bad_status:{item['path']}:{payload.get('status')}")
    return errors


def has_commit_marker(definition: dict[str, Any]) -> bool:
    """Return true only when the stage defines an atomic completion marker."""
    return any(bool(item.get("commit_marker")) for item in definition.get("artifacts") or [])


def expand(command: list[str], values: dict[str, str]) -> list[str]:
    return [part.format(**values) for part in command]


def packet(run_dir: Path, state: dict[str, Any], status: str, stage: str | None, reasons: list[str]) -> None:
    atomic_json(run_dir / "supervisor_packet.json", {
        "schema_version": 3, "created_at": now_iso(), "status": status, "stage": stage,
        "failed_checks": reasons, "resource_snapshot": resource_snapshot(),
        "attempts": (state.get("stages", {}).get(stage, {}) if stage else {}).get("attempts", [])[-2:],
        "bounded_evidence": [], "content_included": False,
    })


def initial_state(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 3, "created_at": now_iso(), "updated_at": now_iso(),
        "controller_pid": os.getpid(), "status": "running", "run_dir": str(run_dir),
        "stages": {stage["id"]: {"status": "pending", "attempts": []} for stage in config["stages"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    config = load_json(args.config)
    definitions = {stage["id"]: stage for stage in config["stages"]}
    state_path = args.run_dir / "adaptive_state.json"
    state = load_json(state_path) if state_path.exists() else initial_state(config, args.run_dir)
    previous_controller = state.get("controller_pid")
    state["controller_pid"] = os.getpid()
    state["updated_at"] = now_iso()
    if previous_controller and previous_controller != os.getpid():
        for stage_id, stage_state in state["stages"].items():
            if stage_state.get("status") == "running":
                definition = definitions[stage_id]
                errors = validate_outputs(args.run_dir, definition)
                if not errors and has_commit_marker(definition):
                    stage_state["status"] = "passed"
                else:
                    stage_state["status"] = "needs_supervisor"
                    stage_state["last_error"] = "controller_restarted_while_stage_running"
    atomic_json(state_path, state)
    values = {
        "run_dir": str(args.run_dir.resolve()), "source_root": str(args.source_root.resolve()),
        "script_dir": str(Path(__file__).resolve().parent), "python": sys.executable,
        # scripts/ -> meeting-records-audit/ -> work/ -> project root
        "workspace_root": str(Path(__file__).resolve().parents[3]),
    }
    max_light = int(config.get("max_parallel_light", 2))
    reserve = float(config.get("reserve_memory_gb", 2.5))
    max_attempts = int(config.get("max_attempts", 2))
    max_resource_wait = int(config.get("max_resource_wait_seconds", 1800))

    while True:
        state = load_json(state_path)
        changed = False
        running_ids = [stage_id for stage_id, stage_state in state["stages"].items() if stage_state.get("status") == "running"]
        for stage_id in running_ids:
            stage_state = state["stages"][stage_id]
            definition = definitions[stage_id]
            pid = int(stage_state["pid"])
            started_epoch = float(stage_state["started_epoch"])
            output_errors = validate_outputs(args.run_dir, definition)
            # A stage receipt is the commit marker.  Check it before probing the
            # PID because a completed child may briefly remain as a zombie and
            # still answer os.kill(pid, 0).  This also makes controller restarts
            # idempotent: a committed stage is never rerun merely because its
            # former PID can no longer be reaped by the new controller.
            if not output_errors and has_commit_marker(definition):
                attempt = stage_state["attempts"][-1]
                attempt["ended_at"] = now_iso()
                attempt["validation_errors"] = []
                stage_state["status"] = "passed"
                stage_state.pop("last_error", None)
                changed = True
                continue
            if not alive(pid):
                attempt = stage_state["attempts"][-1]
                attempt["ended_at"] = now_iso()
                attempt["validation_errors"] = output_errors
                if not output_errors:
                    stage_state["status"] = "passed"
                    stage_state.pop("last_error", None)
                elif len(stage_state["attempts"]) < max_attempts:
                    stage_state["status"] = "pending"
                    stage_state["last_error"] = ";".join(output_errors)
                else:
                    stage_state["status"] = "needs_supervisor"
                    stage_state["last_error"] = ";".join(output_errors)
                changed = True
                continue
            age = time.time() - started_epoch
            stale = time.time() - newest_mtime(args.run_dir, definition, started_epoch)
            timeout = int(definition.get("timeout_seconds", 21600))
            stale_limit = int(definition.get("stale_seconds", 1800))
            if age > timeout or stale > stale_limit:
                # Only kill a process group created by this controller instance.
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                reason = "stage_timeout" if age > timeout else "checkpoint_stale"
                stage_state["attempts"][-1].update(ended_at=now_iso(), error=reason)
                if len(stage_state["attempts"]) < max_attempts:
                    stage_state["status"] = "pending"
                else:
                    stage_state["status"] = "needs_supervisor"
                stage_state["last_error"] = reason
                changed = True

        blocked = [stage_id for stage_id, stage_state in state["stages"].items() if stage_state.get("status") == "needs_supervisor"]
        if blocked:
            state["status"] = "needs_supervisor"
            state["updated_at"] = now_iso()
            atomic_json(state_path, state)
            packet(args.run_dir, state, "NEEDS_SUPERVISOR", blocked[0], [state["stages"][blocked[0]].get("last_error", "unknown")])
            return 2

        snapshot = resource_snapshot()
        running_ids = [stage_id for stage_id, stage_state in state["stages"].items() if stage_state.get("status") == "running"]
        model_running = any(definitions[stage_id].get("resource_class") == "model" for stage_id in running_ids)
        light_running = sum(definitions[stage_id].get("resource_class", "light") == "light" for stage_id in running_ids)
        ready: list[str] = []
        for stage_id, definition in definitions.items():
            stage_state = state["stages"][stage_id]
            if stage_state.get("status") != "pending":
                continue
            if all(state["stages"][dependency]["status"] == "passed" for dependency in definition.get("depends_on") or []):
                ready.append(stage_id)
        launched = 0
        for stage_id in ready:
            definition = definitions[stage_id]
            kind = definition.get("resource_class", "light")
            if kind == "model" and (model_running or running_ids):
                continue
            if kind == "light" and (model_running or light_running >= max_light):
                continue
            available = snapshot.get("available_memory_gb")
            estimate = float(definition.get("estimated_memory_gb", 0.5))
            if available is not None and available < estimate + reserve:
                continue
            stage_state = state["stages"][stage_id]
            attempt_number = len(stage_state["attempts"]) + 1
            log_path = args.run_dir / "logs" / f"{stage_id}.attempt-{attempt_number}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command = expand(definition["command"], values)
            log = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(command, cwd=args.run_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            log.close()
            started_epoch = time.time()
            stage_state.update(status="running", pid=process.pid, started_epoch=started_epoch, log=str(log_path.relative_to(args.run_dir)))
            stage_state["attempts"].append({"number": attempt_number, "started_at": now_iso(), "command": command, "pid": process.pid})
            running_ids.append(stage_id)
            if kind == "model":
                model_running = True
            else:
                light_running += 1
            changed = True
            launched += 1

        # Do not wait forever when a configured memory estimate can never fit.
        # Ordinary dependency/model serialization does not enter this branch
        # because at least one stage is already running in that case.
        if ready and not running_ids and launched == 0:
            waiting_since = float(state.get("resource_wait_since_epoch") or time.time())
            state["resource_wait_since_epoch"] = waiting_since
            state["resource_wait_since"] = dt.datetime.fromtimestamp(waiting_since, dt.timezone.utc).astimezone().isoformat(timespec="seconds")
            if time.time() - waiting_since > max_resource_wait:
                state["status"] = "needs_supervisor"
                state["updated_at"] = now_iso()
                atomic_json(state_path, state)
                packet(args.run_dir, state, "NEEDS_SUPERVISOR", ready[0], ["insufficient_resource_headroom"])
                return 2
        else:
            state.pop("resource_wait_since_epoch", None)
            state.pop("resource_wait_since", None)

        if all(stage_state.get("status") == "passed" for stage_state in state["stages"].values()):
            state["status"] = "complete"
            state["updated_at"] = now_iso()
            state["resource_snapshot"] = snapshot
            atomic_json(state_path, state)
            packet(args.run_dir, state, "COMPLETE", None, [])
            print(state_path)
            return 0
        state["status"] = "running"
        state["updated_at"] = now_iso()
        state["resource_snapshot"] = snapshot
        atomic_json(state_path, state)
        if args.once:
            print(state_path)
            return 0
        time.sleep(max(2, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
