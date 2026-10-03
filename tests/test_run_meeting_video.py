"""T6: run_meeting 录屏接线（14 站管线）契约测试.

Pins the screen-recording wiring inside run_meeting.py
(docs/screen-recording-parsing-design.md §3.4/§4.2-§4.5/§5):

- find_input_events: video 成为新输入 kind（事件 = audio ∨ notes ∨ video）
- STAGE_ORDER: 12→14 站（video_ingest 紧随 inventory、slide_align 紧随 relevance，
  id+label 逐字钉死）
- 纯音频事件逐字节回归：evidence/merge/报告包与 12 站时代的 golden 输出完全一致
  （golden 于改动前从当时代码实跑捕获，2026-10-01），零 image 行、零侧车文件
- 视频事件 stage 级端到端（synth_video 真 fixture + tools-venv 真工具）：
  manifest 派生条目（绝对 path/幂等）、I###### 证据行、relations 覆盖铁律、
  audio-only 侧车、报告包 幻灯片/ + 00_使用说明 导航行
- temporal_overlap / lexical 双路由的确定性对齐（hand-built spans，纯 stdlib）
- §5 降级矩阵：无声视频 fail-fast 零工件、区域不可信仅抽音轨、独立音频优先、
  多视频取首个、OCR 修正建议失败降级不致命

Runs green under system python3 (video-dependent cases skip); fully under
vendor/tools-venv/bin/python. Heavy cases share ONE generated fixture
(spec §6: keep fixtures small — 8s/6s 640x400).
"""
from __future__ import annotations

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
os.environ.setdefault("MST_WHISPER_BIN", "/dummy/whisper-cli")
os.environ.setdefault("MST_WHISPER_MODEL", "/dummy/model.bin")
os.environ.setdefault("MST_QWEN_PYTHON", "/dummy/python")
for _p in (str(WS), str(WS / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_meeting as rm  # noqa: E402
import synth_video  # noqa: E402  (module-level cv2 import is guarded inside)

TOOLS_PYTHON = WS / "vendor" / "tools-venv" / "bin" / "python"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAS_SWIFTC = bool(shutil.which("swiftc"))


def _tools_venv_ready() -> bool:
    if not TOOLS_PYTHON.is_file():
        return False
    try:
        proc = subprocess.run([str(TOOLS_PYTHON), "-c", "import cv2, numpy"],
                              capture_output=True, timeout=60)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_TOOLS_READY = _tools_venv_ready()
# synth_video builds fixtures IN-PROCESS: cv2/numpy must exist in the CURRENT
# interpreter (they don't under system python3 — same guard as T3's tests).
_HAS_CV2 = synth_video.cv2 is not None and synth_video.np is not None
requires_video = unittest.skipUnless(
    _TOOLS_READY and HAS_FFMPEG and _HAS_CV2,
    "vendor/tools-venv (numpy+cv2) + ffmpeg/ffprobe required for video cases")
requires_venv_only = unittest.skipUnless(
    _TOOLS_READY, "vendor/tools-venv required (suggest_ocr_corrections runs under it)")

# spec §3.4: exact station table (ids AND labels), 12→14.
EXPECTED_STAGES = [
    ("inventory", "文件清单"),
    ("video_ingest", "视频分解（音轨/幻灯片）"),
    ("audio_prepare", "音频预处理（降噪）"),
    ("lang_probe", "语言探测"),
    ("asr", "语音识别"),
    ("literal", "组装逐句记录"),
    ("evidence", "生成证据"),
    ("relevance", "无关话语过滤"),
    ("slide_align", "幻灯片对齐"),
    ("reconcile", "主题提取与索引"),
    ("audit", "claim 保真审计"),
    ("notes", "笔记佐证"),
    ("package", "构建报告包"),
    ("validate", "验证与质量门禁"),
]

# §5 行1 中文提示（T3 AUDIO_MISSING_MESSAGE 镜像）
AUDIO_MISSING_MESSAGE = "录屏无声且事件内无独立音频文件:需要含声录屏或另配音频"

# §4.6 输出行 schema（T5 钉死）
CORRECTION_FIELDS = {"record_id", "original", "suggested", "ocr_page_id",
                     "ocr_evidence_id", "basis", "engines_disagreed"}
DISAGREEMENT_MARK = ("acoustic_route_disagreement; "
                     "note text must not repair this transcript")

# ---------------------------------------------------------------------------
# Golden pure-audio outputs — captured byte-for-byte from run_meeting.py
# BEFORE the T6 edit (2026-10-01, 12-station code) on the fixture below.
# 纯音频路径逐字节回归口径（计划「兼容边界」第一条）。
# ---------------------------------------------------------------------------

GOLDEN_RECORDS = [
    {"record_id": "R000001", "source_id": "F000001_0_4000", "start_seconds": 0.0, "end_seconds": 4.0,
     "clean_literal": "今天我们讨论比赛安排。", "raw_text": "今天我们讨论比赛安排。",
     "certainty": "medium", "uncertainty": None, "route_agreement_min": 1.0,
     "engine": "whisper-auto (per-track en/code-switching)", "evidence_ids": ["A000001"]},
    {"record_id": "R000002", "source_id": "F000001_4000_9000", "start_seconds": 4.0, "end_seconds": 9.0,
     "clean_literal": "第一项是招新时间表。", "raw_text": "第一项是招新时间表。",
     "certainty": "low", "uncertainty": DISAGREEMENT_MARK,
     "route_agreement_min": 0.81, "engine": "qwen-window-zh", "evidence_ids": ["A000002"]},
    {"record_id": "R000003", "source_id": "F000001_9000_12000", "start_seconds": 9.0, "end_seconds": 12.0,
     "clean_literal": "会议室预订找行政老师。", "raw_text": "会议室预订找行政老师。",
     "certainty": "medium", "uncertainty": None, "route_agreement_min": 0.97,
     "engine": "qwen-window-zh", "evidence_ids": ["A000003"]},
]
GOLDEN_LITERAL_RECEIPT = {
    "status": "complete", "record_count": 3,
    "engine": "per-track hybrid (whisper 1 / qwen-zh 1)",
    "tracks": {"F000001": {"engine": "qwen", "records": 3}},
}

GOLDEN_EVIDENCE_JSONL = (
    '{"evidence_id": "A000001", "source_id": "F000001_0_4000", "kind": "audio", '
    '"locator": {"start_seconds": 0.0, "end_seconds": 4.0, "window_id": "F000001_0_4000"}, '
    '"literal_text": "今天我们讨论比赛安排。", "confidence": {"route": "per-track hybrid (whisper 1 / qwen-zh 1)", '
    '"quality": "medium", "agreement": 1.0}, "uncertainty": null}\n'
    '{"evidence_id": "A000002", "source_id": "F000001_4000_9000", "kind": "audio", '
    '"locator": {"start_seconds": 4.0, "end_seconds": 9.0, "window_id": "F000001_4000_9000"}, '
    '"literal_text": "第一项是招新时间表。", "confidence": {"route": "per-track hybrid (whisper 1 / qwen-zh 1)", '
    '"quality": "low", "agreement": 0.81}, "uncertainty": "acoustic_route_disagreement; '
    'note text must not repair this transcript"}\n'
    '{"evidence_id": "A000003", "source_id": "F000001_9000_12000", "kind": "audio", '
    '"locator": {"start_seconds": 9.0, "end_seconds": 12.0, "window_id": "F000001_9000_12000"}, '
    '"literal_text": "会议室预订找行政老师。", "confidence": {"route": "per-track hybrid (whisper 1 / qwen-zh 1)", '
    '"quality": "medium", "agreement": 0.97}, "uncertainty": null}\n'
)

GOLDEN_EVIDENCE_RECEIPT = (
    '{\n  "status": "complete",\n  "evidence_count": 3,\n'
    '  "engine": "per-track hybrid (whisper 1 / qwen-zh 1)"\n}\n'
)

GOLDEN_RECONCILED = {
    "units": [{"unit_id": "U000001", "topic_path": ["开场"], "claim": "会议先确定比赛安排。",
               "certainty": "high", "evidence_ids": ["A000001", "A000002"]}],
    "dispositions": [{"evidence_id": "A000001", "status": "accepted", "reason": None},
                     {"evidence_id": "A000002", "status": "uncertain", "reason": "路线分歧"}],
}

GOLDEN_MERGED = (
    '{\n  "units": [\n    {\n      "unit_id": "U000001",\n      "topic_path": [\n        "开场"\n      ],\n'
    '      "claim": "会议先确定比赛安排。",\n      "certainty": "high",\n      "evidence_ids": [\n'
    '        "A000001",\n        "A000002"\n      ]\n    }\n  ],\n  "dispositions": [\n    {\n'
    '      "evidence_id": "A000001",\n      "status": "accepted",\n      "reason": null\n    },\n    {\n'
    '      "evidence_id": "A000002",\n      "status": "uncertain",\n      "reason": "路线分歧"\n    },\n    {\n'
    '      "evidence_id": "A000003",\n      "status": "excluded_logistics",\n'
    '      "note": "excluded from topic reconcile by relevance filter"\n    }\n  ]\n}\n'
)

GOLDEN_GUIDE = (
    "# 使用说明\n\n"
    "- 问演讲者具体说了什么：查《02_逐句会议记录》。\n"
    "- 快速定位主题：先查《01_主题索引》。\n"
    "- 阅读结论：查《04_会议报告》。\n"
    "- 低置信度内容：查《05_不确定与冲突.md》。\n"
    "- 笔记佐证：查《06_笔记佐证与冲突.md》。\n"
    "- AI 检索：使用 meeting.db 或 query_meeting.py。"
)

GOLDEN_PACKAGE_FILES = [
    "00_使用说明.md", "01_主题索引.md", "02_逐句会议记录.md", "03_PPT补充信息.md",
    "04_会议报告.md", "05_不确定与冲突.md", "completion_receipt.json",
    "coverage_receipt.json", "meeting.db",
]
GOLDEN_ARTIFACTS = [
    "00_使用说明.md", "01_主题索引.md", "02_逐句会议记录.md", "03_PPT补充信息.md",
    "04_会议报告.md", "05_不确定与冲突.md", "meeting.db", "coverage_receipt.json",
]

NAV_LINE = "- 问 PPT 页面内容/对应发言：查《03_PPT补充信息》与 幻灯片/ 目录。"
AUTO_REGION_ANNOTATION = "幻灯片区域为自动检测，未经人工确认。"
DEGRADED_REGION_ANNOTATION = "未识别到幻灯片区域，本次仅处理音轨。"
GUIDE_LAST_BULLET = "- AI 检索：使用 meeting.db 或 query_meeting.py。"

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

_SESSION: dict = {}


def setUpModule() -> None:
    if not (_TOOLS_READY and HAS_FFMPEG and _HAS_CV2):
        return  # video classes skip under plain system python3
    tmp = Path(tempfile.mkdtemp(prefix="run-meeting-video-fixture-"))
    # 8s/6s 640x400 fixtures (spec §6: keep them small). mouse_crossings=[]
    # keeps page cuts unambiguous for segmentation on the short timeline.
    voiced = synth_video.generate_meeting_video(
        tmp / "meeting.mp4", duration_s=8.0, fps=5, size=(640, 400),
        audio=True, mouse_crossings=[])
    silent = synth_video.generate_meeting_video(
        tmp / "silent.mp4", duration_s=6.0, fps=5, size=(640, 400),
        audio=False, mouse_crossings=[])
    motion = synth_video.generate_meeting_video(
        tmp / "motion.mp4", duration_s=6.0, fps=5, size=(640, 400),
        ppt_rect=None, audio=True)
    _SESSION.update({"tmp": tmp, "voiced": voiced, "silent": silent, "motion": motion})


def tearDownModule() -> None:
    tmp = _SESSION.get("tmp")
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)


def write_region(dir_path: Path, meta: dict, video_name: str | None = None,
                 source: str = "user", rect_px=None) -> Path:
    """region.json per spec §4.1 (RELATIVE rect from fixture ground truth)."""
    width, height = meta["size"]
    x, y, w, h = rect_px if rect_px is not None else meta["ppt_rect_px"]
    payload = {
        "schema_version": 1,
        "video": video_name or Path(meta["path"]).name,
        "source": source,
        "rect": {"x": x / width, "y": y / height, "w": w / width, "h": h / height},
        "confidence": 0.9,
        "created_ts": 1759200000.0,
    }
    path = dir_path / "region.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def make_run(root: Path, name: str, videos: dict[str, Path], audio: dict[str, Path] | None = None,
             region_meta: dict | None = None, region_video: str | None = None) -> tuple[Path, dict]:
    """Build input/<event> + runs/<event>/source + real inventory manifest.

    Returns (run_dir, event dict). videos/audio map target-name -> source path.
    """
    event_dir = root / "input" / name
    event_dir.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / name
    source_dir = run_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    video_paths = []
    for target, src in sorted(videos.items()):
        shutil.copy2(src, event_dir / target)
        shutil.copy2(src, source_dir / target)
        video_paths.append(event_dir / target)
    audio_paths = []
    for target, src in sorted((audio or {}).items()):
        shutil.copy2(src, event_dir / target)
        shutil.copy2(src, source_dir / target)
        audio_paths.append(event_dir / target)
    if region_meta is not None:
        write_region(event_dir, region_meta, video_name=region_video)
    manifest_path = rm.stage_inventory(run_dir, source_dir)
    event = {"name": name, "dir": event_dir, "audio": audio_paths,
             "video": video_paths, "notes": None}
    return run_dir, event


def make_literal_records(run_dir: Path, records: list[dict],
                         engine: str = "test-fixture") -> Path:
    records_path = run_dir / "literal_records.jsonl"
    with records_path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rm.atomic_json(run_dir / "literal_receipt.json",
                   {"status": "complete", "record_count": len(records),
                    "engine": engine, "tracks": {}})
    return records_path


def record_row(record_id: str, start: float, end: float, text: str,
               source_id: str = "F000007_0000001600_0000004000",
               disagreed: bool = False) -> dict:
    return {
        "record_id": record_id, "source_id": source_id,
        "start_seconds": start, "end_seconds": end,
        "clean_literal": text, "raw_text": text,
        "certainty": "low" if disagreed else "medium",
        "uncertainty": DISAGREEMENT_MARK if disagreed else None,
        "route_agreement_min": 0.8123 if disagreed else 1.0,
        "engine": "qwen-window-zh", "time_precision": "asr_window",
        "evidence_ids": [f"A{record_id[1:]}"],
    }


# ---------------------------------------------------------------------------
# (b) STAGE_ORDER — 14 stations, exact ids + labels
# ---------------------------------------------------------------------------

class StageOrderVideoTest(unittest.TestCase):
    def test_fourteen_stations_exact_ids_and_labels(self):
        self.assertEqual(rm.STAGE_ORDER, EXPECTED_STAGES)

    def test_new_station_positions(self):
        keys = [k for k, _ in rm.STAGE_ORDER]
        self.assertEqual(len(keys), 14)
        self.assertEqual(keys[1], "video_ingest")          # 紧随 inventory (§3.4)
        self.assertEqual(keys[keys.index("relevance") + 1], "slide_align")  # 紧随 relevance
        self.assertEqual(rm._stage_index("video_ingest.probe"), 2)
        self.assertEqual(rm._stage_index("video_ingest.audio"), 2)
        self.assertEqual(rm._stage_index("video_ingest.frames"), 2)
        self.assertEqual(rm._stage_index("video_ingest.segment"), 2)
        self.assertEqual(rm._stage_index("video_ingest.ocr"), 2)
        self.assertEqual(rm._stage_index("slide_align"), 9)


# ---------------------------------------------------------------------------
# (a) find_input_events — video is a new input kind
# ---------------------------------------------------------------------------

class FindInputEventsVideoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(rm, "INPUT_DIR", self.root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def touch(self, *rel: str) -> None:
        for item in rel:
            path = self.root / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def test_video_only_dir_forms_event(self):
        self.touch("lecture/meeting.mp4")
        events = rm.find_input_events()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["name"], "lecture")
        self.assertEqual([p.name for p in ev["video"]], ["meeting.mp4"])
        self.assertEqual(ev["audio"], [])
        self.assertIsNone(ev["notes"])

    def test_audio_only_event_has_empty_video_list(self):
        # shape pin: every event dict carries the "video" key (consumers use it)
        self.touch("lecture/talk.m4a", "lecture/notes.md")
        ev = rm.find_input_events()[0]
        self.assertEqual(ev["video"], [])
        self.assertEqual([p.name for p in ev["audio"]], ["talk.m4a"])

    def test_mixed_audio_video_notes_event(self):
        self.touch("ev/a.m4a", "ev/b.mp4", "ev/c.mov", "ev/notes.md")
        ev = rm.find_input_events()[0]
        self.assertEqual([p.name for p in ev["video"]], ["b.mp4", "c.mov"])  # sorted
        self.assertEqual([p.name for p in ev["audio"]], ["a.m4a"])
        self.assertEqual(ev["notes"].name, "notes.md")

    def test_loose_video_forms_misc_event(self):
        self.touch("rec.mov")
        events = rm.find_input_events()
        self.assertEqual([e["name"] for e in events], ["misc"])
        self.assertEqual([p.name for p in events[0]["video"]], ["rec.mov"])

    def test_loose_mixed_all_kinds_single_misc(self):
        self.touch("a.m4a", "v.mkv", "notes.md")
        events = rm.find_input_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "misc")
        self.assertEqual([p.name for p in events[0]["audio"]], ["a.m4a"])
        self.assertEqual([p.name for p in events[0]["video"]], ["v.mkv"])
        self.assertEqual(events[0]["notes"].name, "notes.md")

    def test_all_video_extensions_accepted(self):
        for i, ext in enumerate(sorted(rm.VIDEO_EXTENSIONS)):
            self.touch(f"ev{i}/meeting{ext}")
        events = rm.find_input_events()
        self.assertEqual(len(events), len(rm.VIDEO_EXTENSIONS))
        self.assertTrue(all(e["video"] for e in events))

    def test_video_exts_come_from_meeting_pipeline(self):
        # import, don't duplicate (spec §3.4): same object source
        self.assertEqual(rm.VIDEO_EXTENSIONS,
                         {".mp4", ".mov", ".mkv", ".webm", ".m4v"})
        self.assertEqual(rm.VIDEO_EXTENSIONS, set(rm._PIPELINE.VIDEO_EXTENSIONS))

    def test_region_json_alone_does_not_form_event(self):
        # region.json is run-input config (kind=config in inventory), never a
        # source that can carry an event by itself
        self.touch("ev/region.json")
        with self.assertRaises(FileNotFoundError) as ctx:
            rm.find_input_events()
        self.assertIn("视频", str(ctx.exception))

    def test_dotfile_video_ignored(self):
        self.touch("ev/.region-preview.png")
        (self.root / "ev" / ".hidden.mp4").write_bytes(b"x")
        with self.assertRaises(FileNotFoundError):
            rm.find_input_events()


# ---------------------------------------------------------------------------
# (e) temporal_overlap — hand-built spans (pure stdlib)
# ---------------------------------------------------------------------------

class TemporalOverlapTest(unittest.TestCase):
    RANGES = [[10.0, 20.0], [30.0, 40.0]]

    def intersect(self, start, end, ranges=None):
        return rm._spans_intersect(start, end, self.RANGES if ranges is None else ranges)

    def test_disjoint_spans_do_not_intersect(self):
        self.assertFalse(self.intersect(0.0, 9.999))
        self.assertFalse(self.intersect(20.001, 29.999))
        self.assertFalse(self.intersect(40.001, 50.0))

    def test_partial_overlaps_intersect(self):
        self.assertTrue(self.intersect(5.0, 10.5))    # left edge of range 1
        self.assertTrue(self.intersect(19.0, 25.0))   # right edge of range 1
        self.assertTrue(self.intersect(35.0, 50.0))   # inside/right of range 2

    def test_containment_both_directions(self):
        self.assertTrue(self.intersect(12.0, 14.0))   # record inside range
        self.assertTrue(self.intersect(5.0, 45.0))    # record spans both ranges

    def test_touching_endpoints_count_closed_interval(self):
        # 确定性求交（§4.5）：closed intervals — touching is intersecting
        self.assertTrue(self.intersect(9.0, 10.0))
        self.assertTrue(self.intersect(20.0, 21.0))
        self.assertTrue(self.intersect(40.0, 41.0))

    def test_second_range_matches(self):
        self.assertTrue(self.intersect(31.0, 32.0))

    def test_empty_and_malformed_ranges_never_raise(self):
        self.assertFalse(self.intersect(0.0, 100.0, ranges=[]))
        self.assertFalse(rm._spans_intersect(0.0, 100.0, None))  # None is NOT the default here
        self.assertFalse(self.intersect(0.0, 100.0, ranges=["x", ["a", "b"], [3]]))
        self.assertTrue(self.intersect(12.0, 13.0, ranges=["x", [10.0, 20.0]]))


class SlideAlignUnitTest(unittest.TestCase):
    """stage_slide_align on hand-built artifacts — no video, no subprocess."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        (self.run_dir / "video").mkdir(parents=True)
        self._ws = mock.patch.object(rm, "WORKSPACE", self.root)
        self._ws.start()
        self.addCleanup(self._ws.stop)

    def write_inputs(self, audio_route: str, image_rows: list[dict],
                     records: list[dict]) -> tuple[Path, Path]:
        rm.atomic_json(self.run_dir / "video" / "ingest_receipt.json",
                       {"schema_version": 1, "status": "complete", "slide_track": True,
                        "audio_route": audio_route, "pages": len(image_rows)})
        evidence_path = self.run_dir / "evidence" / "evidence.jsonl"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        rows = ([{"evidence_id": f"A{i:06d}", "source_id": "F000001", "kind": "audio",
                  "locator": {}, "literal_text": r["clean_literal"],
                  "confidence": {}, "uncertainty": None}
                 for i, r in enumerate(records, 1)] + image_rows)
        with evidence_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        records_path = make_literal_records(self.run_dir, records)
        return evidence_path, records_path

    def image_row(self, n: int, page: str, ranges: list, text: str = "") -> dict:
        return {"evidence_id": f"I{n:06d}", "source_id": f"F{100 + n:06d}", "kind": "image",
                "locator": {"page_id": page, "time_ranges": ranges, "video": "meeting.mp4"},
                "literal_text": text, "confidence": {"route": "apple_vision", "quality": "medium"},
                "uncertainty": None}

    def test_temporal_route_exact_intersection_set(self):
        records = [
            record_row("R000001", 5.0, 9.0, "会前闲聊一"),      # disjoint
            record_row("R000002", 9.0, 10.0, " touching boundary"),  # closed-interval touch
            record_row("R000003", 15.0, 16.0, "页内发言"),       # inside range 1
            record_row("R000004", 21.0, 29.0, "静默页间隙"),     # between ranges
            record_row("R000005", 35.0, 50.0, "第二区间发言"),   # inside range 2
            record_row("R000006", 40.5, 50.0, "区间二之后"),     # disjoint
        ]
        image_rows = [self.image_row(1, "P1", [[10.0, 20.0], [30.0, 40.0]], "今天讨论比赛安排")]
        evidence_path, records_path = self.write_inputs("extracted", image_rows, records)
        out = rm.stage_slide_align(self.run_dir, evidence_path, records_path)
        relations = rm.load_jsonl(out)
        self.assertEqual(len(relations), 1)
        row = relations[0]
        self.assertEqual(set(row), {"relation_id", "slide_source_id", "slide_evidence_ids",
                                    "candidate_audio_records", "relation", "decision_route"})
        self.assertEqual(row["relation_id"], "R000001")
        self.assertEqual(row["slide_source_id"], "F000101")
        self.assertEqual(row["slide_evidence_ids"], ["I000001"])
        self.assertEqual(row["relation"], "unknown")          # v1: no LLM classifier
        self.assertEqual(row["decision_route"], "temporal_overlap")
        self.assertEqual([c["record_id"] for c in row["candidate_audio_records"]],
                         ["R000002", "R000003", "R000005"])
        for cand in row["candidate_audio_records"]:
            self.assertIsInstance(cand["lexical_score"], float)
            self.assertGreaterEqual(cand["lexical_score"], 0.0)
            self.assertLessEqual(cand["lexical_score"], 1.0)
        receipt = json.loads((self.run_dir / "relations" / "slide_align_receipt.json")
                             .read_text(encoding="utf-8"))
        self.assertTrue(receipt["coverage_complete"])
        self.assertEqual(receipt["decision_route"], "temporal_overlap")
        self.assertFalse(receipt["relation_decisions_final"])

    def test_lexical_route_for_external_audio(self):
        # 时间轴不同源（独立音频文件）→ lexical 路由（§4.5）
        records = [
            record_row("R000001", 0.0, 5.0, "今天讨论比赛安排时间表"),   # high similarity
            record_row("R000002", 5.0, 10.0, "完全无关的天气闲聊内容"),  # ~zero
            record_row("R000003", 10.0, 15.0, "比赛安排"),               # medium
        ]
        image_rows = [self.image_row(1, "P1", [[0.0, 8.0]], "今天讨论比赛安排")]
        evidence_path, records_path = self.write_inputs("external", image_rows, records)
        out = rm.stage_slide_align(self.run_dir, evidence_path, records_path)
        row = rm.load_jsonl(out)[0]
        self.assertEqual(row["decision_route"], "lexical")
        ids = [c["record_id"] for c in row["candidate_audio_records"]]
        self.assertIn("R000001", ids)
        self.assertNotIn("R000002", ids)  # below minimum-score
        self.assertLessEqual(len(ids), 5)  # top-k
        scores = [c["lexical_score"] for c in row["candidate_audio_records"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(s >= 0.03 for s in scores))
        # exact mirror of align_audio_slides scoring
        expect = round(rm._ALIGN.similarity("今天讨论比赛安排", "今天讨论比赛安排时间表"), 4)
        self.assertEqual(row["candidate_audio_records"][0]["lexical_score"], expect)

    def test_page_without_candidates_still_gets_row_coverage_law(self):
        records = [record_row("R000001", 0.0, 5.0, "只与第一页重叠")]
        image_rows = [
            self.image_row(1, "P1", [[0.0, 5.0]]),
            self.image_row(2, "P2", [[100.0, 120.0]]),   # nobody talks over it
            self.image_row(3, "P3", [[200.0, 210.0]]),
        ]
        evidence_path, records_path = self.write_inputs("extracted", image_rows, records)
        out = rm.stage_slide_align(self.run_dir, evidence_path, records_path)
        relations = rm.load_jsonl(out)
        self.assertEqual(len(relations), 3)  # EVERY image id gets a row
        union = {eid for r in relations for eid in r["slide_evidence_ids"]}
        self.assertEqual(union, {"I000001", "I000002", "I000003"})
        by_page = {r["slide_evidence_ids"][0]: r for r in relations}
        self.assertEqual([c["record_id"] for c in by_page["I000001"]["candidate_audio_records"]],
                         ["R000001"])
        self.assertEqual(by_page["I000002"]["candidate_audio_records"], [])
        self.assertEqual(by_page["I000003"]["candidate_audio_records"], [])
        self.assertEqual([r["relation_id"] for r in relations],
                         ["R000001", "R000002", "R000003"])

    def test_no_slide_track_returns_none_and_writes_nothing(self):
        # pure-audio event: placeholder behavior in stage_build_package stays
        records = [record_row("R000001", 0.0, 5.0, "纯音频")]
        evidence_path = self.run_dir / "evidence" / "evidence.jsonl"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(
            {"evidence_id": "A000001", "kind": "audio"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        records_path = make_literal_records(self.run_dir, records)
        self.assertIsNone(rm.stage_slide_align(self.run_dir, evidence_path, records_path))
        self.assertFalse((self.run_dir / "relations" / "final.jsonl").exists())
        # slide_track=false receipt (degraded) also skips
        rm.atomic_json(self.run_dir / "video" / "ingest_receipt.json",
                       {"slide_track": False, "audio_route": "extracted"})
        self.assertIsNone(rm.stage_slide_align(self.run_dir, evidence_path, records_path))
        self.assertFalse((self.run_dir / "relations" / "final.jsonl").exists())

    def test_malformed_record_timing_is_skipped_not_fatal(self):
        records = [
            record_row("R000001", 0.0, 5.0, "正常记录"),
            dict(record_row("R000002", 0.0, 5.0, "坏时间戳"), start_seconds="NaN-ish"),
        ]
        image_rows = [self.image_row(1, "P1", [[0.0, 6.0]])]
        evidence_path, records_path = self.write_inputs("extracted", image_rows, records)
        out = rm.stage_slide_align(self.run_dir, evidence_path, records_path)
        row = rm.load_jsonl(out)[0]
        self.assertEqual([c["record_id"] for c in row["candidate_audio_records"]], ["R000001"])


# ---------------------------------------------------------------------------
# (c) pure-audio byte-identity against pre-change goldens
# ---------------------------------------------------------------------------

class PureAudioByteIdentityTest(unittest.TestCase):
    """兼容边界（计划）：纯音频/notes 事件行为与产物逐字节不变。

    Goldens embedded above were captured from the 12-station code BEFORE the
    T6 edit; every assertion here compares bytes, not parsed structure."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        self.run_dir.mkdir(parents=True)
        self._ws = mock.patch.object(rm, "WORKSPACE", self.root)
        self._ws.start()
        self.addCleanup(self._ws.stop)
        self.records_path = make_literal_records(
            self.run_dir, GOLDEN_RECORDS,
            engine=GOLDEN_LITERAL_RECEIPT["engine"])
        rm.atomic_json(self.run_dir / "literal_receipt.json", GOLDEN_LITERAL_RECEIPT)

    def test_evidence_zero_image_rows_and_byte_identical(self):
        out = rm.stage_evidence(self.run_dir, self.records_path)
        self.assertEqual(out.read_bytes(), GOLDEN_EVIDENCE_JSONL.encode("utf-8"))
        rows = rm.load_jsonl(out)
        self.assertEqual([r for r in rows if r.get("kind") == "image"], [])  # ZERO image rows
        receipt = (self.run_dir / "evidence" / "evidence_receipt.json").read_bytes()
        self.assertEqual(receipt, GOLDEN_EVIDENCE_RECEIPT.encode("utf-8"))
        # audio-only sidecar is a video-event artifact — never created here
        self.assertFalse((self.run_dir / "evidence" / "evidence_audio_only.jsonl").exists())

    def test_stale_sidecar_swept_on_pure_audio_rerun(self):
        # FIX-5 retry hygiene: an earlier run established the slide track and
        # left the audio-only sidecar; this rerun produces no image rows, so
        # stage_evidence must sweep it (downstream reads it "when present" and
        # build_package_v3 would later SystemExit on the stale coverage).
        sidecar = self.run_dir / "evidence" / "evidence_audio_only.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text('{"evidence_id": "A999999", "kind": "audio"}\n', encoding="utf-8")
        out = rm.stage_evidence(self.run_dir, self.records_path)
        self.assertFalse(sidecar.exists())
        # outputs stay byte-identical to the goldens despite the planted file
        self.assertEqual(out.read_bytes(), GOLDEN_EVIDENCE_JSONL.encode("utf-8"))
        receipt = (self.run_dir / "evidence" / "evidence_receipt.json").read_bytes()
        self.assertEqual(receipt, GOLDEN_EVIDENCE_RECEIPT.encode("utf-8"))

    # -- planted-stale retry hygiene (quality review I-1 / I-2) ---------------

    def golden_package_inputs(self) -> tuple[Path, Path]:
        """Golden reconciled + annotated fixtures (capture-identical shape)."""
        rec_json = self.run_dir / "reconciled" / "reconciled.json"
        rec_json.parent.mkdir(parents=True, exist_ok=True)
        doc = json.loads(json.dumps(GOLDEN_RECONCILED))
        doc["dispositions"].append({"evidence_id": "A000003", "status": "excluded_logistics",
                                    "note": "excluded from topic reconcile by relevance filter"})
        rec_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        ann_path = self.run_dir / "relevance" / "literal_records_annotated.jsonl"
        ann_path.parent.mkdir(parents=True, exist_ok=True)
        with ann_path.open("w", encoding="utf-8") as f:
            for r in GOLDEN_RECORDS:
                f.write(json.dumps(dict(r, relevance={"label": "content"}),
                                   ensure_ascii=False) + "\n")
        return rec_json, ann_path

    def plant_stale_relations(self) -> None:
        """Run-1 established the slide track and wrote REAL relations."""
        rel_dir = self.run_dir / "relations"
        rel_dir.mkdir(parents=True, exist_ok=True)
        (rel_dir / "final.jsonl").write_text(json.dumps({
            "relation_id": "R000001", "slide_source_id": "F000009",
            "slide_evidence_ids": ["I000001"], "candidate_audio_records": [],
            "relation": "unknown", "decision_route": "temporal_overlap"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        rm.atomic_json(rel_dir / "slide_align_receipt.json", {"relation_count": 1})

    def plant_run1_slide_artifacts(self) -> None:
        """Full run-1 slide-layer state: ingest receipt (slide_track=true),
        slides/, extracted audio, sidecar, REAL relations — all stale for a
        video-removed rerun."""
        video_dir = self.run_dir / "video"
        slides_dir = self.run_dir / "slides"
        video_dir.mkdir(parents=True, exist_ok=True)
        slides_dir.mkdir(parents=True, exist_ok=True)
        rm.atomic_json(video_dir / "ingest_receipt.json", {
            "schema_version": 1, "status": "complete", "video": "meeting.mp4",
            "video_source_id": "F000009", "region_source": "user",
            "region_confirmed_by_user": True, "audio_route": "extracted",
            "audio_extracted": str(self.run_dir / "extracted_audio.m4a"),
            "slide_track": True, "pages": 1, "ocr": "complete",
            "derived_source_ids": ["F000010", "F000011"], "warnings": []})
        rm.atomic_json(video_dir / "region.json",
                       {"schema_version": 1, "video": "meeting.mp4", "source": "user"})
        rm.atomic_json(slides_dir / "slides.json", {
            "schema_version": 1, "video": "meeting.mp4", "region_source": "user",
            "slides": [{"page_id": "P1", "image": "P1.png", "time_ranges": [[0.0, 5.0]]}]})
        (slides_dir / "P1.png").write_bytes(b"\x89PNG stale page")
        (slides_dir / "ocr.jsonl").write_text("", encoding="utf-8")
        (self.run_dir / "extracted_audio.m4a").write_bytes(b"stale audio")
        sidecar = self.run_dir / "evidence" / "evidence_audio_only.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text('{"evidence_id": "A999999", "kind": "audio"}\n', encoding="utf-8")
        self.plant_stale_relations()

    def test_stale_relations_swept_and_package_byte_identical(self):
        # I-1: degraded/pure-audio rerun (no ingest receipt — video removed and
        # already swept by stage_video_ingest, or degraded receipt replaced it).
        # Run-1's REAL relations/final.jsonl would ride --relations into
        # build_package_v3 and SystemExit on the coverage mismatch — every
        # retry would fail. stage_slide_align must sweep them (same standard
        # as the sidecar sweep) and the classic empty placeholder must take
        # over, package byte-identical.
        evidence_path = rm.stage_evidence(self.run_dir, self.records_path)
        self.plant_stale_relations()
        rel_dir = self.run_dir / "relations"
        self.assertIsNone(
            rm.stage_slide_align(self.run_dir, evidence_path, self.records_path))
        self.assertFalse((rel_dir / "final.jsonl").exists())
        self.assertFalse((rel_dir / "slide_align_receipt.json").exists())
        rec_json, ann_path = self.golden_package_inputs()
        pkg = self.root / "outputs" / "ev"
        rm.stage_build_package(self.run_dir, evidence_path, ann_path, rec_json,
                               self.run_dir / "notes" / "note_relations.jsonl", pkg)
        self.assertEqual((pkg / "00_使用说明.md").read_bytes(), GOLDEN_GUIDE.encode("utf-8"))
        self.assertEqual(sorted(p.name for p in pkg.iterdir()), GOLDEN_PACKAGE_FILES)
        self.assertEqual((rel_dir / "final.jsonl").read_bytes(), b"")  # placeholder restored

    def test_stale_ingest_artifacts_swept_on_video_removed_rerun(self):
        # I-2: with the event's video removed between retries, run-1's ingest
        # receipt (slide_track=true) + slides/ would drive _slide_image_evidence
        # into the phantom 「幻灯片页 P1 缺少 manifest 派生条目（video_ingest 接线
        # 错误）」 fatal on EVERY retry. The no-video skip path must sweep the
        # station's artifacts so the rerun behaves exactly like a fresh
        # pure-audio run — evidence/merge/guide byte-identical to the goldens.
        self.plant_run1_slide_artifacts()
        manifest = self.run_dir / "manifest.json"  # fresh inventory: no derived entries
        rm.atomic_json(manifest, {"schema_version": 1, "source_root": "x", "files": [],
                                  "counts": {}, "file_count": 0, "total_bytes": 0,
                                  "audio_duration_seconds": 0})
        event = {"name": "ev", "dir": self.root / "input" / "ev", "audio": [],
                 "video": [], "notes": None}
        self.assertIsNone(rm.stage_video_ingest(self.run_dir, manifest, event))
        self.assertFalse((self.run_dir / "video").exists())
        self.assertFalse((self.run_dir / "slides").exists())
        self.assertFalse((self.run_dir / "extracted_audio.m4a").exists())
        # no misleading raise: evidence regenerates pure-audio, byte-identical,
        # and the stale sidecar is swept (FIX-5 chain)
        evidence_path = rm.stage_evidence(self.run_dir, self.records_path)
        self.assertEqual(evidence_path.read_bytes(), GOLDEN_EVIDENCE_JSONL.encode("utf-8"))
        self.assertFalse((self.run_dir / "evidence" / "evidence_audio_only.jsonl").exists())
        # I-1 chain: stale relations swept downstream
        self.assertIsNone(
            rm.stage_slide_align(self.run_dir, evidence_path, self.records_path))
        self.assertFalse((self.run_dir / "relations" / "final.jsonl").exists())
        # merge + guide byte-identical to the pure-audio goldens
        rec_json, ann_path = self.golden_package_inputs()
        rm.atomic_json(self.run_dir / "relevance" / "receipt.json",
                       {"excluded_ids": ["R000003"], "records_total": 3})
        merged = rm.merge_excluded_dispositions(self.run_dir, rec_json, ann_path,
                                                evidence_path)
        self.assertEqual(merged.read_bytes(), GOLDEN_MERGED.encode("utf-8"))
        pkg = self.root / "outputs" / "ev"
        rm.stage_build_package(self.run_dir, evidence_path, ann_path, merged,
                               self.run_dir / "notes" / "note_relations.jsonl", pkg)
        self.assertEqual((pkg / "00_使用说明.md").read_bytes(), GOLDEN_GUIDE.encode("utf-8"))
        self.assertEqual(sorted(p.name for p in pkg.iterdir()), GOLDEN_PACKAGE_FILES)

    def test_merge_excluded_dispositions_identical_paths(self):
        evidence_path = rm.stage_evidence(self.run_dir, self.records_path)
        rec_dir = self.run_dir / "reconciled"
        rec_dir.mkdir()
        rec_json = rec_dir / "reconciled.json"
        rec_json.write_text(json.dumps(GOLDEN_RECONCILED, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        annotated = [dict(r, relevance={"label": "content"}) for r in GOLDEN_RECORDS]
        ann_path = self.run_dir / "relevance" / "literal_records_annotated.jsonl"
        ann_path.parent.mkdir(parents=True)
        with ann_path.open("w", encoding="utf-8") as f:
            for r in annotated:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        # (1) no relevance receipt -> identity
        self.assertEqual(rm.merge_excluded_dispositions(self.run_dir, rec_json, ann_path,
                                                        evidence_path), rec_json)
        # (2) empty exclusions -> identity (classic early return preserved)
        rm.atomic_json(self.run_dir / "relevance" / "receipt.json",
                       {"excluded_ids": [], "records_total": 3})
        self.assertEqual(rm.merge_excluded_dispositions(self.run_dir, rec_json, ann_path,
                                                        evidence_path), rec_json)
        # (3) with exclusion -> byte-identical merged golden
        rm.atomic_json(self.run_dir / "relevance" / "receipt.json",
                       {"excluded_ids": ["R000003"], "records_total": 3})
        merged = rm.merge_excluded_dispositions(self.run_dir, rec_json, ann_path, evidence_path)
        self.assertEqual(merged, rec_dir / "reconciled_merged.json")
        self.assertEqual(merged.read_bytes(), GOLDEN_MERGED.encode("utf-8"))

    def test_build_package_byte_identical_no_new_flags(self):
        evidence_path = rm.stage_evidence(self.run_dir, self.records_path)
        rec_json = self.run_dir / "reconciled" / "reconciled.json"
        rec_json.parent.mkdir(exist_ok=True)
        doc = json.loads(json.dumps(GOLDEN_RECONCILED))  # deep copy
        doc["dispositions"].append({"evidence_id": "A000003", "status": "excluded_logistics",
                                    "note": "excluded from topic reconcile by relevance filter"})
        rec_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        annotated = [dict(r, relevance={"label": "content"}) for r in GOLDEN_RECORDS]
        ann_path = self.run_dir / "relevance" / "literal_records_annotated.jsonl"
        ann_path.parent.mkdir(parents=True)
        with ann_path.open("w", encoding="utf-8") as f:
            for r in annotated:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        pkg = self.root / "outputs" / "ev"
        rm.stage_build_package(self.run_dir, evidence_path, ann_path, rec_json,
                               self.run_dir / "notes" / "note_relations.jsonl", pkg)
        # guide byte-identical (no nav line for pure audio)
        self.assertEqual((pkg / "00_使用说明.md").read_bytes(), GOLDEN_GUIDE.encode("utf-8"))
        # artifact set byte-identical: no 幻灯片/, no new files
        self.assertEqual(sorted(p.name for p in pkg.iterdir()), GOLDEN_PACKAGE_FILES)
        self.assertFalse((pkg / "幻灯片").exists())
        completion = json.loads((pkg / "completion_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(completion["artifacts"], GOLDEN_ARTIFACTS)
        self.assertNotIn("ocr_correction_unmatched", completion)
        # empty relations placeholder preserved (0-byte JSONL contract)
        self.assertEqual((self.run_dir / "relations" / "final.jsonl").read_bytes(), b"")

    def test_video_ingest_skips_with_zero_artifacts(self):
        manifest = self.run_dir / "manifest.json"
        rm.atomic_json(manifest, {"schema_version": 1, "source_root": "x", "files": [],
                                  "counts": {}, "file_count": 0, "total_bytes": 0,
                                  "audio_duration_seconds": 0})
        event = {"name": "ev", "dir": self.root, "audio": [], "video": [], "notes": None}
        self.assertIsNone(rm.stage_video_ingest(self.run_dir, manifest, event))
        self.assertFalse((self.run_dir / "video").exists())
        self.assertFalse((self.run_dir / "slides").exists())
        self.assertFalse((self.run_dir / "extracted_audio.m4a").exists())

    def test_relevance_evidence_input_selection(self):
        # sidecar absent -> classic evidence.jsonl is fed (command byte-stable);
        # sidecar present -> audio-only view wins (v1 §3.4 decision)
        evidence = self.run_dir / "evidence" / "evidence.jsonl"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("", encoding="utf-8")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop-after-capture")

        with mock.patch.object(rm, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                rm.stage_relevance(self.run_dir, self.records_path, evidence)
        self.assertEqual(captured["cmd"][captured["cmd"].index("--evidence") + 1], str(evidence))

        sidecar = self.run_dir / "evidence" / "evidence_audio_only.jsonl"
        sidecar.write_text("", encoding="utf-8")
        with mock.patch.object(rm, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                rm.stage_relevance(self.run_dir, self.records_path, evidence)
        self.assertEqual(captured["cmd"][captured["cmd"].index("--evidence") + 1], str(sidecar))

    def test_quality_gate_evidence_arg_only_with_sidecar(self):
        pkg = self.root / "outputs" / "ev"
        pkg.mkdir(parents=True)
        calls = []

        class FakeCompleted:
            returncode = 0
            stdout = ""

        def fake_sub(cmd, **kwargs):
            calls.append(cmd)
            return FakeCompleted()

        with mock.patch.object(rm.subprocess, "run", side_effect=fake_sub):
            self.assertTrue(rm.stage_validate(pkg, self.run_dir))
        self.assertNotIn("--evidence", calls[-1])  # gate call is last

        sidecar = self.run_dir / "evidence" / "evidence_audio_only.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("", encoding="utf-8")
        calls.clear()
        with mock.patch.object(rm.subprocess, "run", side_effect=fake_sub):
            self.assertTrue(rm.stage_validate(pkg, self.run_dir))
        gate_cmd = calls[-1]
        self.assertEqual(gate_cmd[gate_cmd.index("--evidence") + 1], str(sidecar))


# ---------------------------------------------------------------------------
# manifest derived-entry injection (stdlib: unit level, spec §4.3 + §6 幂等)
# ---------------------------------------------------------------------------

class DerivedManifestInjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        (self.run_dir / "slides").mkdir(parents=True)
        self.audio = self.run_dir / "extracted_audio.m4a"
        self.audio.write_bytes(b"fake-audio-bytes")
        (self.run_dir / "slides" / "P1.png").write_bytes(b"fake-png-1")
        (self.run_dir / "slides" / "P2.png").write_bytes(b"fake-png-2")
        self.manifest_path = self.run_dir / "manifest.json"
        rm.atomic_json(self.manifest_path, {
            "schema_version": 1, "created_at": "x", "source_root": str(self.run_dir / "source"),
            "file_count": 2, "total_bytes": 20, "counts": {"video": 1, "note": 1},
            "audio_duration_seconds": 0,
            "files": [
                {"source_id": "F000001", "relative_path": "meeting.mp4", "kind": "video",
                 "extension": ".mp4", "size_bytes": 10, "modified_at": "x", "sha256": "a",
                 "eligible_source": True, "media": {"duration_seconds": 8.0}},
                {"source_id": "F000002", "relative_path": "notes.md", "kind": "note",
                 "extension": ".md", "size_bytes": 10, "modified_at": "x", "sha256": "b",
                 "eligible_source": True},
            ]})
        self.slides = [
            {"page_id": "P1", "image": "P1.png", "time_ranges": [[0.0, 3.5]]},
            {"page_id": "P2", "image": "P2.png", "time_ranges": [[3.5, 8.0], [9.0, 10.0]]},
        ]

    def test_derived_entries_shape_and_absolute_paths(self):
        derived = rm._inject_derived_entries(self.manifest_path, self.run_dir, "meeting.mp4",
                                             self.audio, 8.0, self.slides)
        self.assertEqual([d["source_id"] for d in derived], ["F000003", "F000004", "F000005"])
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        self.assertEqual(len(files), 5)
        audio_entry = files[2]
        self.assertEqual(audio_entry["kind"], "audio")
        self.assertEqual(audio_entry["relative_path"], "extracted_audio.m4a")
        self.assertEqual(audio_entry["derived_from"], "meeting.mp4")
        self.assertTrue(Path(audio_entry["path"]).is_absolute())   # §4.3 contract
        self.assertTrue(Path(audio_entry["path"]).is_file())
        self.assertEqual(Path(audio_entry["path"]).resolve(), self.audio.resolve())
        self.assertTrue(audio_entry["eligible_source"])
        self.assertEqual(audio_entry["media"], {"duration_seconds": 8.0})
        self.assertEqual(audio_entry["sha256"], rm._PIPELINE.sha256_file(self.audio))
        img1 = files[3]
        self.assertEqual(img1["kind"], "image")
        self.assertEqual(img1["page_id"], "P1")
        self.assertEqual(img1["time_ranges"], [[0.0, 3.5]])
        self.assertEqual(img1["relative_path"], "slides/P1.png")
        self.assertTrue(Path(img1["path"]).is_absolute())
        self.assertTrue(Path(img1["path"]).is_file())
        self.assertEqual(files[4]["page_id"], "P2")
        self.assertEqual(files[4]["time_ranges"], [[3.5, 8.0], [9.0, 10.0]])
        # aggregates recomputed
        self.assertEqual(manifest["counts"], {"video": 1, "note": 1, "audio": 1, "image": 2})
        self.assertEqual(manifest["file_count"], 5)
        self.assertEqual(manifest["audio_duration_seconds"], 8.0)

    def test_injection_is_idempotent(self):
        # spec §6: manifest 派生条目追加幂等（retry 复用 runs/<event>）
        rm._inject_derived_entries(self.manifest_path, self.run_dir, "meeting.mp4",
                                   self.audio, 8.0, self.slides)
        first = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        rm._inject_derived_entries(self.manifest_path, self.run_dir, "meeting.mp4",
                                   self.audio, 8.0, self.slides)
        second = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        derived_first = [f for f in first["files"] if "derived_from" in f]
        derived_second = [f for f in second["files"] if "derived_from" in f]
        self.assertEqual(len(derived_first), 3)
        self.assertEqual([f["source_id"] for f in derived_second],
                         [f["source_id"] for f in derived_first])
        self.assertEqual(second["counts"], first["counts"])
        self.assertEqual(second["file_count"], first["file_count"])
        self.assertEqual(second["audio_duration_seconds"], first["audio_duration_seconds"])

    def test_audio_only_degraded_injection(self):
        derived = rm._inject_derived_entries(self.manifest_path, self.run_dir, "meeting.mp4",
                                             self.audio, 6.0, [])
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["kind"], "audio")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn("image", manifest["counts"])


# ---------------------------------------------------------------------------
# OCR corrections helper (venv: real suggest_ocr_corrections subprocess)
# ---------------------------------------------------------------------------

@requires_venv_only
class OcrCorrectionsUnitTest(unittest.TestCase):
    """_suggest_ocr_corrections: deterministic rows via T5's honest gate,
    degrade-never-kill semantics (spec §5), 0-corrections normal pass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        (self.run_dir / "video").mkdir(parents=True)
        (self.run_dir / "slides").mkdir(parents=True)
        (self.run_dir / "evidence").mkdir(parents=True)
        evidence_rows = [
            {"evidence_id": "A000001", "source_id": "F000001", "kind": "audio",
             "locator": {}, "literal_text": "x", "confidence": {}, "uncertainty": None},
            {"evidence_id": "I000001", "source_id": "F000002", "kind": "image",
             "locator": {"page_id": "P1", "time_ranges": [[0, 5]], "video": "m.mp4"},
             "literal_text": "GPT5", "confidence": {}, "uncertainty": None},
        ]
        with (self.run_dir / "evidence" / "evidence.jsonl").open("w", encoding="utf-8") as f:
            for row in evidence_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_records(self, disagreed: bool) -> None:
        rec = record_row("R000001", 0.0, 20.0, "我们先用GPT4跑一遍基线。",
                         disagreed=disagreed)
        make_literal_records(self.run_dir, [rec])

    def write_ocr(self, text: str = "GPT5") -> None:
        (self.run_dir / "slides" / "ocr.jsonl").write_text(
            json.dumps({"file": "P1.png", "items": [{"text": text}], "error": None},
                       ensure_ascii=False) + "\n", encoding="utf-8")

    def test_correction_row_carries_evidence_id_from_image_rows(self):
        self.write_records(disagreed=True)
        self.write_ocr()
        out = rm._suggest_ocr_corrections(self.run_dir)
        self.assertIsNotNone(out)
        rows = rm.load_jsonl(out)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), CORRECTION_FIELDS)          # §4.6 exact schema
        self.assertEqual(row["record_id"], "R000001")
        self.assertEqual(row["original"], "GPT4")
        self.assertEqual(row["suggested"], "GPT5")
        self.assertEqual(row["ocr_page_id"], "P1")
        self.assertEqual(row["ocr_evidence_id"], "I000001")    # from OUR image rows
        self.assertEqual(row["basis"], "edit_distance")
        self.assertTrue(row["engines_disagreed"])
        # the evidence map artifact is built from image rows (page stem -> I-id)
        map_rows = rm.load_jsonl(self.run_dir / "video" / "ocr_evidence_map.jsonl")
        self.assertEqual(map_rows, [{"page_id": "P1", "evidence_id": "I000001"}])
        receipt = json.loads((self.run_dir / "video" / "ocr_corrections_receipt.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(receipt["corrections_count"], 1)

    def test_no_disagreement_zero_corrections_is_normal_pass(self):
        self.write_records(disagreed=False)
        self.write_ocr()
        out = rm._suggest_ocr_corrections(self.run_dir)
        self.assertIsNotNone(out)
        self.assertEqual(rm.load_jsonl(out), [])  # 0 corrections = pass, file exists

    def test_missing_ocr_returns_none(self):
        self.write_records(disagreed=True)
        self.assertIsNone(rm._suggest_ocr_corrections(self.run_dir))
        self.assertFalse((self.run_dir / "ocr_corrections.jsonl").exists())

    def test_bad_input_degrades_never_raises(self):
        # spec §5: audit 站内的修正建议失败降级跳过，不得杀死本来健康的运行
        self.write_records(disagreed=True)
        (self.run_dir / "slides" / "ocr.jsonl").write_text("{not json\n", encoding="utf-8")
        self.assertIsNone(rm._suggest_ocr_corrections(self.run_dir))
        # T5 fail-fast: zero partial writes
        self.assertFalse((self.run_dir / "ocr_corrections.jsonl").exists())


# ---------------------------------------------------------------------------
# §5 degradation matrix (real tools, small fixtures)
# ---------------------------------------------------------------------------

@requires_video
class VideoIngestDegradedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._ws = mock.patch.object(rm, "WORKSPACE", self.root)
        self._ws.start()
        self.addCleanup(self._ws.stop)

    def receipt_of(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "video" / "ingest_receipt.json").read_text(encoding="utf-8"))

    def test_fail_fast_message_mirrors_t3_constant(self):
        # M-1: run_meeting pre-checks with the SAME fail-fast message T3's
        # extract_video_slides raises — pin the mirror against silent drift.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mst_extract_probe", WS / "core" / "scripts" / "extract_video_slides.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(rm._AUDIO_MISSING_MESSAGE, mod.AUDIO_MISSING_MESSAGE)

    def test_silent_video_no_external_audio_fails_fast_zero_artifacts(self):
        # (f) spec §5 行1：fail-fast，中文提示，零工件，源文件不动
        video = Path(_SESSION["silent"]["path"])
        before = rm._PIPELINE.sha256_file(video)
        run_dir, event = make_run(self.root, "silent-ev", {"silent.mp4": video},
                                  region_meta=_SESSION["silent"])
        manifest = run_dir / "manifest.json"
        with self.assertRaises(RuntimeError) as ctx:
            rm.stage_video_ingest(run_dir, manifest, event)
        self.assertIn("需要含声录屏或另配音频", str(ctx.exception))
        self.assertIn("录屏无声且事件内无独立音频文件", str(ctx.exception))
        # ZERO ingest artifacts (pre-check fires before any tool runs)
        self.assertFalse((run_dir / "slides").exists())
        self.assertFalse((run_dir / "extracted_audio.m4a").exists())
        self.assertFalse((run_dir / "video").exists())
        # source untouched, input preserved (existing failure semantics)
        self.assertEqual(rm._PIPELINE.sha256_file(video), before)
        self.assertTrue((event["dir"] / "silent.mp4").is_file())

    def test_unreliable_region_skips_slides_keeps_audio(self):
        # spec §5 行2：all-motion 录屏 → detect exit 2 → 幻灯片轨降级、音轨照常
        video = Path(_SESSION["motion"]["path"])
        run_dir, event = make_run(self.root, "motion-ev", {"motion.mp4": video})
        receipt = rm.stage_video_ingest(run_dir, run_dir / "manifest.json", event)
        self.assertEqual(receipt["status"], "degraded")
        self.assertFalse(receipt["slide_track"])
        self.assertEqual(receipt["pages"], 0)
        self.assertEqual(receipt["ocr"], "skipped")
        self.assertIn("region_unreliable_slide_track_skipped", receipt["warnings"])
        # No region was ever USED (detect wrote none) → the "auto region not
        # user-confirmed" warning must NOT fire on this branch.
        self.assertNotIn("region_auto_not_user_confirmed", receipt["warnings"])
        self.assertFalse(receipt["region_confirmed_by_user"])
        self.assertEqual(receipt["region_source"], "auto")
        # detect ran and honestly recorded the unreliable result
        detect = json.loads((run_dir / "video" / "detect_receipt.json").read_text(encoding="utf-8"))
        self.assertFalse(detect["reliable"])
        self.assertFalse((run_dir / "video" / "region.json").exists())  # never written
        # audio track continues: extracted + injected into manifest
        self.assertEqual(receipt["audio_route"], "extracted")
        audio = run_dir / "extracted_audio.m4a"
        self.assertTrue(audio.is_file())
        self.assertGreater(audio.stat().st_size, 0)
        self.assertFalse((run_dir / "slides").exists())
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        derived = [f for f in manifest["files"] if "derived_from" in f]
        self.assertEqual([f["kind"] for f in derived], ["audio"])
        self.assertTrue(Path(derived[0]["path"]).is_absolute())
        self.assertTrue(Path(derived[0]["path"]).is_file())

    def test_external_audio_wins_video_audio_ignored(self):
        # spec §2 决策表行2：独立音频优先，视频音轨忽略（不产 extracted_audio）
        video = Path(_SESSION["voiced"]["path"])
        external = self.root / "external.m4a"
        external.write_bytes(b"fake-external-audio")
        run_dir, event = make_run(self.root, "dual-ev", {"meeting.mp4": video},
                                  audio={"external.m4a": external},
                                  region_meta=_SESSION["voiced"])
        receipt = rm.stage_video_ingest(run_dir, run_dir / "manifest.json", event)
        self.assertEqual(receipt["audio_route"], "external")
        self.assertIsNone(receipt["audio_extracted"])
        self.assertFalse((run_dir / "extracted_audio.m4a").exists())
        # slide track still stands; external audio timelines are NOT同源 →
        # slide_align must take the lexical route (checked via receipt route input)
        self.assertTrue(receipt["slide_track"])
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        derived = [f for f in manifest["files"] if "derived_from" in f]
        self.assertEqual({f["kind"] for f in derived}, {"image"})  # no audio entry
        # slide_align derives the lexical route from audio_route == "external"
        self.assertEqual(receipt["audio_route"], "external")

    def test_multiple_videos_first_only_with_warning(self):
        # spec §5 行4：按文件名排序取第一个 + warning
        voiced = Path(_SESSION["voiced"]["path"])
        silent = Path(_SESSION["silent"]["path"])
        run_dir, event = make_run(
            self.root, "multi-ev",
            {"aaa_first.mp4": voiced, "zzz_second.mp4": silent},
            region_meta=_SESSION["voiced"], region_video="aaa_first.mp4")
        receipt = rm.stage_video_ingest(run_dir, run_dir / "manifest.json", event)
        self.assertIn("multiple_videos_first_only", receipt["warnings"])
        self.assertEqual(receipt["video"], "aaa_first.mp4")
        self.assertTrue(receipt["slide_track"])  # first video processed normally

    def test_cli_fallback_detect_writes_auto_region(self):
        # spec §3.4 条目1：input region.json 缺失 → CLI 自动检测兜底（source=auto）
        video = Path(_SESSION["voiced"]["path"])
        run_dir, event = make_run(self.root, "fallback-ev", {"meeting.mp4": video})
        receipt = rm.stage_video_ingest(run_dir, run_dir / "manifest.json", event)
        region = run_dir / "video" / "region.json"
        self.assertTrue(region.is_file())
        region_doc = json.loads(region.read_text(encoding="utf-8"))
        self.assertEqual(region_doc["source"], "auto")        # §4.1 shape
        self.assertEqual(region_doc["video"], "meeting.mp4")
        self.assertEqual(region_doc["schema_version"], 1)
        self.assertEqual(receipt["region_source"], "auto")
        self.assertFalse(receipt["region_confirmed_by_user"])
        self.assertIn("region_auto_not_user_confirmed", receipt["warnings"])
        self.assertTrue((run_dir / "video" / "detect_receipt.json").is_file())
        self.assertTrue((run_dir / "video" / "region-preview.png").is_file())
        self.assertTrue(receipt["slide_track"])
        # slides.json echoes the auto region source (report-facing metadata)
        slides_doc = json.loads((run_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
        self.assertEqual(slides_doc["region_source"], "auto")

    def test_stale_region_video_binding_triggers_redetection(self):
        # spec §5 末行：region.json 与视频文件名不匹配 → 重新自动检测 + warning
        video = Path(_SESSION["voiced"]["path"])
        run_dir, event = make_run(self.root, "stale-ev", {"renamed.mp4": video},
                                  region_meta=_SESSION["voiced"],
                                  region_video="some_other_video.mov")
        receipt = rm.stage_video_ingest(run_dir, run_dir / "manifest.json", event)
        self.assertIn("region_config_invalid_redetected", receipt["warnings"])
        self.assertIn("region_auto_not_user_confirmed", receipt["warnings"])
        self.assertFalse(receipt["region_confirmed_by_user"])
        self.assertTrue((run_dir / "video" / "region.json").is_file())  # auto one written
        self.assertTrue(receipt["slide_track"])


# ---------------------------------------------------------------------------
# (d) video event end-to-end at stage level — one shared fixture run
# ---------------------------------------------------------------------------

@requires_video
class VideoEventStageTest(unittest.TestCase):
    """Full stage chain on the voiced fixture: inventory → video_ingest →
    evidence → slide_align → merge → corrections → build_package."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.root = Path(cls._tmp.name)
        cls._ws = mock.patch.object(rm, "WORKSPACE", cls.root)
        cls._ws.start()
        cls.addClassCleanup(cls._ws.stop)

        video = Path(_SESSION["voiced"]["path"])
        cls.run_dir, cls.event = make_run(cls.root, "demo", {"meeting.mp4": video},
                                          region_meta=_SESSION["voiced"])
        cls.manifest_path = cls.run_dir / "manifest.json"
        cls.ingest = rm.stage_video_ingest(cls.run_dir, cls.manifest_path, cls.event)
        assert cls.ingest is not None and cls.ingest["slide_track"], cls.ingest

        cls.slides = json.loads((cls.run_dir / "slides" / "slides.json")
                                .read_text(encoding="utf-8"))["slides"]
        # literal records built from the REAL slide timeline: two records over
        # the first page's first range, nothing elsewhere (empty-candidate rows)
        s0, e0 = cls.slides[0]["time_ranges"][0]
        mid = round((s0 + e0) / 2, 3)
        derived_audio_id = next(
            (f["source_id"] for f in json.loads(cls.manifest_path.read_text(encoding="utf-8"))["files"]
             if f.get("kind") == "audio" and "derived_from" in f), "F000002")
        cls.records = [
            record_row("R000001", round(s0, 3), mid,
                       "Topic 1: Roadmap opens the meeting.",
                       source_id=f"{derived_audio_id}_0000000000_0000004000"),
            record_row("R000002", mid, round(e0, 3),
                       "预算与时间表随后逐项确认。",
                       source_id=f"{derived_audio_id}_0000004000_0000008000"),
        ]
        cls.records_path = make_literal_records(cls.run_dir, cls.records,
                                                engine="test-fixture (no ASR)")
        cls.evidence_path = rm.stage_evidence(cls.run_dir, cls.records_path)
        cls.annotated_path = cls.run_dir / "relevance" / "literal_records_annotated.jsonl"
        cls.annotated_path.parent.mkdir(parents=True, exist_ok=True)
        with cls.annotated_path.open("w", encoding="utf-8") as f:
            for row in cls.records:
                f.write(json.dumps(dict(row, relevance={"label": "content"}),
                                   ensure_ascii=False) + "\n")
        # relevance receipt with zero exclusions — merge must STILL add image
        # dispositions (build_package_v3 coverage law)
        rm.atomic_json(cls.run_dir / "relevance" / "receipt.json",
                       {"schema_version": 1, "records_total": 2, "labels": {"content": 2},
                        "lexical_candidates": 0, "excluded_from_reconcile": 0,
                        "excluded_ids": [], "llm": {"calls": 0}, "invariants": {}})
        cls.relations_path = rm.stage_slide_align(cls.run_dir, cls.evidence_path,
                                                  cls.annotated_path)
        assert cls.relations_path is not None

        reconciled = {
            "units": [{"unit_id": "U000001", "topic_path": ["开场"],
                       "claim": "会议从路线图讲起。", "certainty": "high",
                       "evidence_ids": ["A000001", "A000002"]}],
            "dispositions": [{"evidence_id": "A000001", "status": "accepted", "reason": None},
                             {"evidence_id": "A000002", "status": "uncertain", "reason": None}],
        }
        cls.reconciled_path = cls.run_dir / "reconciled" / "reconciled.json"
        cls.reconciled_path.parent.mkdir(parents=True, exist_ok=True)
        cls.reconciled_path.write_text(
            json.dumps(reconciled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        cls.merged_path = rm.merge_excluded_dispositions(
            cls.run_dir, cls.reconciled_path, cls.annotated_path, cls.evidence_path)
        cls.corrections = rm._suggest_ocr_corrections(cls.run_dir)
        cls.pkg = cls.root / "outputs" / "demo"
        rm.stage_build_package(cls.run_dir, cls.evidence_path, cls.annotated_path,
                               cls.merged_path, cls.run_dir / "notes/note_relations.jsonl",
                               cls.pkg)

    # -- ingest / manifest ---------------------------------------------------

    def test_ingest_receipt_contract(self):
        receipt = self.ingest
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["video"], "meeting.mp4")
        self.assertTrue(receipt["slide_track"])
        self.assertEqual(receipt["audio_route"], "extracted")
        self.assertTrue(receipt["region_confirmed_by_user"])
        self.assertEqual(receipt["region_source"], "user")
        self.assertEqual(receipt["pages"], len(self.slides))
        self.assertGreaterEqual(receipt["pages"], 2)
        for token in receipt["warnings"]:
            self.assertNotEqual(token, "segmentation_anomaly_suspected_embedded_video")
        audio = Path(receipt["audio_extracted"])
        self.assertEqual(audio.resolve(),
                         (self.run_dir / "extracted_audio.m4a").resolve())
        self.assertTrue(audio.is_file())

    def test_manifest_derived_entries_absolute_and_complete(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        derived = [f for f in manifest["files"] if "derived_from" in f]
        audio_entries = [f for f in derived if f["kind"] == "audio"]
        image_entries = [f for f in derived if f["kind"] == "image"]
        self.assertEqual(len(audio_entries), 1)
        self.assertEqual(len(image_entries), len(self.slides))
        for entry in derived:
            self.assertEqual(entry["derived_from"], "meeting.mp4")
            self.assertTrue(entry["eligible_source"])
            self.assertTrue(Path(entry["path"]).is_absolute())   # §4.3 hard contract
            self.assertTrue(Path(entry["path"]).is_file())
            self.assertEqual(entry["sha256"],
                             rm._PIPELINE.sha256_file(Path(entry["path"])))
        self.assertEqual(audio_entries[0]["relative_path"], "extracted_audio.m4a")
        self.assertAlmostEqual(audio_entries[0]["media"]["duration_seconds"], 8.0, delta=0.5)
        by_page = {f["page_id"]: f for f in image_entries}
        for slide in self.slides:
            entry = by_page[slide["page_id"]]
            self.assertEqual(entry["relative_path"], f"slides/{slide['image']}")
            self.assertEqual(entry["time_ranges"], slide["time_ranges"])
        self.assertEqual(len({f["source_id"] for f in manifest["files"]}),
                         len(manifest["files"]))  # unique ids
        self.assertEqual(manifest["counts"]["image"], len(self.slides))
        self.assertEqual(manifest["file_count"], len(manifest["files"]))

    # -- evidence (I###### rows + audio-only sidecar) -------------------------

    def test_evidence_image_rows_follow_spec_44(self):
        rows = rm.load_jsonl(self.evidence_path)
        audio_rows = [r for r in rows if r["kind"] == "audio"]
        image_rows = [r for r in rows if r["kind"] == "image"]
        self.assertEqual(len(audio_rows), 2)
        self.assertEqual([r["evidence_id"] for r in audio_rows], ["A000001", "A000002"])
        self.assertEqual([r["evidence_id"] for r in image_rows],
                         [f"I{i:06d}" for i in range(1, len(self.slides) + 1)])  # after A rows
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        image_sources = {f["page_id"]: f["source_id"] for f in manifest["files"]
                         if f.get("kind") == "image" and "derived_from" in f}
        for row, slide in zip(image_rows, self.slides):
            self.assertEqual(set(row), {"evidence_id", "source_id", "kind", "locator",
                                        "literal_text", "confidence", "uncertainty"})
            self.assertEqual(row["kind"], "image")
            self.assertEqual(row["source_id"], image_sources[slide["page_id"]])
            self.assertEqual(set(row["locator"]), {"page_id", "time_ranges", "video"})
            self.assertEqual(row["locator"]["page_id"], slide["page_id"])
            self.assertEqual(row["locator"]["time_ranges"], slide["time_ranges"])
            self.assertEqual(row["locator"]["video"], "meeting.mp4")
            self.assertIsInstance(row["literal_text"], str)
            # three legal §4.4 confidence shapes, keyed by the row's own
            # uncertainty marker (ocr_page_failed rows appear when the Vision
            # tool ran but a page errored — e.g. Vision XPC blocked by a file
            # sandbox; ocr_unavailable when no OCR output exists at all)
            uncertainty = row["uncertainty"] or ""
            if not uncertainty:
                self.assertEqual(row["confidence"],
                                 {"route": "apple_vision", "quality": "medium"})
            elif uncertainty.startswith("ocr_page_failed"):
                self.assertEqual(row["confidence"],
                                 {"route": "apple_vision", "quality": "low"})
                self.assertEqual(row["literal_text"], "")
            else:
                self.assertTrue(uncertainty.startswith("ocr_unavailable"), uncertainty)
                self.assertEqual(row["confidence"], {"route": "unavailable", "quality": "low"})
                self.assertEqual(row["literal_text"], "")
        receipt = json.loads((self.run_dir / "evidence" / "evidence_receipt.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(receipt["evidence_count"], len(rows))
        self.assertEqual(receipt["image_evidence_count"], len(image_rows))

    def test_audio_only_sidecar_excludes_image_rows(self):
        # CRITICAL v1 decision (§3.4): the reconcile view stays AUDIO-ONLY
        sidecar = self.run_dir / "evidence" / "evidence_audio_only.jsonl"
        self.assertTrue(sidecar.is_file())
        rows = rm.load_jsonl(sidecar)
        self.assertEqual([r for r in rows if r.get("kind") == "image"], [])
        self.assertEqual([r["evidence_id"] for r in rows], ["A000001", "A000002"])
        # sidecar audio lines are byte-identical to the main file's audio lines
        main_lines = [l for l in self.evidence_path.read_text(encoding="utf-8").splitlines()
                      if '"kind": "audio"' in l]
        self.assertEqual(sidecar.read_text(encoding="utf-8").splitlines(), main_lines)

    # -- relations (coverage law) ---------------------------------------------

    def test_relations_cover_all_image_ids_exactly_once(self):
        relations = rm.load_jsonl(self.relations_path)
        image_ids = {f"I{i:06d}" for i in range(1, len(self.slides) + 1)}
        union = [eid for r in relations for eid in r["slide_evidence_ids"]]
        self.assertEqual(sorted(union), sorted(image_ids))       # union == ALL I-ids
        self.assertEqual(len(union), len(set(union)))            # exactly once
        self.assertEqual(len(relations), len(self.slides))
        for row in relations:
            self.assertEqual(set(row), {"relation_id", "slide_source_id", "slide_evidence_ids",
                                        "candidate_audio_records", "relation", "decision_route"})
            self.assertEqual(row["relation"], "unknown")
            self.assertEqual(row["decision_route"], "temporal_overlap")
        by_eid = {r["slide_evidence_ids"][0]: r for r in relations}
        first = by_eid["I000001"]
        self.assertEqual({c["record_id"] for c in first["candidate_audio_records"]},
                         {"R000001", "R000002"})
        empty_rows = [r for r in relations if not r["candidate_audio_records"]]
        if len(self.slides) >= 2:
            self.assertTrue(empty_rows)  # 无候选页也必须有行（覆盖铁律）

    def test_merge_added_dispositions_for_image_rows(self):
        merged = json.loads(self.merged_path.read_text(encoding="utf-8"))
        self.assertNotEqual(self.merged_path, self.reconciled_path)  # merge ran
        dispositions = {d["evidence_id"]: d for d in merged["dispositions"]}
        evidence_ids = {r["evidence_id"] for r in rm.load_jsonl(self.evidence_path)}
        self.assertEqual(set(dispositions), evidence_ids)  # build_package_v3 coverage law
        for eid, disp in dispositions.items():
            if eid.startswith("I"):
                self.assertEqual(disp["status"], "uncertain")  # committed enum vocabulary
                self.assertIn("slide image evidence", disp["note"])
                # FIX-3: build_package_v3.py:156 renders 'reason' (not 'note')
                # into 05 — the honest explanation must ride BOTH keys.
                self.assertEqual(disp["reason"], disp["note"])

    # -- package ---------------------------------------------------------------

    def test_package_has_slides_layer_and_nav_line(self):
        slides_dir = self.pkg / "幻灯片"
        self.assertTrue(slides_dir.is_dir())
        pages = sorted(p.name for p in slides_dir.glob("P*.png"))
        self.assertEqual(len(pages), len(self.slides))
        self.assertTrue((slides_dir / "slides.json").is_file())
        self.assertFalse((slides_dir / "ocr.jsonl").exists())  # run artifact, not copied
        completion = json.loads((self.pkg / "completion_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(completion["artifacts"], GOLDEN_ARTIFACTS + ["幻灯片/"])
        self.assertIn(completion["status"], {"COMPLETE", "COMPLETE_WITH_UNCERTAINTY"})
        coverage = json.loads((self.pkg / "coverage_receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(all(coverage["invariants"].values()), coverage["invariants"])
        # nav line: present for slide layer, exact spec §3.4 wording
        guide = (self.pkg / "00_使用说明.md").read_text(encoding="utf-8")
        self.assertIn(NAV_LINE, guide)
        lines = guide.splitlines()
        self.assertEqual(lines[3], NAV_LINE)  # right after the 02-record line
        # user-confirmed region (this fixture) → NO 报告标注 lines
        self.assertNotIn(AUTO_REGION_ANNOTATION, guide)
        self.assertNotIn(DEGRADED_REGION_ANNOTATION, guide)
        self.assertEqual(lines[-1], GUIDE_LAST_BULLET)
        # FIX-3: 05 renders the image-row reason (not 未说明)
        uncertain_doc = (self.pkg / "05_不确定与冲突.md").read_text(encoding="utf-8")
        self.assertIn("I000001", uncertain_doc)
        self.assertIn("slide image evidence", uncertain_doc)
        self.assertNotIn("I000001** · uncertain · 未说明", uncertain_doc)
        # 03 renders the real relations layer
        ppt = (self.pkg / "03_PPT补充信息.md").read_text(encoding="utf-8")
        self.assertIn("### I000001", ppt)
        self.assertIn("候选语音对应", ppt)
        self.assertIn("R000001", ppt)
        self.assertIn("temporal_overlap", ppt)

    def test_ocr_outcome_pinned_on_both_paths(self):
        # Real Apple Vision may or may not run in the current environment
        # (swiftc missing, or Vision XPC blocked e.g. under a file sandbox —
        # vision_handler_failed). BOTH outcomes are contractual (§5): either
        # complete OCR artifacts, or the honest degrade — slides/package must
        # stand either way and nothing may crash the run.
        self.assertIn(self.ingest["ocr"], {"complete", "unavailable"})
        if self.ingest["ocr"] == "complete":
            self.assertTrue((self.run_dir / "slides" / "ocr.jsonl").is_file())
            self.assertNotIn("ocr_unavailable", self.ingest["warnings"])
            self.assertIsNotNone(self.corrections)
            rows = rm.load_jsonl(self.corrections)
            for row in rows:  # 0 rows is a normal pass; any row must fit §4.6
                self.assertEqual(set(row), CORRECTION_FIELDS)
                self.assertIn(row["ocr_evidence_id"],
                              {f"I{i:06d}" for i in range(1, len(self.slides) + 1)})
        else:
            self.assertIn("ocr_unavailable", self.ingest["warnings"])
            # The failed tool run may still leave a partial ocr.jsonl (rows
            # carry per-page "error"); _suggest_ocr_corrections then runs on it
            # (normally yielding 0 rows) — or is None when no output exists.
            if self.corrections is not None:
                for row in rm.load_jsonl(self.corrections):
                    self.assertEqual(set(row), CORRECTION_FIELDS)
        self.assertTrue((self.pkg / "幻灯片").is_dir())  # package built either way


# ---------------------------------------------------------------------------
# FIX-1 报告标注: 00_使用说明 conditional annotation lines (stdlib only)
# ---------------------------------------------------------------------------

class GuideAnnotationTest(unittest.TestCase):
    """§3.4 item1 / §5 行2 报告标注 inside outputs/: auto-region and
    degraded-region states are marked in the guide; user-confirmed regions
    and pure-audio events stay un-annotated (golden byte-identity pinned in
    PureAudioByteIdentityTest)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        (self.run_dir / "video").mkdir(parents=True)
        self._ws = mock.patch.object(rm, "WORKSPACE", self.root)
        self._ws.start()
        self.addCleanup(self._ws.stop)

    def build(self, *, slide_track: bool, region_source: str = "auto",
              warnings: list[str] | None = None) -> Path:
        """Minimal REAL chain: evidence → align → merge → package (stdlib)."""
        evidence_dir = self.run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        rows = [{"evidence_id": "A000001", "source_id": "F000001_0000000000_0000005000",
                 "kind": "audio",
                 "locator": {"start_seconds": 0.0, "end_seconds": 5.0,
                             "window_id": "F000001_0000000000_0000005000"},
                 "literal_text": "今天讨论比赛安排。",
                 "confidence": {"route": "test", "quality": "medium", "agreement": 1.0},
                 "uncertainty": None}]
        if slide_track:
            rows.append({"evidence_id": "I000001", "source_id": "F000002", "kind": "image",
                         "locator": {"page_id": "P1", "time_ranges": [[0.0, 5.0]],
                                     "video": "meeting.mp4"},
                         "literal_text": "Topic 1: Roadmap",
                         "confidence": {"route": "apple_vision", "quality": "medium"},
                         "uncertainty": None})
        evidence_path = evidence_dir / "evidence.jsonl"
        with evidence_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        records = [record_row("R000001", 0.0, 5.0, "今天讨论比赛安排。")]
        make_literal_records(self.run_dir, records)
        annotated_path = self.run_dir / "relevance" / "literal_records_annotated.jsonl"
        annotated_path.parent.mkdir(parents=True, exist_ok=True)
        with annotated_path.open("w", encoding="utf-8") as f:
            for row in records:
                f.write(json.dumps(dict(row, relevance={"label": "content"}),
                                   ensure_ascii=False) + "\n")
        rm.atomic_json(self.run_dir / "relevance" / "receipt.json",
                       {"schema_version": 1, "records_total": 1, "labels": {"content": 1},
                        "lexical_candidates": 0, "excluded_from_reconcile": 0,
                        "excluded_ids": [], "llm": {"calls": 0}, "invariants": {}})
        rm.atomic_json(self.run_dir / "video" / "ingest_receipt.json", {
            "schema_version": 1, "status": "complete" if slide_track else "degraded",
            "video": "meeting.mp4", "video_source_id": "F000001",
            "region_source": region_source,
            "region_confirmed_by_user": region_source == "user",
            "audio_route": "extracted",
            "audio_extracted": str(self.run_dir / "extracted_audio.m4a"),
            "slide_track": slide_track, "pages": 1 if slide_track else 0,
            "ocr": "complete" if slide_track else "skipped",
            "derived_source_ids": [], "warnings": warnings or []})
        if slide_track:
            slides_dir = self.run_dir / "slides"
            slides_dir.mkdir(parents=True, exist_ok=True)
            (slides_dir / "P1.png").write_bytes(b"\x89PNG fake page bytes")
            rm.atomic_json(slides_dir / "slides.json", {
                "schema_version": 1, "video": "meeting.mp4", "region_source": region_source,
                "region_rect_px": [64, 40, 512, 320],
                "slides": [{"page_id": "P1", "image": "P1.png",
                            "time_ranges": [[0.0, 5.0]], "representative_time_s": 2.0}]})
            self.assertIsNotNone(
                rm.stage_slide_align(self.run_dir, evidence_path, annotated_path))
        reconciled = {"units": [{"unit_id": "U000001", "topic_path": ["开场"],
                                 "claim": "会议从比赛安排讲起。", "certainty": "high",
                                 "evidence_ids": ["A000001"]}],
                      "dispositions": [{"evidence_id": "A000001", "status": "accepted",
                                        "reason": None}]}
        rec_path = self.run_dir / "reconciled" / "reconciled.json"
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(json.dumps(reconciled, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        merged = rm.merge_excluded_dispositions(self.run_dir, rec_path, annotated_path,
                                                evidence_path)
        pkg = self.root / "outputs" / "ev"
        rm.stage_build_package(self.run_dir, evidence_path, annotated_path, merged,
                               self.run_dir / "notes" / "note_relations.jsonl", pkg)
        return pkg

    def guide(self, pkg: Path) -> list[str]:
        return (pkg / "00_使用说明.md").read_text(encoding="utf-8").splitlines()

    def test_auto_region_annotation_after_nav_line(self):
        # M-4: the annotation keys on the ingest warning TOKEN emitted by the
        # CLI-fallback detect path (stage_video_ingest), not on region_source.
        pkg = self.build(slide_track=True, region_source="auto",
                         warnings=["region_auto_not_user_confirmed"])
        lines = self.guide(pkg)
        self.assertEqual(lines[3], NAV_LINE)                   # nav line position
        self.assertEqual(lines[-1], AUTO_REGION_ANNOTATION)    # annotation after it (end)
        self.assertNotIn(DEGRADED_REGION_ANNOTATION, "\n".join(lines))
        self.assertTrue((pkg / "幻灯片").is_dir())

    def test_auto_region_source_without_warning_token_not_annotated(self):
        # M-4 regression pin: an off-schema hand-made region.json (source
        # "auto"/missing) never triggers the annotation — only a real
        # CLI-fallback detect run does.
        pkg = self.build(slide_track=True, region_source="auto", warnings=[])
        text = "\n".join(self.guide(pkg))
        self.assertIn(NAV_LINE, text)
        self.assertNotIn(AUTO_REGION_ANNOTATION, text)
        self.assertEqual(self.guide(pkg)[-1], GUIDE_LAST_BULLET)

    def test_user_confirmed_region_has_no_annotation(self):
        pkg = self.build(slide_track=True, region_source="user")
        text = "\n".join(self.guide(pkg))
        self.assertIn(NAV_LINE, text)
        self.assertNotIn(AUTO_REGION_ANNOTATION, text)
        self.assertNotIn(DEGRADED_REGION_ANNOTATION, text)
        self.assertEqual(self.guide(pkg)[-1], GUIDE_LAST_BULLET)

    def test_unreliable_region_degraded_annotation(self):
        pkg = self.build(slide_track=False,
                         warnings=["region_unreliable_slide_track_skipped"])
        lines = self.guide(pkg)
        self.assertEqual(lines[-1], DEGRADED_REGION_ANNOTATION)
        text = "\n".join(lines)
        self.assertNotIn(NAV_LINE, text)                       # no slide layer → no nav
        self.assertNotIn(AUTO_REGION_ANNOTATION, text)
        self.assertFalse((pkg / "幻灯片").exists())

    def test_other_degraded_states_add_no_annotation(self):
        # receipt exists, slide track absent, but NOT the unreliable-region
        # warning → guide stays byte-identical to the classic golden
        pkg = self.build(slide_track=False, warnings=["ocr_unavailable"])
        text = (pkg / "00_使用说明.md").read_text(encoding="utf-8")
        self.assertEqual(text, GOLDEN_GUIDE)


# ---------------------------------------------------------------------------
# FIX-4: degrade handlers survive missing tools / hung subprocesses (stdlib)
# ---------------------------------------------------------------------------

class CorrectionsDegradeTest(unittest.TestCase):
    """_suggest_ocr_corrections must degrade — never kill — when the tools
    venv is gone (FileNotFoundError/OSError) or the subprocess hangs
    (TimeoutExpired), matching its docstring promise (§5)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.root / "runs" / "ev"
        for sub in ("video", "slides", "evidence"):
            (self.run_dir / sub).mkdir(parents=True)
        (self.run_dir / "slides" / "ocr.jsonl").write_text(
            json.dumps({"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}) + "\n",
            encoding="utf-8")
        (self.run_dir / "evidence" / "evidence.jsonl").write_text(
            json.dumps({"evidence_id": "I000001", "kind": "image",
                        "locator": {"page_id": "P1"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        make_literal_records(self.run_dir, [record_row("R000001", 0.0, 5.0, "用GPT4跑基线",
                                                       disagreed=True)])

    def test_missing_interpreter_degrades(self):
        with mock.patch.object(rm, "run", side_effect=FileNotFoundError(2, "No such file")):
            self.assertIsNone(rm._suggest_ocr_corrections(self.run_dir))
        self.assertFalse((self.run_dir / "ocr_corrections.jsonl").exists())

    def test_oserror_degrades(self):
        with mock.patch.object(rm, "run", side_effect=OSError("venv deleted")):
            self.assertIsNone(rm._suggest_ocr_corrections(self.run_dir))

    def test_timeout_degrades(self):
        with mock.patch.object(rm, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="suggest", timeout=1)):
            self.assertIsNone(rm._suggest_ocr_corrections(self.run_dir))


if __name__ == "__main__":
    unittest.main()
