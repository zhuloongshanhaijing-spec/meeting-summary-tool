#!/usr/bin/env python3
"""Compile and run the bundled Apple Vision OCR adapter."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        raise SystemExit(f"input directory not found: {args.input_dir}")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise SystemExit("swiftc is required")
    args.build_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    binary = args.build_dir / "vision_ocr"
    source = Path(__file__).with_name("vision_ocr.swift")
    compile_command = [swiftc]
    sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk")
    if sdk.exists():
        compile_command += ["-sdk", str(sdk), "-target", "arm64-apple-macosx15.4"]
    compile_command += [str(source), "-o", str(binary)]
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(args.build_dir / "clang-module-cache")
    subprocess.run(compile_command, check=True, env=environment)
    subprocess.run([str(binary), str(args.input_dir), str(args.output)], check=True)
    if not args.output.is_file() or args.output.stat().st_size == 0:
        raise SystemExit("Vision OCR produced no output")
    if args.receipt:
        record_count = sum(1 for line in args.output.read_text(encoding="utf-8").splitlines() if line.strip())
        payload = {
            "schema_version": 3,
            "status": "complete",
            "record_count": record_count,
            "output": str(args.output),
            "content_included": False,
        }
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.receipt.with_suffix(args.receipt.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.receipt)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
