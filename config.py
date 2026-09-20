"""Central path/config resolution for the Meeting Summary Tool.

Resolution order per key: environment variable > config.json > default.
config.json lives at the repo root and is gitignored — start from
config.example.json. All missing REQUIRED keys are reported together in
one collective error with their env-var names (see INSTALL.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# key -> (env var, required, default)
SPEC = {
    "whisper_bin":   ("MST_WHISPER_BIN",   True,  None),
    "whisper_model": ("MST_WHISPER_MODEL", True,  None),
    "qwen_python":   ("MST_QWEN_PYTHON",   True,  None),
    "ollama_url":    ("MST_OLLAMA_URL",    False, "http://127.0.0.1:11434"),
    "ollama_model":  ("MST_OLLAMA_MODEL",  False, "qwen3:8b"),
    "ffmpeg":        ("MST_FFMPEG",        False, None),
    "whisper_root":  ("MST_WHISPER_ROOT",  False, None),
}

_PRIVATE_FILE = ROOT / "config.json"


def _load_file() -> dict:
    if not _PRIVATE_FILE.exists():
        return {}
    try:
        return json.loads(_PRIVATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[config] config.json is not valid JSON: {exc}") from None


def resolve() -> dict:
    """Resolve every key; raise one collective SystemExit if required keys missing."""
    file_cfg = _load_file()
    out: dict = {}
    missing: list[tuple[str, str]] = []
    for key, (env, required, default) in SPEC.items():
        val = os.environ.get(env) or file_cfg.get(key) or default
        if val in (None, ""):
            if required:
                missing.append((key, env))
            else:
                out[key] = None
        else:
            out[key] = val
    if missing:
        detail = "\n".join(f"  - {k:14s} env {e}" for k, e in missing)
        raise SystemExit(
            "[config] missing required settings. Copy config.example.json to "
            "config.json and fill it in, or export the env vars (see INSTALL.md):\n" + detail)
    return out
