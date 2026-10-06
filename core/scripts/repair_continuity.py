#!/usr/bin/env python3
"""Audit transcript continuity, benchmark bounded cloud ASR clips, and build a readable layer.

The literal/evidence layers are immutable inputs.  Cloud results are candidates only;
the readable transcript is a separate derivative with a machine-readable receipt.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path


TERMINAL = re.compile(r"[。！？!?；;]$")
BROKEN = re.compile(r"(?:的|和|与|是|在|把|对|到|从|因为|所以|但是|如果|然后|以及|或者)[。！？!?；;]$")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compact(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum() or "\u3400" <= ch <= "\u9fff")


def similarity(a: str, b: str) -> float:
    import difflib
    a, b = compact(a), compact(b)
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def window_rows(records: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in records:
        grouped[(row["source_id"], float(row["start_seconds"]), float(row["end_seconds"]))].append(row)
    result = []
    for (source_id, start, end), rows in sorted(grouped.items()):
        rows.sort(key=lambda r: r["sentence_index_in_window"])
        text = "".join(r["clean_literal"] for r in rows)
        uncertain_join = any(
            edit.get("type") == "overlap_not_deleted_without_exact_support"
            for row in rows for edit in row.get("edits", [])
        )
        score = 0
        reasons = []
        if not TERMINAL.search(text):
            score += 3; reasons.append("no_terminal_punctuation")
        if BROKEN.search(text):
            score += 4; reasons.append("ends_with_connector")
        if uncertain_join:
            score += 2; reasons.append("unresolved_window_overlap")
        if any(row.get("certainty") == "low" for row in rows):
            score += 3; reasons.append("low_certainty")
        result.append({"source_id": source_id, "start_seconds": start, "end_seconds": end,
                       "record_ids": [r["record_id"] for r in rows], "text": text,
                       "risk_score": score, "reasons": reasons})
    return result


def select_clips(windows: list[dict], per_source: int = 4, separation: float = 180.0) -> list[dict]:
    selected = []
    for source_id in sorted({w["source_id"] for w in windows}):
        candidates = sorted((w for w in windows if w["source_id"] == source_id),
                            key=lambda w: (-w["risk_score"], w["start_seconds"]))
        chosen = []
        for row in candidates:
            if row["risk_score"] <= 0 or any(abs(row["start_seconds"] - x["start_seconds"]) < separation for x in chosen):
                continue
            chosen.append(row)
            if len(chosen) == per_source:
                break
        for row in chosen:
            start = max(0.0, row["start_seconds"] - 10.0)
            end = row["end_seconds"] + 15.0
            selected.append({"clip_id": f"C{len(selected)+1:03d}", "source_id": source_id,
                             "start_seconds": start, "end_seconds": end,
                             "boundary_seconds": row["end_seconds"], "risk_score": row["risk_score"],
                             "reasons": row["reasons"], "record_ids": row["record_ids"]})
    return selected


def keychain(service: str) -> str:
    result = subprocess.run(["security", "find-generic-password", "-w", "-s", service],
                            capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"credential unavailable: {service}")
    return result.stdout.strip()


def extract_clips(manifest: dict, clips: list[dict], output: Path, ffmpeg: str) -> None:
    files = {row["source_id"]: row for row in manifest["files"]}
    root = Path(manifest["source_root"])
    for clip in clips:
        source = root / files[clip["source_id"]]["relative_path"]
        target = output / "clips" / f"{clip['clip_id']}.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", str(clip["start_seconds"]),
               "-i", str(source), "-t", str(clip["end_seconds"] - clip["start_seconds"]),
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(target)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode or not target.is_file():
            raise RuntimeError(f"clip extraction failed: {clip['clip_id']}")
        clip["local_path"] = str(target)
        clip["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        clip["bytes"] = target.stat().st_size


def groq_asr(audio: Path, timeout: int = 90) -> dict:
    key = keychain("ai-project-foundry-groq")
    boundary = "----aipf" + secrets.token_hex(12)
    parts = []
    for name, value in [("model", "whisper-large-v3"), ("language", "zh"),
                        ("response_format", "verbose_json"), ("temperature", "0")]:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.flac\"\r\n"
                  "Content-Type: audio/flac\r\n\r\n").encode() + audio.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions",
        data=b"".join(parts), headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def cloudflare_asr(audio: Path, timeout: int = 90) -> dict:
    token = keychain("ai-project-foundry-cloudflare-token")
    account = keychain("ai-project-foundry-cloudflare-account-id")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/openai/whisper-large-v3-turbo"
    body = json.dumps({"audio": base64.b64encode(audio.read_bytes()).decode(), "language": "zh",
                       "vad_filter": True, "condition_on_previous_text": True}).encode()
    request = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def response_text(provider: str, payload: dict) -> str:
    if provider == "groq":
        return str(payload.get("text") or "").strip()
    result = payload.get("result") or {}
    return str(result.get("text") or result.get("response") or "").strip()


def benchmark(clips: list[dict], output: Path) -> dict:
    rows = []
    for clip in clips:
        audio = Path(clip["local_path"])
        candidates = {}
        errors = {}
        for provider, runner in (("groq", groq_asr), ("cloudflare", cloudflare_asr)):
            started = time.monotonic()
            try:
                payload = runner(audio)
                write_json(output / "private_candidates" / f"{clip['clip_id']}__{provider}.json", payload)
                candidates[provider] = response_text(provider, payload)
                errors[provider] = None
            except Exception as exc:
                candidates[provider] = ""
                errors[provider] = f"{type(exc).__name__}: {exc}"
            rows.append({"clip_id": clip["clip_id"], "provider": provider,
                         "elapsed_seconds": round(time.monotonic() - started, 3),
                         "success": bool(candidates[provider]), "error": errors[provider],
                         "character_count": len(compact(candidates[provider]))})
        both = all(candidates.values())
        clip["cloud_agreement"] = round(similarity(candidates["groq"], candidates["cloudflare"]), 4) if both else None
        clip["providers_succeeded"] = [p for p, text in candidates.items() if text]
    return {"schema_version": 1, "policy": "user-authorized bounded disputed clips only",
            "clip_count": len(clips), "uploaded_audio_seconds": round(sum(c["end_seconds"]-c["start_seconds"] for c in clips), 1),
            "rows": rows, "clips": [{k:v for k,v in c.items() if k != "local_path"} for c in clips],
            "content_included": False}


def consent_request(clips: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "status": "AWAITING_EXPLICIT_APPROVAL",
        "purpose": "compare transcription continuity on bounded disputed meeting-audio clips",
        "destinations": ["Groq API", "Cloudflare Workers AI"],
        "data_class": "private_meeting_audio_excerpt",
        "clip_count": len(clips),
        "total_audio_seconds": round(sum(c["end_seconds"] - c["start_seconds"] for c in clips), 1),
        "clips": [{"clip_id": c["clip_id"], "source_id": c["source_id"],
                   "start_seconds": c["start_seconds"], "end_seconds": c["end_seconds"],
                   "sha256": c["sha256"], "bytes": c["bytes"]} for c in clips],
        "risks": ["audio leaves the local Mac", "provider retention and processing terms apply"],
        "approval": {"approved": False, "approved_destinations": [], "approved_clip_sha256": []},
        "content_included": False,
    }


def validate_consent(path: Path, clips: list[dict]) -> None:
    consent = json.loads(path.read_text(encoding="utf-8"))
    approval = consent.get("approval") or {}
    expected_destinations = {"Groq API", "Cloudflare Workers AI"}
    expected_hashes = {c["sha256"] for c in clips}
    if approval.get("approved") is not True:
        raise RuntimeError("cloud upload consent is not approved")
    if set(approval.get("approved_destinations") or []) != expected_destinations:
        raise RuntimeError("approved destinations do not match this run")
    if set(approval.get("approved_clip_sha256") or []) != expected_hashes:
        raise RuntimeError("approved clip hashes do not match this run")


def readable_transcript(records: list[dict], output: Path) -> dict:
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in records: by_source[row["source_id"]].append(row)
    lines = ["# 连续可读会议稿", "", "> 本稿是阅读层，不替代逐句证据稿。措辞来自原逐句记录；仅合并段落、保留时间锚点，未用模型补写事实。", ""]
    paragraph_count = 0
    for source_id, rows in sorted(by_source.items()):
        rows.sort(key=lambda r: (r["start_seconds"], r["sentence_index_in_window"]))
        lines += [f"## {source_id}", ""]
        paragraph, ids, start, end = [], [], None, None
        for row in rows:
            if start is None: start = float(row["start_seconds"])
            end = float(row["end_seconds"])
            paragraph.append(row["clean_literal"])
            ids.append(row["record_id"])
            text = "".join(paragraph)
            # Close only at a plausible sentence boundary and a readable paragraph size.
            if (len(compact(text)) >= 180 and TERMINAL.search(text) and not BROKEN.search(text)) or len(compact(text)) >= 360:
                lines += [f"**{int(start//60):02d}:{int(start%60):02d}–{int(end//60):02d}:{int(end%60):02d}**  {text}",
                          f"<sub>证据：{ids[0]}–{ids[-1]}</sub>", ""]
                paragraph_count += 1; paragraph=[]; ids=[]; start=None
        if paragraph:
            text = "".join(paragraph)
            lines += [f"**{int(start//60):02d}:{int(start%60):02d}–{int(end//60):02d}:{int(end%60):02d}**  {text}",
                      f"<sub>证据：{ids[0]}–{ids[-1]}</sub>", ""]
            paragraph_count += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"schema_version": 1, "status": "complete", "input_record_count": len(records),
            "paragraph_count": paragraph_count, "semantic_rewriting": False,
            "evidence_layer_overwritten": False, "output": str(output), "content_included": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--readable-output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--skip-cloud", action="store_true")
    parser.add_argument("--consent", type=Path)
    args = parser.parse_args()
    records = read_jsonl(args.records)
    windows = window_rows(records)
    clips = select_clips(windows)
    analysis = {"schema_version": 1, "record_count": len(records), "window_count": len(windows),
        "nonterminal_record_count": sum(not TERMINAL.search(r["clean_literal"]) for r in records),
        "connector_ending_record_count": sum(bool(BROKEN.search(r["clean_literal"])) for r in records),
        "unresolved_window_join_count": sum("unresolved_window_overlap" in w["reasons"] for w in windows),
        "selected_clip_count": len(clips), "content_included": False}
    write_json(args.output_dir / "continuity_analysis_receipt.json", analysis)
    extract_clips(json.loads(args.manifest.read_text(encoding="utf-8")), clips, args.output_dir, args.ffmpeg)
    write_json(args.output_dir / "clip_selection_receipt.json", {"clips": [{k:v for k,v in c.items() if k != "local_path"} for c in clips], "content_included": False})
    request_path = args.output_dir / "cloud_upload_consent_request.json"
    if not request_path.exists():
        write_json(request_path, consent_request(clips))
    if not args.skip_cloud:
        if not args.consent:
            raise RuntimeError(f"explicit consent artifact required: {request_path}")
        validate_consent(args.consent, clips)
        write_json(args.output_dir / "cloud_asr_benchmark_receipt.json", benchmark(clips, args.output_dir))
    write_json(args.output_dir / "readable_transcript_receipt.json", readable_transcript(records, args.readable_output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
