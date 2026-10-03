"""Consumer-side regressions for manifest DERIVED entries (T6.5).

Pins the additive surface required by docs/screen-recording-parsing-design.md
§4.3/§4.4 for the two consumers that prepare_audio.py (committed) already
handled:

- segment_asr_windows.py: audio items may carry an absolute "path" override
  (derived media lives in runs/, not under source_root); a relative override
  fails loud before that item's outputs (per-item guard, matching the
  script's existing mid-loop failure semantics); without "path" the classic
  source_root/relative_path resolution still applies (both directions).
- validate_package_v3.py sources_immutable: same override contract; original
  AND derived eligible entries are hash-checked (§4.3 「原始+派生」口径 — the
  loop enumerates manifest["files"] itself, so derived entries can neither
  dodge nor break the source check).
- validate_package_v3.py slide_evidence_complete: `### I######` console image
  evidence headings (§4.4) satisfy the check while the classic `### E######`
  flow stays unchanged, and unrendered headings still fail for both series.

Runs under plain system python3 (stdlib only). ffmpeg on PATH is required for
the segment-window media fixtures (documented pipeline dependency); those
tests skip cleanly when it is absent. Validator fixtures are built with the
committed build_package_v3.py so the validator is exercised against a real
package rather than a hand-rolled one.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
for _p in (str(WS),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CORE_SCRIPTS = WS / "core" / "scripts"

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = unittest.skipUnless(
    FFMPEG, "ffmpeg required (documented pipeline dependency)")


def run_script(script: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(CORE_SCRIPTS / script), *args],
        capture_output=True, text=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_wav(path: Path, seconds: float = 0.5, frequency: float = 440.0,
              rate: int = 16000) -> None:
    """Minimal mono 16-bit PCM wav — the shape segment_asr_windows expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = int(rate * seconds)
    samples = array.array(
        "h", (int(12000 * math.sin(2 * math.pi * frequency * index / rate))
              for index in range(count)))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(samples.tobytes())


# ---------------------------------------------------------------------------
# segment_asr_windows.py — absolute "path" override for derived audio (fix 1)
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestSegmentAsrWindowsDerivedPath(unittest.TestCase):
    """segment_asr_windows: derived manifest entries carry an absolute `path`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source_root = self.root / "input" / "event"
        self.source_root.mkdir(parents=True)
        # Derived audio as produced by video_ingest (design §4.3): lives in
        # runs/, NOT under source_root.
        self.derived_dir = self.root / "runs" / "event"
        self.derived_dir.mkdir(parents=True)
        self.prepared = self.root / "prepared"

    def write_manifest(self, item: dict) -> Path:
        manifest_path = self.root / f"manifest-{item['source_id']}.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "source_root": str(self.source_root),
            "file_count": 1,
            "files": [item],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    def prepare_tracks(self, source_id: str) -> None:
        """prepare_audio outputs the script reads for duration + aux routes."""
        write_wav(self.prepared / "artifacts/audio/normalized" / f"{source_id}.wav")
        write_wav(self.prepared / "artifacts/audio/enhanced" / f"{source_id}.wav",
                  frequency=520.0)

    def run_segment(self, manifest: Path, out_name: str) -> tuple[subprocess.CompletedProcess, Path]:
        output_dir = self.root / out_name
        return run_script("segment_asr_windows.py", [
            "--manifest", str(manifest),
            "--prepared-run", str(self.prepared),
            "--output-dir", str(output_dir),
            "--window-seconds", "20", "--overlap-seconds", "5",
        ]), output_dir

    def receipt_of(self, output_dir: Path) -> dict:
        return json.loads(
            (output_dir / "window_receipt.json").read_text(encoding="utf-8"))

    def test_derived_absolute_path_override_is_used(self):
        derived_audio = self.derived_dir / "extracted_audio.m4a"
        completed_fixture = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             str(derived_audio)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed_fixture.returncode, 0, completed_fixture.stderr[-800:])
        self.prepare_tracks("F000007")
        # relative_path resolves nowhere under source_root: success is only
        # possible when the absolute override is honored (prepare_audio parity).
        manifest = self.write_manifest({
            "source_id": "F000007", "kind": "audio",
            "relative_path": "extracted_audio.m4a",
            "path": str(derived_audio),
            "eligible_source": True, "derived_from": "meeting.mp4",
        })
        completed, output_dir = self.run_segment(manifest, "segment-override")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        receipt = self.receipt_of(output_dir)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["window_count"], 1)
        routes = sorted(record["route"] for record in receipt["records"])
        self.assertEqual(
            routes, ["impulse_noise_reduced", "normalized", "original"])
        original = next(record for record in receipt["records"]
                        if record["route"] == "original")
        self.assertTrue(original["output"].startswith(
            str(output_dir / "flat") + "/"))
        with wave.open(original["output"], "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getframerate(), 16000)
            self.assertGreater(reader.getnframes(), 0)

    def test_relative_path_override_fails_loud(self):
        # A relative "path" override would resolve against the process CWD and
        # segment the wrong file: it must fail loudly, before any of THIS
        # item's windows or the receipt are produced (prepare_audio parity,
        # review 2026-09-30). The guard is per-item and fires mid-loop —
        # consistent with the script's existing failure semantics; here the
        # bad item is the only one, so nothing at all is written.
        manifest = self.write_manifest({
            "source_id": "F000010", "kind": "audio",
            "relative_path": "ignored-relative.wav",
            "path": "extracted_audio.m4a",  # relative override -> rejected
            "eligible_source": True, "derived_from": "meeting.mp4",
        })
        completed, output_dir = self.run_segment(manifest, "segment-relative-override")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("manifest path override must be absolute", completed.stderr)
        self.assertIn("F000010", completed.stderr)
        self.assertFalse((output_dir / "window_receipt.json").exists())
        self.assertEqual(list((output_dir / "flat").iterdir()), [])

    def test_without_path_relative_resolution_still_applies(self):
        # No "path" key -> classic source_root/relative_path resolution.
        write_wav(self.source_root / "recording.wav")
        self.prepare_tracks("F000001")
        manifest = self.write_manifest({
            "source_id": "F000001", "kind": "audio",
            "relative_path": "recording.wav",
            "eligible_source": True,
        })
        completed, output_dir = self.run_segment(manifest, "segment-classic")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        receipt = self.receipt_of(output_dir)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(len(receipt["records"]), 3)
        for record in receipt["records"]:
            self.assertTrue(Path(record["output"]).is_file(), record["output"])

    def test_without_path_missing_source_fails_loud(self):
        # Negative pin: without "path" there is no fallback magic — a source
        # missing under source_root fails loudly instead of inventing a file.
        self.prepare_tracks("F000008")
        manifest = self.write_manifest({
            "source_id": "F000008", "kind": "audio",
            "relative_path": "missing-recording.wav",
            "eligible_source": True,
        })
        completed, output_dir = self.run_segment(manifest, "segment-missing")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("window extraction failed", completed.stderr)
        self.assertIn("F000008", completed.stderr)
        self.assertFalse((output_dir / "window_receipt.json").exists())


# ---------------------------------------------------------------------------
# validate_package_v3.py — derived sources_immutable + I###### evidence
# (fixes 2, 3, and the §4.3 「原始+派生」口径 pin for fix 4)
# ---------------------------------------------------------------------------

class TestValidatePackageV3DerivedSources(unittest.TestCase):
    """validate_package_v3: derived manifest entries and console I-ids."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.source_root = self.tmp_path / "input" / "event"
        self.source_root.mkdir(parents=True)
        self.run_dir = self.tmp_path / "runs" / "event"
        # Original entry under source_root (classic resolution).
        self.original_audio = self.source_root / "recording.wav"
        self.original_audio.write_bytes(b"original-recording-bytes")
        # Derived entries (§4.3) live under runs/; their relative_path values
        # deliberately resolve nowhere under source_root, so only the absolute
        # "path" override can find them.
        self.derived_audio = self.run_dir / "extracted_audio.m4a"
        self.derived_audio.parent.mkdir(parents=True)
        self.derived_audio.write_bytes(b"derived-extracted-audio-bytes")
        self.derived_slide = self.run_dir / "slides" / "P1.png"
        self.derived_slide.parent.mkdir(parents=True)
        self.derived_slide.write_bytes(b"\x89PNG derived slide bytes")
        self._manifest_seq = 0

    # -- manifest fixture ----------------------------------------------------

    def write_manifest(self, *, path_overrides: dict[str, str] | None = None) -> Path:
        files = [
            {"source_id": "F000001", "kind": "audio",
             "relative_path": "recording.wav",
             "sha256": sha256_file(self.original_audio),
             "eligible_source": True},
            {"source_id": "F000002", "kind": "audio", "derived_from": "meeting.mp4",
             "relative_path": "extracted_audio.m4a", "path": str(self.derived_audio),
             "sha256": sha256_file(self.derived_audio),
             "eligible_source": True},
            {"source_id": "F000003", "kind": "image", "derived_from": "meeting.mp4",
             "page_id": "P1", "time_ranges": [[0.0, 1.0]],
             "relative_path": "slides/P1.png", "path": str(self.derived_slide),
             "sha256": sha256_file(self.derived_slide),
             "eligible_source": True},
        ]
        for source_id, override in (path_overrides or {}).items():
            next(item for item in files if item["source_id"] == source_id)["path"] = override
        self._manifest_seq += 1
        manifest_path = self.tmp_path / f"manifest-{self._manifest_seq}.json"
        # file_count follows the §4.3 「原始+派生」口径 (1 original + 2 derived);
        # the validator itself enumerates files[] directly and never reads it.
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "source_root": str(self.source_root),
            "file_count": len(files),
            "files": files,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    # -- package fixture (built by the committed build_package_v3.py) ---------

    def build_package(self, name: str, *, image_evidence_id: str = "I000001") -> dict[str, Path]:
        fixture = self.tmp_path / f"fixture-{name}"
        fixture.mkdir(parents=True)
        audio_evidence = {
            "evidence_id": "A000001", "source_id": "F000001", "kind": "audio",
            "locator": {"track": "F000001", "start_seconds": 0.0, "end_seconds": 4.0},
            "literal_text": "今天我们讨论比赛安排",
            "confidence": {"route": "asr_primary", "quality": "high"},
            "uncertainty": None,
        }
        image_evidence = {
            "evidence_id": image_evidence_id, "source_id": "F000003", "kind": "image",
            "locator": {"page_id": "P1", "time_ranges": [[0.0, 1.0]], "video": "meeting.mp4"},
            "literal_text": "比赛安排：下周三下午选拔",
            "confidence": {"route": "apple_vision", "quality": "medium"},
            "uncertainty": None,
        }
        record = {
            "record_id": "R000001", "source_id": "F000001",
            "start_seconds": 0.0, "end_seconds": 4.0,
            "raw_text": "今天我们讨论比赛安排",
            "clean_literal": "今天我们讨论比赛安排。",
            "evidence_ids": ["A000001"], "certainty": "high",
            "time_precision": "asr_window", "uncertainty": None,
        }
        relation = {
            "relation_id": "R000001", "slide_source_id": "F000003",
            "slide_evidence_ids": [image_evidence_id],
            "candidate_audio_records": [],
            "relation": "unknown", "decision_route": "temporal_overlap",
        }
        reconciled = {
            "units": [{"unit_id": "U000001", "topic_path": ["开场"],
                       "claim": "本次会议先确定比赛安排。", "certainty": "high",
                       "evidence_ids": ["A000001"]}],
            "dispositions": [
                {"evidence_id": "A000001", "status": "accepted", "reason": None},
                {"evidence_id": image_evidence_id, "status": "accepted", "reason": None},
            ],
        }
        paths = {
            "evidence": fixture / "evidence.jsonl",
            "records": fixture / "literal_records.jsonl",
            "relations": fixture / "relations.jsonl",
            "reconciled": fixture / "reconciled.json",
            "package": self.tmp_path / f"package-{name}",
        }
        paths["evidence"].write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in (audio_evidence, image_evidence)), encoding="utf-8")
        paths["records"].write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        paths["relations"].write_text(
            json.dumps(relation, ensure_ascii=False) + "\n", encoding="utf-8")
        paths["reconciled"].write_text(
            json.dumps(reconciled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        completed = run_script("build_package_v3.py", [
            "--evidence", str(paths["evidence"]),
            "--literal-record", str(paths["records"]),
            "--relations", str(paths["relations"]),
            "--reconciled", str(paths["reconciled"]),
            "--output-dir", str(paths["package"]),
        ])
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return paths

    def run_validate(self, paths: dict[str, Path],
                     manifest: Path) -> subprocess.CompletedProcess:
        return run_script("validate_package_v3.py", [
            "--manifest", str(manifest),
            "--literal-record", str(paths["records"]),
            "--relations", str(paths["relations"]),
            "--package-dir", str(paths["package"]),
        ])

    def receipt_of(self, paths: dict[str, Path]) -> dict:
        return json.loads(
            (paths["package"] / "validation_receipt.json").read_text(encoding="utf-8"))

    # -- sources_immutable with derived entries (fix 2) -----------------------

    def test_derived_entries_pass_sources_immutable(self):
        paths = self.build_package("derived-pass")
        completed = self.run_validate(paths, self.write_manifest())
        self.assertEqual(completed.returncode, 0,
                         completed.stdout[-500:] + completed.stderr[-2000:])
        receipt = self.receipt_of(paths)
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["checks"]["sources_immutable"])
        self.assertEqual(receipt["source_mismatches"], [])
        # §4.4: console image-evidence rows use I###### ids and satisfy the
        # slide-evidence coverage check (fix 3, I-direction).
        self.assertTrue(receipt["checks"]["slide_evidence_complete"])
        slide_text = (paths["package"] / "03_PPT补充信息.md").read_text(encoding="utf-8")
        self.assertIn("### I000001", slide_text)

    def test_modified_derived_file_fails_sources_immutable(self):
        paths = self.build_package("derived-modified")
        manifest = self.write_manifest()  # hashes captured pre-tamper
        self.derived_audio.write_bytes(b"tampered-after-manifest-bytes")
        completed = self.run_validate(paths, manifest)
        self.assertEqual(completed.returncode, 2)
        receipt = self.receipt_of(paths)
        self.assertEqual(receipt["status"], "FAIL")
        self.assertFalse(receipt["checks"]["sources_immutable"])
        self.assertEqual(receipt["source_mismatches"], ["F000002"])

    def test_missing_derived_file_fails_sources_immutable(self):
        paths = self.build_package("derived-missing")
        manifest = self.write_manifest()
        self.derived_slide.unlink()
        completed = self.run_validate(paths, manifest)
        self.assertEqual(completed.returncode, 2)
        receipt = self.receipt_of(paths)
        self.assertEqual(receipt["status"], "FAIL")
        self.assertFalse(receipt["checks"]["sources_immutable"])
        self.assertEqual(receipt["source_mismatches"], ["F000003"])

    def test_original_and_derived_entries_are_both_checked(self):
        # §4.3 「原始+派生」口径 (fix-4 pin): the source walk enumerates
        # manifest["files"] itself, so originals and derived entries are all
        # hash-checked — tampering with both surfaces both source ids.
        paths = self.build_package("both-counted")
        manifest = self.write_manifest()
        self.original_audio.write_bytes(b"tampered-original")
        self.derived_audio.write_bytes(b"tampered-derived")
        completed = self.run_validate(paths, manifest)
        self.assertEqual(completed.returncode, 2)
        receipt = self.receipt_of(paths)
        self.assertFalse(receipt["checks"]["sources_immutable"])
        self.assertEqual(set(receipt["source_mismatches"]), {"F000001", "F000002"})

    def test_relative_path_override_fails_loud(self):
        paths = self.build_package("relative-override")
        manifest = self.write_manifest(
            path_overrides={"F000002": "extracted_audio.m4a"})
        completed = self.run_validate(paths, manifest)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("manifest path override must be absolute", completed.stderr)
        self.assertIn("F000002", completed.stderr)
        # Fail-loud happens before the receipt is written (prepare_audio parity).
        self.assertFalse((paths["package"] / "validation_receipt.json").exists())

    # -- slide_evidence_complete across both id series (fix 3) ----------------

    def test_classic_e_evidence_flow_unchanged(self):
        paths = self.build_package("e-flow", image_evidence_id="E000001")
        completed = self.run_validate(paths, self.write_manifest())
        self.assertEqual(completed.returncode, 0,
                         completed.stdout[-500:] + completed.stderr[-2000:])
        receipt = self.receipt_of(paths)
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["checks"]["slide_evidence_complete"])
        slide_text = (paths["package"] / "03_PPT补充信息.md").read_text(encoding="utf-8")
        self.assertIn("### E000001", slide_text)

    def test_unrendered_slide_evidence_still_fails(self):
        # Negative pin for both id series: the regex extension is a superset,
        # not a bypass — a heading missing from 03 must still fail the check.
        for name, evidence_id in (("missing-i", "I000001"), ("missing-e", "E000001")):
            with self.subTest(evidence_id=evidence_id):
                paths = self.build_package(name, image_evidence_id=evidence_id)
                slide_path = paths["package"] / "03_PPT补充信息.md"
                slide_path.write_text(
                    slide_path.read_text(encoding="utf-8").replace(
                        f"### {evidence_id}", "### removed-heading"),
                    encoding="utf-8")
                completed = self.run_validate(paths, self.write_manifest())
                self.assertEqual(completed.returncode, 2)
                receipt = self.receipt_of(paths)
                self.assertFalse(receipt["checks"]["slide_evidence_complete"])


if __name__ == "__main__":
    unittest.main()
