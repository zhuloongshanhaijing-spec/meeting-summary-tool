#!/usr/bin/env python3
"""Reliable controller for local-first meeting record pipelines.

This controller owns metadata, state, validation, retries, compact supervisor
packets, and safe archive planning. Specialist adapters remain ordinary command
arrays in config.json, so a failed model cannot mutate controller state directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_STAGES = [
    "inventory",
    "audio_prepare",
    "audio_segment",
    "asr_primary",
    "asr_secondary",
    "image_prepare",
    "ocr_primary",
    "ocr_secondary",
    "difficult_media",
    "source_align",
    "unit_extract",
    "coverage_audit",
    "render",
    "archive",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".caf"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
NOTE_EXTENSIONS = {".txt", ".md", ".pdf", ".doc", ".docx", ".ppt", ".pptx"}
BAD_TEXT_PATTERNS = ("\ufffd", "\\u0000")


class PipelineError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read valid JSON: {path}: {exc}") from exc


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def classify(path: Path) -> str:
    # region.json is the per-event slide-region input config (design §4.1):
    # registered for provenance but never an eligible source (never moved).
    if path.name == "region.json":
        return "config"
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in NOTE_EXTENSIONS:
        return "note"
    return "other"


def run_probe(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def image_metadata(path: Path) -> dict[str, Any]:
    if not shutil.which("sips"):
        return {}
    code, output = run_probe(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
    if code != 0:
        return {"probe_error": output.strip()[:500]}
    width = re.search(r"pixelWidth:\s*(\d+)", output)
    height = re.search(r"pixelHeight:\s*(\d+)", output)
    return {
        "width": int(width.group(1)) if width else None,
        "height": int(height.group(1)) if height else None,
    }


def audio_metadata(path: Path) -> dict[str, Any]:
    if not shutil.which("afinfo"):
        return {}
    code, output = run_probe(["afinfo", str(path)], timeout=60)
    if code != 0:
        return {"probe_error": output.strip()[:500]}
    duration = re.search(r"estimated duration:\s*([0-9.]+) sec", output)
    channels = re.search(r"Data format:\s+(\d+) ch", output)
    sample_rate = re.search(r"Data format:.*?([0-9]+) Hz", output)
    return {
        "duration_seconds": float(duration.group(1)) if duration else None,
        "channels": int(channels.group(1)) if channels else None,
        "sample_rate_hz": int(sample_rate.group(1)) if sample_rate else None,
    }


def video_metadata(path: Path) -> dict[str, Any]:
    """Probe a video file with ffprobe; never raise.

    Missing ffprobe or malformed output degrades every field to None so a
    corrupt/odd video can never crash inventory (design §3.7/§5).
    """
    metadata: dict[str, Any] = {
        "duration_seconds": None,
        "has_audio": None,
        "width": None,
        "height": None,
    }
    if not shutil.which("ffprobe"):
        return metadata
    code, output = run_probe(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path),
        ],
        timeout=60,
    )
    if code != 0:
        metadata["probe_error"] = output.strip()[:500]
        return metadata
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        metadata["probe_error"] = "ffprobe output is not valid JSON"
        return metadata
    streams = payload.get("streams") if isinstance(payload, dict) else None
    format_info = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        streams = []
    if not isinstance(format_info, dict):
        format_info = {}
    metadata["has_audio"] = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )
    video_stream = next(
        (stream for stream in streams
         if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    duration = format_info.get("duration")
    if duration is None and isinstance(video_stream, dict):
        duration = video_stream.get("duration")
    try:
        parsed_duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        parsed_duration = None
    # nan/inf would serialize as non-RFC JSON literals into the manifest;
    # degrade them to null like any other malformed probe value.
    metadata["duration_seconds"] = (
        parsed_duration
        if parsed_duration is not None and math.isfinite(parsed_duration)
        else None
    )
    if isinstance(video_stream, dict):
        for key in ("width", "height"):
            try:
                value = video_stream.get(key)
                metadata[key] = int(value) if value is not None else None
            except (TypeError, ValueError, OverflowError):
                metadata[key] = None
    return metadata


def inventory(source: Path) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_dir():
        raise PipelineError(f"source is not a directory: {source}")
    files: list[dict[str, Any]] = []
    for index, path in enumerate(sorted(p for p in source.rglob("*") if p.is_file()), start=1):
        kind = classify(path)
        stat = path.stat()
        item: dict[str, Any] = {
            "source_id": f"F{index:06d}",
            "relative_path": str(path.relative_to(source)),
            "kind": kind,
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).astimezone().isoformat(),
            "sha256": sha256_file(path),
            "eligible_source": kind in {"image", "audio", "note", "video"} and not path.name.startswith("."),
        }
        if kind == "image":
            item["media"] = image_metadata(path)
        elif kind == "audio":
            item["media"] = audio_metadata(path)
        elif kind == "video":
            item["media"] = video_metadata(path)
        files.append(item)
    counts: dict[str, int] = {}
    for item in files:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    duration = sum(
        float(item.get("media", {}).get("duration_seconds") or 0)
        for item in files
        if item["kind"] == "audio"
    )
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "source_root": str(source),
        "file_count": len(files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "counts": counts,
        "audio_duration_seconds": duration,
        "files": files,
    }


def ollama_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "binary": shutil.which("ollama"),
        "reachable": False,
        "models": [],
    }
    if not result["binary"]:
        return result
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            payload = json.load(response)
        result["reachable"] = True
        result["models"] = [model.get("name") for model in payload.get("models", [])]
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        result["error"] = str(exc)
    return result


def doctor() -> dict[str, Any]:
    tools = [
        "python3",
        "swift",
        "sips",
        "afinfo",
        "ffmpeg",
        "ffprobe",
        "whisper-cli",
        "deep-filter",
        "ollama",
    ]
    return {
        "checked_at": now_iso(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "tools": {name: shutil.which(name) for name in tools},
        "ollama": ollama_status(),
    }


def default_stage_state(name: str) -> dict[str, Any]:
    return {"name": name, "status": "pending", "attempts": [], "artifacts": []}


def init_run(manifest_path: Path, run_dir: Path, config_path: Path | None) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise PipelineError("unsupported or malformed manifest")
    run_dir = run_dir.resolve()
    state_path = run_dir / "state.json"
    if state_path.exists():
        raise PipelineError(f"run already exists: {state_path}; use status or resume it")
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "artifacts", "evidence", "reports"):
        (run_dir / name).mkdir(exist_ok=True)
    config = load_json(config_path) if config_path else {
        "version": 1,
        "max_attempts": 2,
        "supervision_mode": "metadata_first",
        "stages": DEFAULT_STAGES,
        "commands": {},
    }
    stages = config.get("stages") or DEFAULT_STAGES
    if not isinstance(stages, list) or any(not isinstance(item, str) for item in stages):
        raise PipelineError("config stages must be a list of names")
    atomic_write_json(run_dir / "manifest.json", manifest)
    atomic_write_json(run_dir / "config.json", config)
    state = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_dir": str(run_dir),
        "manifest": "manifest.json",
        "status": "ready",
        "stages": {name: default_stage_state(name) for name in stages},
        "stage_order": stages,
    }
    if "inventory" in state["stages"]:
        state["stages"]["inventory"].update(
            {"status": "passed", "artifacts": ["manifest.json"], "validated_at": now_iso()}
        )
    atomic_write_json(state_path, state)
    write_supervisor_packet(run_dir, state)
    return state


def validate_text(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"not valid UTF-8 text: {exc}"]
    if not text.strip():
        errors.append("text is empty")
    for pattern in BAD_TEXT_PATTERNS:
        if text.count(pattern) >= 2:
            errors.append(f"contains repeated invalid marker {pattern!r}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 8:
        dominant = max((lines.count(line) for line in set(lines)), default=0)
        if dominant / len(lines) > 0.35:
            errors.append("excessive repeated lines")
    return errors


def validate_artifact(spec: dict[str, Any], run_dir: Path) -> list[str]:
    errors: list[str] = []
    relative = spec.get("path")
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return ["artifact path must be a safe run-relative path"]
    path = run_dir / relative
    if not path.is_file():
        return [f"missing artifact: {relative}"]
    minimum = int(spec.get("min_bytes", 1))
    if path.stat().st_size < minimum:
        errors.append(f"artifact smaller than {minimum} bytes: {relative}")
    artifact_type = spec.get("type", "file")
    if artifact_type == "json":
        try:
            value = load_json(path)
            for key in spec.get("required_keys", []):
                if not isinstance(value, dict) or key not in value:
                    errors.append(f"missing required key {key!r}: {relative}")
        except PipelineError as exc:
            errors.append(str(exc))
    elif artifact_type in {"text", "markdown", "jsonl"}:
        errors.extend(validate_text(path))
        if artifact_type == "jsonl" and not errors:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"invalid JSONL at line {line_number}: {exc}")
                    break
    return errors


def load_state(run_dir: Path) -> dict[str, Any]:
    state = load_json(run_dir / "state.json")
    if state.get("schema_version") != 1:
        raise PipelineError("unsupported state schema")
    return state


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(run_dir / "state.json", state)
    write_supervisor_packet(run_dir, state)


def previous_stages_passed(state: dict[str, Any], stage_name: str) -> bool:
    for name in state["stage_order"]:
        if name == stage_name:
            return True
        if state["stages"][name]["status"] not in {"passed", "skipped"}:
            return False
    return False


def execute_stage(run_dir: Path, stage_name: str) -> dict[str, Any]:
    state = load_state(run_dir)
    config = load_json(run_dir / "config.json")
    if stage_name not in state["stages"]:
        raise PipelineError(f"unknown stage: {stage_name}")
    stage = state["stages"][stage_name]
    if stage["status"] == "passed":
        return {"stage": stage_name, "status": "already_passed"}
    if not previous_stages_passed(state, stage_name):
        raise PipelineError(f"previous stages have not passed before {stage_name}")
    definition = (config.get("commands") or {}).get(stage_name)
    if not isinstance(definition, dict):
        stage["status"] = "needs_supervisor"
        stage["last_error"] = "no command adapter configured"
        state["status"] = "needs_supervisor"
        save_state(run_dir, state)
        raise PipelineError(f"no command adapter configured for {stage_name}")
    command = definition.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        raise PipelineError(f"stage command must be a nonempty string array: {stage_name}")
    max_attempts = int(config.get("max_attempts", 2))
    if len(stage["attempts"]) >= max_attempts:
        stage["status"] = "needs_supervisor"
        state["status"] = "needs_supervisor"
        save_state(run_dir, state)
        raise PipelineError(f"maximum attempts reached for {stage_name}")

    attempt_number = len(stage["attempts"]) + 1
    log_relative = f"logs/{stage_name}.attempt-{attempt_number}.log"
    log_path = run_dir / log_relative
    started = now_iso()
    stage["status"] = "running"
    state["status"] = "running"
    save_state(run_dir, state)
    timeout = int(definition.get("timeout_seconds", 3600))
    exit_code: int | None = None
    error: str | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=str(run_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                text=True,
            )
        exit_code = completed.returncode
        if exit_code != 0:
            error = f"command exited with {exit_code}"
    except subprocess.TimeoutExpired:
        error = f"command timed out after {timeout}s"
    except OSError as exc:
        error = f"command launch failed: {exc}"

    validation_errors: list[str] = []
    artifacts = definition.get("artifacts") or []
    if error is None:
        if not isinstance(artifacts, list):
            validation_errors.append("artifacts definition is not a list")
        else:
            for spec in artifacts:
                if not isinstance(spec, dict):
                    validation_errors.append("artifact definition is not an object")
                else:
                    validation_errors.extend(validate_artifact(spec, run_dir))
    attempt = {
        "number": attempt_number,
        "started_at": started,
        "ended_at": now_iso(),
        "command": command,
        "exit_code": exit_code,
        "log": log_relative,
        "error": error,
        "validation_errors": validation_errors,
    }
    stage["attempts"].append(attempt)
    if error is None and not validation_errors:
        stage["status"] = "passed"
        stage["artifacts"] = [spec["path"] for spec in artifacts]
        stage.pop("last_error", None)
        state["status"] = "ready"
    else:
        stage["last_error"] = error or "; ".join(validation_errors)
        if len(stage["attempts"]) >= max_attempts:
            stage["status"] = "needs_supervisor"
            state["status"] = "needs_supervisor"
        else:
            stage["status"] = "failed"
            state["status"] = "needs_retry"
    save_state(run_dir, state)
    return {"stage": stage_name, "status": stage["status"], "attempt": attempt}


def next_stage(state: dict[str, Any]) -> str | None:
    for name in state["stage_order"]:
        if state["stages"][name]["status"] not in {"passed", "skipped"}:
            return name
    return None


def tail_text(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return str(exc)
    return data[-limit:]


def write_supervisor_packet(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    stage_name = next_stage(state)
    failed_checks: list[str] = []
    attempts: list[dict[str, Any]] = []
    bounded: list[dict[str, Any]] = []
    if stage_name:
        stage = state["stages"][stage_name]
        attempts = stage.get("attempts", [])[-2:]
        if stage.get("last_error"):
            failed_checks.append(stage["last_error"])
        if attempts:
            latest = attempts[-1]
            failed_checks.extend(latest.get("validation_errors") or [])
            if latest.get("error"):
                if latest["error"] not in failed_checks:
                    failed_checks.append(latest["error"])
            log = latest.get("log")
            if log:
                bounded.append({"kind": "log_tail", "path": log, "text": tail_text(run_dir / log)})
    if state["status"] == "needs_retry":
        packet_status = "NEEDS_RETRY"
        actions = ["retry the same stage once", "change route if the failure is deterministic"]
    elif state["status"] == "needs_supervisor":
        packet_status = "NEEDS_SUPERVISOR"
        actions = ["inspect failed checks and bounded log", "repair adapter or select a fallback route"]
    elif stage_name is None:
        packet_status = "COMPLETE"
        actions = ["run final validation and verify archive receipt"]
    else:
        packet_status = "READY"
        actions = [f"run stage {stage_name}"]
    packet = {
        "schema_version": 1,
        "created_at": now_iso(),
        "run_id": state["run_id"],
        "stage": stage_name,
        "status": packet_status,
        "attempts": attempts,
        "failed_checks": failed_checks,
        "resource_snapshot": doctor(),
        "suggested_actions": actions,
        "bounded_evidence": bounded,
        "content_included": bool(bounded),
    }
    atomic_write_json(run_dir / "supervisor_packet.json", packet)
    return packet


def validate_run(run_dir: Path, require_complete: bool) -> dict[str, Any]:
    state = load_state(run_dir)
    manifest = load_json(run_dir / state["manifest"])
    errors: list[str] = []
    if not manifest.get("files"):
        errors.append("manifest has no files")
    for name in state["stage_order"]:
        stage = state["stages"].get(name)
        if not stage:
            errors.append(f"missing stage state: {name}")
            continue
        if require_complete and stage["status"] not in {"passed", "skipped"}:
            errors.append(f"stage not complete: {name}={stage['status']}")
        for relative in stage.get("artifacts", []):
            if not (run_dir / relative).is_file():
                errors.append(f"registered artifact missing: {name}: {relative}")
    return {"valid": not errors, "errors": errors, "checked_at": now_iso()}


def archive_plan(run_dir: Path, destination_name: str) -> dict[str, Any]:
    state = load_state(run_dir)
    manifest = load_json(run_dir / state["manifest"])
    source_root = Path(manifest["source_root"])
    destination = source_root / destination_name
    moves: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in manifest["files"]:
        if not item.get("eligible_source", item.get("kind") in {"image", "audio", "note"}):
            continue
        source = source_root / item["relative_path"]
        target = destination / item["relative_path"]
        if not source.is_file():
            errors.append(f"source missing: {source}")
            continue
        observed_hash = sha256_file(source)
        if observed_hash != item["sha256"]:
            errors.append(f"source hash changed: {source}")
        if target.exists():
            errors.append(f"archive collision: {target}")
        moves.append({"source": str(source), "target": str(target), "sha256": item["sha256"]})
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "run_id": state["run_id"],
        "dry_run": True,
        "destination": str(destination),
        "moves": moves,
        "errors": errors,
        "safe_to_apply": not errors,
    }


def apply_archive(run_dir: Path, plan_path: Path) -> dict[str, Any]:
    plan = load_json(plan_path)
    if not plan.get("dry_run") or not plan.get("safe_to_apply") or plan.get("errors"):
        raise PipelineError("archive plan is not safe to apply")
    validation = validate_run(run_dir, require_complete=False)
    if not validation["valid"]:
        raise PipelineError(f"run validation failed: {validation['errors']}")
    completed: list[dict[str, Any]] = []
    for item in plan["moves"]:
        source = Path(item["source"])
        target = Path(item["target"])
        if sha256_file(source) != item["sha256"]:
            raise PipelineError(f"source changed since plan: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        if sha256_file(target) != item["sha256"]:
            raise PipelineError(f"post-move hash mismatch: {target}")
        completed.append(item)
    receipt = {
        "schema_version": 1,
        "created_at": now_iso(),
        "run_id": plan["run_id"],
        "destination": plan["destination"],
        "moves": completed,
        "hashes_verified": True,
    }
    atomic_write_json(run_dir / "archive_receipt.json", receipt)
    return receipt


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="check local tools and services")
    doctor_parser.add_argument("--output", type=Path)

    inventory_parser = sub.add_parser("inventory", help="hash and characterize source files")
    inventory_parser.add_argument("--source", required=True, type=Path)
    inventory_parser.add_argument("--output", required=True, type=Path)

    init_parser = sub.add_parser("init-run", help="initialize a resumable run directory")
    init_parser.add_argument("--manifest", required=True, type=Path)
    init_parser.add_argument("--run-dir", required=True, type=Path)
    init_parser.add_argument("--config", type=Path)

    status_parser = sub.add_parser("status", help="show state and refresh supervisor packet")
    status_parser.add_argument("--run-dir", required=True, type=Path)

    execute_parser = sub.add_parser("execute", help="execute one configured stage")
    execute_parser.add_argument("--run-dir", required=True, type=Path)
    execute_parser.add_argument("--stage", required=True)

    validate_parser = sub.add_parser("validate", help="validate controller state and artifacts")
    validate_parser.add_argument("--run-dir", required=True, type=Path)
    validate_parser.add_argument("--require-complete", action="store_true")

    archive_parser = sub.add_parser("archive-plan", help="write a non-mutating archive plan")
    archive_parser.add_argument("--run-dir", required=True, type=Path)
    archive_parser.add_argument("--output", required=True, type=Path)
    archive_parser.add_argument("--destination-name", default="原始材料")

    apply_parser = sub.add_parser("archive-apply", help="apply an explicitly approved archive plan")
    apply_parser.add_argument("--run-dir", required=True, type=Path)
    apply_parser.add_argument("--plan", required=True, type=Path)
    apply_parser.add_argument("--yes", action="store_true", help="required explicit mutation flag")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
            if args.output:
                atomic_write_json(args.output, result)
        elif args.command == "inventory":
            result = inventory(args.source)
            atomic_write_json(args.output, result)
        elif args.command == "init-run":
            result = init_run(args.manifest, args.run_dir, args.config)
        elif args.command == "status":
            state = load_state(args.run_dir)
            packet = write_supervisor_packet(args.run_dir, state)
            result = {"state": state, "supervisor_packet": packet}
        elif args.command == "execute":
            result = execute_stage(args.run_dir, args.stage)
        elif args.command == "validate":
            result = validate_run(args.run_dir, args.require_complete)
            print_json(result)
            return 0 if result["valid"] else 2
        elif args.command == "archive-plan":
            result = archive_plan(args.run_dir, args.destination_name)
            atomic_write_json(args.output, result)
        elif args.command == "archive-apply":
            if not args.yes:
                raise PipelineError("archive-apply requires --yes")
            result = apply_archive(args.run_dir, args.plan)
        else:
            raise PipelineError(f"unsupported command: {args.command}")
        print_json(result)
        return 0
    except PipelineError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
