#!/usr/bin/env python3
"""Run a secondary FunASR model while keeping recognized content on disk."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import wave
from pathlib import Path


def json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    return str(value)


def count_text(value) -> int:
    if isinstance(value, dict):
        return sum(len(item) for key, item in value.items() if key == "text" and isinstance(item, str)) + sum(
            count_text(item) for key, item in value.items() if key != "text"
        )
    if isinstance(value, list):
        return sum(count_text(item) for item in value)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", default="iic/SenseVoiceSmall")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--chunk-seconds", type=float, default=60.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--model-cache",
        type=Path,
        default=Path(os.environ["MST_FUNASR_MODELS"]) if os.environ.get("MST_FUNASR_MODELS") else Path.home() / ".cache" / "funasr-models",
    )
    args = parser.parse_args()

    inputs = sorted(args.input_dir.glob("*.wav"))
    if not inputs:
        raise SystemExit(f"no WAV files found: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.model_cache.mkdir(parents=True, exist_ok=True)
    os.environ["MODELSCOPE_CACHE"] = str(args.model_cache)
    os.environ["HF_HOME"] = str(args.model_cache / "huggingface")
    log_path = args.output_dir / "funasr.log"
    records = []
    with log_path.open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            from funasr import AutoModel

            model = AutoModel(model=args.model, device=args.device, disable_update=True)
            for source in inputs:
                output_path = args.output_dir / f"{source.stem}.json"
                if args.resume and output_path.is_file():
                    try:
                        existing = json.loads(output_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing = None
                    existing_count = count_text(existing)
                    if isinstance(existing, list):
                        records.append(
                            {
                                "input": str(source), "output": str(output_path),
                                "chunk_count": len(existing), "chunk_seconds": args.chunk_seconds,
                                "recognized_character_count": existing_count, "reused": True,
                            }
                        )
                        continue
                source_results = []
                total_character_count = 0
                with wave.open(str(source), "rb") as reader:
                    channels = reader.getnchannels()
                    sample_width = reader.getsampwidth()
                    sample_rate = reader.getframerate()
                    if channels != 1 or sample_width != 2:
                        raise RuntimeError(f"expected mono PCM16 WAV: {source}")
                    frames_per_chunk = max(1, int(sample_rate * args.chunk_seconds))
                    chunk_index = 0
                    with tempfile.TemporaryDirectory(prefix="meeting-funasr-") as temporary:
                        temporary_path = Path(temporary)
                        while True:
                            frames = reader.readframes(frames_per_chunk)
                            if not frames:
                                break
                            chunk_path = temporary_path / f"chunk-{chunk_index:05d}.wav"
                            with wave.open(str(chunk_path), "wb") as writer:
                                writer.setnchannels(channels)
                                writer.setsampwidth(sample_width)
                                writer.setframerate(sample_rate)
                                writer.writeframes(frames)
                            result = model.generate(
                                input=str(chunk_path), cache={}, language="auto", use_itn=True,
                            )
                            safe_result = json_safe(result)
                            character_count = count_text(safe_result)
                            frame_count = len(frames) // (channels * sample_width)
                            start_seconds = chunk_index * args.chunk_seconds
                            source_results.append(
                                {
                                    "chunk_index": chunk_index,
                                    "start_seconds": round(start_seconds, 3),
                                    "end_seconds": round(start_seconds + frame_count / sample_rate, 3),
                                    "result": safe_result,
                                    "recognized_character_count": character_count,
                                }
                            )
                            total_character_count += character_count
                            chunk_index += 1
                output_path.write_text(
                    json.dumps(source_results, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                records.append(
                    {
                        "input": str(source),
                        "output": str(output_path),
                        "chunk_count": len(source_results),
                        "chunk_seconds": args.chunk_seconds,
                        "recognized_character_count": total_character_count,
                        "reused": False,
                    }
                )
        except Exception as exc:
            log.flush()
            raise SystemExit(f"FunASR failed; inspect {log_path}: {type(exc).__name__}: {exc}") from exc
    receipt = {
        "schema_version": 1,
        "adapter": "FunASR",
        "model": args.model,
        "device": args.device,
        "model_cache": str(args.model_cache),
        "log": str(log_path),
        "records": records,
        "reused_count": sum(record.get("reused", False) for record in records),
    }
    receipt_path = args.output_dir / "funasr_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
