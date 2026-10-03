"""Shared schema-surface regressions for the screen-recording module (T4).

Pins the additive surface introduced by design §3.7/§4.1/§4.3/§4.6/§4.7:
- meeting_pipeline.classify: video extensions -> "video"; exact "region.json"
  -> "config" (never eligible); audio/image/note classification unchanged.
- meeting_pipeline.inventory: video items eligible with ffprobe `media`
  metadata; corrupt video / missing ffprobe degrade to null fields, never
  crash; region.json registered but not eligible.
- prepare_audio: manifest audio items may carry an absolute `path` override
  (derived media lives in runs/, not under source_root); without `path` the
  classic source_root/relative_path resolution still applies.
- build_package_v3: default invocation surface (artifacts list, no 幻灯片/,
  no OCR sections) unchanged and byte-stable; optional --slides-dir and
  --ocr-corrections extend the package additively; empty/absent corrections
  behave exactly like the default call.
- config.SPEC: optional `tools_python` key resolved env > config.json >
  default vendor/tools-venv interpreter.

Runs under plain system python3 (stdlib only). ffmpeg/ffprobe on PATH are
required for the media fixtures (documented pipeline dependencies); the
tests skip cleanly when they are absent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
CORE_SCRIPTS = WS / "core" / "scripts"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = unittest.skipUnless(
    FFMPEG, "ffmpeg required (documented pipeline dependency)")
requires_media_tools = unittest.skipUnless(
    FFMPEG and FFPROBE, "ffmpeg/ffprobe required (documented pipeline dependencies)")


def load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


pipeline = load_module(
    "mst_meeting_pipeline_schema_surface", CORE_SCRIPTS / "meeting_pipeline.py")
mst_config = load_module("mst_config_schema_surface", WS / "config.py")


def run_script(script: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(CORE_SCRIPTS / script), *args],
        capture_output=True, text=True,
    )


def generate_video(path: Path, *, with_audio: bool,
                   seconds: float = 1.0, size: str = "320x240", rate: int = 5) -> None:
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    command += [str(path)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg fixture generation failed: {completed.stderr[-800:]}")


class TestClassifySurface(unittest.TestCase):
    """classify(): new video/config kinds, existing kinds untouched."""

    def test_video_extension_set_pinned(self):
        self.assertEqual(
            pipeline.VIDEO_EXTENSIONS, {".mp4", ".mov", ".mkv", ".webm", ".m4v"})

    def test_video_extensions_classify_as_video(self):
        for extension in sorted(pipeline.VIDEO_EXTENSIONS):
            with self.subTest(extension=extension):
                self.assertEqual(pipeline.classify(Path(f"meeting{extension}")), "video")
                self.assertEqual(pipeline.classify(Path(f"MEETING{extension.upper()}")), "video")

    def test_region_json_classifies_as_config(self):
        self.assertEqual(pipeline.classify(Path("region.json")), "config")
        self.assertEqual(pipeline.classify(Path("input/event-a/region.json")), "config")

    def test_other_json_files_are_not_config(self):
        # Only the exact input-config name is "config"; every other .json
        # (including run artifacts like slides.json) stays "other".
        for name in ("slides.json", "manifest.json", "config.json", "REGION.JSON", "region.JSON"):
            with self.subTest(name=name):
                self.assertEqual(pipeline.classify(Path(name)), "other")

    def test_existing_kinds_unchanged(self):
        expectations = {
            "recording.m4a": "audio", "track.mp3": "audio", "raw.wav": "audio",
            "photo.jpg": "image", "scan.png": "image", "frame.heic": "image",
            "notes.md": "note", "agenda.txt": "note", "deck.pptx": "note",
            "mystery.bin": "other", "archive.zip": "other",
        }
        for name, kind in expectations.items():
            with self.subTest(name=name):
                self.assertEqual(pipeline.classify(Path(name)), kind)

    def test_extension_sets_disjoint(self):
        sets = [pipeline.IMAGE_EXTENSIONS, pipeline.AUDIO_EXTENSIONS,
                pipeline.VIDEO_EXTENSIONS, pipeline.NOTE_EXTENSIONS]
        for i, first in enumerate(sets):
            for second in sets[i + 1:]:
                self.assertEqual(first & second, set())


@requires_media_tools
class TestInventoryVideoSurface(unittest.TestCase):
    """inventory(): video media metadata via ffprobe, config/dotfile rules."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # Registered immediately: the tempdir is reclaimed even when the
        # fixture generation below fails half-way (review 2026-09-30).
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.source = Path(cls._tmp.name) / "event"
        cls.source.mkdir(parents=True)
        generate_video(cls.source / "meeting.mp4", with_audio=True)
        generate_video(cls.source / "silent.mov", with_audio=False)
        (cls.source / "corrupt.mp4").write_bytes(b"this-is-not-a-video-container" * 8)
        (cls.source / ".region-preview.png").write_bytes(b"dotfile-preview-bytes")
        (cls.source / "region.json").write_text(json.dumps({
            "schema_version": 1, "video": "meeting.mp4", "source": "user",
            "rect": {"x": 0.08, "y": 0.05, "w": 0.66, "h": 0.82},
            "confidence": 0.87, "created_ts": 1759200000.0,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        (cls.source / "notes.md").write_text("# 会议笔记\n", encoding="utf-8")
        cls.manifest = pipeline.inventory(cls.source)
        cls.by_name = {
            Path(item["relative_path"]).name: item for item in cls.manifest["files"]
        }

    def test_counts_and_audio_duration_exclude_video(self):
        self.assertEqual(self.manifest["file_count"], 6)
        self.assertEqual(
            self.manifest["counts"],
            {"video": 3, "image": 1, "config": 1, "note": 1},
        )
        # Video durations never leak into the audio total.
        self.assertEqual(self.manifest["audio_duration_seconds"], 0.0)

    def test_video_with_audio_media_metadata(self):
        item = self.by_name["meeting.mp4"]
        self.assertEqual(item["kind"], "video")
        self.assertEqual(item["extension"], ".mp4")
        self.assertTrue(item["eligible_source"])
        media = item["media"]
        self.assertIsInstance(media["duration_seconds"], float)
        self.assertAlmostEqual(media["duration_seconds"], 1.0, delta=0.5)
        self.assertIs(media["has_audio"], True)
        self.assertEqual(media["width"], 320)
        self.assertEqual(media["height"], 240)

    def test_silent_video_has_audio_false(self):
        item = self.by_name["silent.mov"]
        self.assertEqual(item["kind"], "video")
        self.assertTrue(item["eligible_source"])
        self.assertIs(item["media"]["has_audio"], False)
        self.assertAlmostEqual(item["media"]["duration_seconds"], 1.0, delta=0.5)
        self.assertEqual(item["media"]["width"], 320)
        self.assertEqual(item["media"]["height"], 240)

    def test_corrupt_video_degrades_to_nulls_without_crashing(self):
        item = self.by_name["corrupt.mp4"]
        self.assertEqual(item["kind"], "video")
        self.assertTrue(item["eligible_source"])
        media = item["media"]
        for field in ("duration_seconds", "has_audio", "width", "height"):
            self.assertIsNone(media[field], f"{field} should degrade to null")

    def test_region_json_registered_but_not_eligible(self):
        item = self.by_name["region.json"]
        self.assertEqual(item["kind"], "config")
        self.assertEqual(item["extension"], ".json")
        self.assertFalse(item["eligible_source"])
        self.assertNotIn("media", item)

    def test_note_and_dotfile_surface_unchanged(self):
        note = self.by_name["notes.md"]
        self.assertEqual(note["kind"], "note")
        self.assertTrue(note["eligible_source"])
        self.assertNotIn("media", note)
        preview = self.by_name[".region-preview.png"]
        self.assertEqual(preview["kind"], "image")
        self.assertFalse(preview["eligible_source"])  # dotfile rule still wins


class TestVideoMetadataHostileProbe(unittest.TestCase):
    """video_metadata(): non-finite probe values degrade to null, so the
    manifest never contains non-RFC JSON literals like NaN/Infinity."""

    def probe_with(self, payload: dict) -> dict:
        # No real ffprobe needed: patch the probe transport and tool discovery.
        with mock.patch.object(pipeline, "run_probe",
                               return_value=(0, json.dumps(payload))), \
                mock.patch.object(pipeline.shutil, "which",
                                  return_value="/fake/ffprobe"):
            return pipeline.video_metadata(Path("hostile.mp4"))

    def test_nonfinite_durations_degrade_to_null(self):
        cases = {
            "nan string in format": {"format": {"duration": "nan"}, "streams": []},
            "inf string in format": {"format": {"duration": "inf"}, "streams": []},
            "json NaN literal": {"format": {"duration": float("nan")}, "streams": []},
            "stream fallback nan": {
                "format": {},
                "streams": [{"codec_type": "video", "duration": "nan",
                             "width": 320, "height": 240}],
            },
        }
        for label, payload in cases.items():
            with self.subTest(payload=label):
                metadata = self.probe_with(payload)
                self.assertIsNone(metadata["duration_seconds"])
                serialized = json.dumps(metadata)
                self.assertNotIn("NaN", serialized)
                self.assertNotIn("Infinity", serialized)

    def test_nonfinite_width_degrades_to_null(self):
        payload = {
            "format": {"duration": "1.5"},
            "streams": [{"codec_type": "video", "width": float("inf"), "height": 240}],
        }
        metadata = self.probe_with(payload)
        self.assertIsNone(metadata["width"])
        self.assertEqual(metadata["height"], 240)
        self.assertEqual(metadata["duration_seconds"], 1.5)
        self.assertNotIn("Infinity", json.dumps(metadata))


@requires_ffmpeg
class TestPrepareAudioAbsolutePathOverride(unittest.TestCase):
    """prepare_audio: derived manifest entries carry an absolute `path`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source_root = self.root / "input" / "event"
        self.source_root.mkdir(parents=True)
        (self.source_root / "notes.md").write_text("# 笔记\n", encoding="utf-8")
        # Derived audio as produced by video_ingest (design §4.3): lives in
        # runs/, NOT under source_root.
        self.derived_dir = self.root / "runs" / "event"
        self.derived_dir.mkdir(parents=True)
        self.derived_audio = self.derived_dir / "extracted_audio.m4a"
        completed = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             str(self.derived_audio)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-800:])

    def write_manifest(self, item: dict) -> Path:
        manifest_path = self.root / f"manifest-{item['source_id']}.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "source_root": str(self.source_root),
            "files": [item],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    def run_prepare(self, manifest: Path, run_name: str) -> tuple[subprocess.CompletedProcess, Path]:
        run_dir = self.root / run_name
        # No --enhanced: the default route needs no RNN model.
        return run_script("prepare_audio.py", [
            "--manifest", str(manifest), "--run-dir", str(run_dir),
        ]), run_dir

    def test_absolute_path_override_is_used(self):
        manifest = self.write_manifest({
            "source_id": "F000007", "kind": "audio",
            "relative_path": "extracted_audio.m4a",  # resolves nowhere under source_root
            "path": str(self.derived_audio),
            "eligible_source": True, "derived_from": "meeting.mp4",
        })
        completed, run_dir = self.run_prepare(manifest, "run-override")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        output = run_dir / "artifacts" / "audio" / "normalized" / "F000007.wav"
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 44)
        receipt = json.loads(
            (run_dir / "artifacts" / "audio" / "prepare_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(len(receipt["tracks"]), 1)
        track = receipt["tracks"][0]
        self.assertEqual(track["source_id"], "F000007")
        self.assertEqual(track["route"], "original_normalized")
        self.assertEqual(track["source"], str(self.derived_audio))
        self.assertEqual(track["output"], "artifacts/audio/normalized/F000007.wav")

    def test_without_path_relative_resolution_still_applies(self):
        # No "path" key -> classic source_root/relative_path resolution; the
        # file does not exist there, so preparation must fail loudly rather
        # than silently invent a source.
        manifest = self.write_manifest({
            "source_id": "F000008", "kind": "audio",
            "relative_path": "missing-recording.wav",
            "eligible_source": True,
        })
        completed, _ = self.run_prepare(manifest, "run-relative")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("audio preparation failed", completed.stderr)

    def test_relative_path_override_fails_loud(self):
        # A relative "path" override would resolve against the process CWD and
        # could transcribe the wrong file: it must fail loudly, before any
        # output or receipt is produced (review 2026-09-30).
        manifest = self.write_manifest({
            "source_id": "F000010", "kind": "audio",
            "relative_path": "ignored-relative.wav",
            "path": "extracted_audio.m4a",  # relative override -> rejected
            "eligible_source": True, "derived_from": "meeting.mp4",
        })
        completed, run_dir = self.run_prepare(manifest, "run-relative-override")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("manifest path override must be absolute", completed.stderr)
        self.assertIn("F000010", completed.stderr)
        self.assertFalse(
            (run_dir / "artifacts" / "audio" / "prepare_receipt.json").exists())
        self.assertFalse(
            (run_dir / "artifacts" / "audio" / "normalized" / "F000010.wav").exists())


# ---------------------------------------------------------------------------
# build_package_v3
# ---------------------------------------------------------------------------

ORIGINAL_ARTIFACTS = [
    "00_使用说明.md", "01_主题索引.md", "02_逐句会议记录.md", "03_PPT补充信息.md",
    "04_会议报告.md", "05_不确定与冲突.md", "meeting.db", "coverage_receipt.json",
]
PACKAGE_FILES = ORIGINAL_ARTIFACTS + ["completion_receipt.json"]

EVIDENCE_ROW = {
    "evidence_id": "A000001", "source_id": "F000001", "kind": "audio",
    "locator": {"track": "F000001", "start_seconds": 0.0, "end_seconds": 4.0},
    "literal_text": "今天我们讨论比赛安排",
    "confidence": {"route": "asr_primary", "quality": "high"},
    "uncertainty": None,
}
RECORD_ROW = {
    "record_id": "R000001", "source_id": "F000001",
    "start_seconds": 0.0, "end_seconds": 4.0,
    "raw_text": "今天我们讨论比赛安排",
    "clean_literal": "今天我们讨论比赛安排。",
    "evidence_ids": ["A000001"], "certainty": "high",
    "time_precision": "asr_window",
    "edits": [{"type": "punctuation", "note": "句末补句号"}],
    "uncertainty": None,
}
RECONCILED = {
    "units": [{
        "unit_id": "U000001", "topic_path": ["开场"],
        "claim": "本次会议先确定比赛安排。", "certainty": "high",
        "evidence_ids": ["A000001"],
    }],
    "dispositions": [{"evidence_id": "A000001", "status": "accepted", "reason": None}],
}


def correction_row(record_id: str = "R000001", original: str = "张菁",
                   suggested: str = "张京", page: str = "P1",
                   evidence_id: str = "I000001", basis: str = "pinyin") -> dict:
    return {
        "record_id": record_id, "original": original, "suggested": suggested,
        "ocr_page_id": page, "ocr_evidence_id": evidence_id,
        "basis": basis, "engines_disagreed": True,
    }


def write_package_fixture(dirpath: Path) -> dict[str, Path]:
    dirpath.mkdir(parents=True, exist_ok=True)
    paths = {
        "evidence": dirpath / "evidence.jsonl",
        "records": dirpath / "literal_records.jsonl",
        "relations": dirpath / "relations.jsonl",
        "reconciled": dirpath / "reconciled.json",
    }
    paths["evidence"].write_text(
        json.dumps(EVIDENCE_ROW, ensure_ascii=False) + "\n", encoding="utf-8")
    paths["records"].write_text(
        json.dumps(RECORD_ROW, ensure_ascii=False) + "\n", encoding="utf-8")
    paths["relations"].write_text("", encoding="utf-8")  # zero-line JSONL contract
    paths["reconciled"].write_text(
        json.dumps(RECONCILED, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


class TestBuildPackageV3Surface(unittest.TestCase):
    """Default output invariance + additive --slides-dir/--ocr-corrections."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.fixture = write_package_fixture(self.tmp_path / "fixture")

    def run_build(self, out_name: str, extra: tuple[str, ...] = ()) -> Path:
        out_dir = self.tmp_path / out_name
        completed = run_script("build_package_v3.py", [
            "--evidence", str(self.fixture["evidence"]),
            "--literal-record", str(self.fixture["records"]),
            "--relations", str(self.fixture["relations"]),
            "--reconciled", str(self.fixture["reconciled"]),
            "--output-dir", str(out_dir),
            *extra,
        ])
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return out_dir

    def assert_packages_identical(self, left: Path, right: Path) -> None:
        self.assertEqual(
            sorted(path.name for path in left.iterdir()),
            sorted(path.name for path in right.iterdir()),
            "package file sets differ",
        )
        for name in PACKAGE_FILES:
            self.assertEqual(
                (left / name).read_bytes(), (right / name).read_bytes(),
                f"{name} is not byte-identical",
            )

    def completion_of(self, out_dir: Path) -> dict:
        return json.loads((out_dir / "completion_receipt.json").read_text(encoding="utf-8"))

    def test_default_run_pins_original_surface(self):
        out_dir = self.run_build("default")
        completion = self.completion_of(out_dir)
        self.assertEqual(completion["artifacts"], ORIGINAL_ARTIFACTS)
        self.assertNotIn("ocr_correction_unmatched", completion)
        self.assertFalse((out_dir / "幻灯片").exists())
        self.assertEqual(
            sorted(path.name for path in out_dir.iterdir()), sorted(PACKAGE_FILES))
        text_05 = (out_dir / "05_不确定与冲突.md").read_text(encoding="utf-8")
        self.assertNotIn("OCR 修正建议", text_05)
        text_02 = (out_dir / "02_逐句会议记录.md").read_text(encoding="utf-8")
        self.assertNotIn("〔OCR建议", text_02)
        self.assertIn("### R000001 · 00:00:00–00:00:04 · high", text_02)

    def test_default_runs_are_byte_identical(self):
        self.assert_packages_identical(self.run_build("pass-1"), self.run_build("pass-2"))

    def test_empty_corrections_file_behaves_like_default(self):
        baseline = self.run_build("baseline")
        empty = self.tmp_path / "ocr_corrections_empty.jsonl"
        empty.write_text("", encoding="utf-8")
        out_dir = self.run_build("empty-corrections",
                                 ("--ocr-corrections", str(empty)))
        self.assert_packages_identical(baseline, out_dir)
        self.assertNotIn(
            "OCR 修正建议",
            (out_dir / "05_不确定与冲突.md").read_text(encoding="utf-8"),
        )

    def test_missing_corrections_file_behaves_like_default(self):
        baseline = self.run_build("baseline-2")
        out_dir = self.run_build(
            "missing-corrections",
            ("--ocr-corrections", str(self.tmp_path / "does-not-exist.jsonl")),
        )
        self.assert_packages_identical(baseline, out_dir)

    def test_slides_dir_and_corrections_extend_package_additively(self):
        slides_src = self.tmp_path / "slides"
        slides_src.mkdir()
        page_bytes = {"P1.png": b"\x89PNG fake page 1 bytes", "P2.png": b"\x89PNG fake page 2 bytes"}
        for name, payload in page_bytes.items():
            (slides_src / name).write_bytes(payload)
        slides_meta = {
            "schema_version": 1, "video": "meeting.mp4", "region_source": "user",
            "fps_sampled": 1.0, "warnings": [],
            "slides": [
                {"page_id": "P1", "image": "P1.png", "time_ranges": [[0.0, 1.0]]},
                {"page_id": "P2", "image": "P2.png", "time_ranges": [[1.0, 2.0]]},
            ],
        }
        (slides_src / "slides.json").write_text(
            json.dumps(slides_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (slides_src / "ocr.jsonl").write_text("{}", encoding="utf-8")  # must NOT be copied

        corrections_path = self.tmp_path / "ocr_corrections.jsonl"
        rows = [
            correction_row(),  # matches R000001
            correction_row(record_id="R999999", original="李想", suggested="李湘",
                           page="P2", evidence_id="I000002", basis="edit_distance"),
        ]
        corrections_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        baseline = self.run_build("baseline-full")
        out_dir = self.run_build("full", (
            "--slides-dir", str(slides_src),
            "--ocr-corrections", str(corrections_path),
        ))

        # 幻灯片/ directory: P*.png + slides.json copied byte-for-byte, nothing else.
        slides_target = out_dir / "幻灯片"
        self.assertTrue(slides_target.is_dir())
        self.assertEqual(
            sorted(path.name for path in slides_target.iterdir()),
            ["P1.png", "P2.png", "slides.json"],
        )
        for name, payload in page_bytes.items():
            self.assertEqual((slides_target / name).read_bytes(), payload)
        self.assertEqual(
            (slides_target / "slides.json").read_bytes(),
            (slides_src / "slides.json").read_bytes(),
        )

        # Receipt: artifacts extended once; unmatched correction counted.
        completion = self.completion_of(out_dir)
        self.assertEqual(completion["artifacts"], ORIGINAL_ARTIFACTS + ["幻灯片/"])
        self.assertEqual(completion["ocr_correction_unmatched"], 1)

        # 05: advisory section lists every correction row (matched or not).
        text_05 = (out_dir / "05_不确定与冲突.md").read_text(encoding="utf-8")
        self.assertIn("## OCR 修正建议（录屏幻灯片证据）", text_05)
        self.assertIn("- 原词「张菁」→ 候选「张京」（证据 [P1]，依据 pinyin）", text_05)
        self.assertIn("- 原词「李想」→ 候选「李湘」（证据 [P2]，依据 edit_distance）", text_05)

        # 02: annotation is rendering-layer only — appended to the record
        # heading; the clean_literal body line stays untouched.
        text_02 = (out_dir / "02_逐句会议记录.md").read_text(encoding="utf-8")
        lines_02 = text_02.splitlines()
        heading = next(line for line in lines_02 if line.startswith("### R000001"))
        self.assertEqual(
            heading,
            "### R000001 · 00:00:00–00:00:04 · high 〔OCR建议: 张菁→张京 (P1)〕",
        )
        self.assertEqual(text_02.count("〔OCR建议"), 1)  # unmatched row not annotated
        self.assertIn("今天我们讨论比赛安排。", lines_02)
        body_line = next(line for line in lines_02 if line == "今天我们讨论比赛安排。")
        self.assertNotIn("〔OCR建议", body_line)

        # Everything else in the package is untouched by the new flags.
        for name in PACKAGE_FILES:
            if name in {"02_逐句会议记录.md", "05_不确定与冲突.md", "completion_receipt.json"}:
                continue
            self.assertEqual(
                (baseline / name).read_bytes(), (out_dir / name).read_bytes(),
                f"{name} changed even though only slides/corrections were added",
            )

    def test_corrections_only_never_touch_artifacts_list(self):
        corrections_path = self.tmp_path / "ocr_corrections_single.jsonl"
        corrections_path.write_text(
            json.dumps(correction_row(), ensure_ascii=False) + "\n", encoding="utf-8")
        out_dir = self.run_build("corrections-only",
                                 ("--ocr-corrections", str(corrections_path)))
        completion = self.completion_of(out_dir)
        self.assertEqual(completion["artifacts"], ORIGINAL_ARTIFACTS)
        self.assertEqual(completion["ocr_correction_unmatched"], 0)
        self.assertFalse((out_dir / "幻灯片").exists())

    def test_multiple_corrections_same_record_keep_file_order(self):
        corrections_path = self.tmp_path / "ocr_corrections_double.jsonl"
        rows = [
            correction_row(),
            correction_row(original="比赛", suggested="比拼", page="P2",
                           evidence_id="I000002", basis="edit_distance"),
        ]
        corrections_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")
        out_dir = self.run_build("double", ("--ocr-corrections", str(corrections_path)))
        text_02 = (out_dir / "02_逐句会议记录.md").read_text(encoding="utf-8")
        heading = next(line for line in text_02.splitlines()
                       if line.startswith("### R000001"))
        self.assertTrue(heading.endswith(
            " 〔OCR建议: 张菁→张京 (P1)〕 〔OCR建议: 比赛→比拼 (P2)〕"), heading)
        self.assertEqual(self.completion_of(out_dir)["ocr_correction_unmatched"], 0)

    def test_slides_rerun_into_same_output_dir_leaves_no_stale_pages(self):
        slides_a = self.tmp_path / "slides-a"
        slides_b = self.tmp_path / "slides-b"
        for directory, pages in ((slides_a, ("P1", "P2", "P3")), (slides_b, ("P1",))):
            directory.mkdir()
            for page in pages:
                (directory / f"{page}.png").write_bytes(
                    f"{directory.name}-{page}".encode("utf-8"))
            (directory / "slides.json").write_text(
                json.dumps({"schema_version": 1, "slides": []}, ensure_ascii=False) + "\n",
                encoding="utf-8")

        out_dir = self.run_build("rerun", ("--slides-dir", str(slides_a)))
        target = out_dir / "幻灯片"
        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            ["P1.png", "P2.png", "P3.png", "slides.json"],
        )

        # Rebuild into the SAME output dir with a smaller slide set: stale
        # P2/P3 pages must not survive (review 2026-09-30).
        out_dir_2 = self.run_build("rerun", ("--slides-dir", str(slides_b)))
        self.assertEqual(out_dir, out_dir_2)
        target = out_dir_2 / "幻灯片"
        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            ["P1.png", "slides.json"],
        )
        self.assertEqual((target / "P1.png").read_bytes(), b"slides-b-P1")
        completion = self.completion_of(out_dir_2)
        self.assertEqual(completion["artifacts"], ORIGINAL_ARTIFACTS + ["幻灯片/"])


class TestConfigToolsPython(unittest.TestCase):
    """config.SPEC: optional tools_python key, env > config.json > default."""

    REQUIRED_FAKE = {
        "whisper_bin": "/tmp/fake-whisper-cli",
        "whisper_model": "/tmp/fake-model.bin",
        "qwen_python": "/tmp/fake-qwen-python",
    }
    DEFAULT_TOOLS_PYTHON = str(WS / "vendor" / "tools-venv" / "bin" / "python")

    def test_spec_entry_shape(self):
        env, required, default = mst_config.SPEC["tools_python"]
        self.assertEqual(env, "MST_TOOLS_PYTHON")
        self.assertFalse(required)
        self.assertEqual(default, self.DEFAULT_TOOLS_PYTHON)

    def resolve_with(self, file_cfg: dict, env: dict) -> dict:
        clean_env = {key: value for key, value in env.items()}
        with mock.patch.object(mst_config, "_load_file", return_value=file_cfg), \
                mock.patch.dict(os.environ, clean_env, clear=True):
            return mst_config.resolve()

    def test_env_wins_over_file_and_default(self):
        resolved = self.resolve_with(
            {**self.REQUIRED_FAKE, "tools_python": "/from/config-json"},
            {"MST_TOOLS_PYTHON": "/from/env"},
        )
        self.assertEqual(resolved["tools_python"], "/from/env")

    def test_file_wins_over_default(self):
        resolved = self.resolve_with(
            {**self.REQUIRED_FAKE, "tools_python": "/from/config-json"}, {},
        )
        self.assertEqual(resolved["tools_python"], "/from/config-json")

    def test_default_when_unset_everywhere(self):
        resolved = self.resolve_with(dict(self.REQUIRED_FAKE), {})
        self.assertEqual(resolved["tools_python"], self.DEFAULT_TOOLS_PYTHON)

    def test_key_is_optional(self):
        # A config.json without tools_python must never trigger the collective
        # missing-required-keys error.
        resolved = self.resolve_with(dict(self.REQUIRED_FAKE), {})
        self.assertIn("tools_python", resolved)


if __name__ == "__main__":
    unittest.main()
