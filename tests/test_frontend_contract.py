"""Frontend contract tests — screen-recording spec §6.3 + §3.6 (static text).

§6.3 (test plan item 3) requires「app.js 无硬编码断言」: the console stage
track must derive its station count from stage_total / STAGE_LABELS, never a
literal, and the frontend stage tables must stay an exact mirror of
run_meeting.STAGE_ORDER (14 stations, spec §3.4) — the drift the M3 review
flag caught is pinned here permanently. §3.6's UI surface is pinned where it
is statically auditable: the five video extensions in app.js's drag-drop
filter AND index.html's file-input accept attribute.

No browser and no JS engine: app.js / index.html are parsed as text with
regexes (house rule — plain statically-auditable JS). Single sources of truth
are imported guarded, mirroring tests/test_webapp_server.py's precedent
(fresh clones without config.json still run the text-level pins).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
for _p in (str(WS), str(WS / "webapp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

APP_JS = (WS / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WS / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
RUN_MEETING_PY = (WS / "run_meeting.py").read_text(encoding="utf-8")

import server as webapp  # noqa: E402  (VIDEO_EXTS upload whitelist, §3.5)

try:  # STAGE_ORDER single source of truth (spec §3.4: 14 stations)
    import run_meeting  # noqa: E402
    HAS_RUN_MEETING = True
except (SystemExit, ImportError):  # fresh clone without config.json
    run_meeting = None
    HAS_RUN_MEETING = False

try:  # VIDEO_EXTENSIONS single source of truth (mirror-drift pin, spec §3.7)
    sys.path.insert(0, str(WS / "core" / "scripts"))
    import meeting_pipeline  # noqa: E402
    HAS_MEETING_PIPELINE = True
except (SystemExit, ImportError):
    meeting_pipeline = None
    HAS_MEETING_PIPELINE = False

# Literal pins (§3.5 five-extension whitelist; §3.4 video_ingest substages)
VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "m4v"}
VIDEO_SUBSTAGES = {"video_ingest.probe", "video_ingest.audio",
                   "video_ingest.frames", "video_ingest.segment",
                   "video_ingest.ocr"}


def js_string_array(src: str, var: str) -> list[str]:
    """Extract `var <name> = ["…", …];` — ordered string literals."""
    m = re.search(r"var " + var + r" = \[(.*?)\];", src, re.S)
    if m is None:
        raise AssertionError(f"var {var} array not found in app.js")
    return re.findall(r'"([^"]+)"', m.group(1))


def js_object_keys(src: str, var: str) -> set[str]:
    """Extract the keys of `var <name> = {…};` (bare and quoted forms).
    Safe without anchors: the mirrored tables' VALUES contain no ASCII colon
    (labels use 「：」/「 · 」), so `token:` only ever matches a key."""
    m = re.search(r"var " + var + r" = \{(.*?)\};", src, re.S)
    if m is None:
        raise AssertionError(f"var {var} object not found in app.js")
    return {a or b for a, b in
            re.findall(r'(?:"([^"]+)"|([A-Za-z_]\w*))\s*:', m.group(1))}


class FrontendContractTest(unittest.TestCase):
    """app.js / index.html static contract (spec §6.3, §3.6, §3.4 mirror)."""

    # -- §6.3: 无硬编码站数 — track length derives from stage_total/labels --

    def test_no_hardcoded_station_count_literals(self):
        for shape in ("|| 12", "buildTrack(12)", "|| 14", "buildTrack(14)"):
            self.assertNotIn(
                shape, APP_JS,
                f"station-count literal {shape!r} reappeared in app.js "
                "(spec §6.3: 前端无硬编码站数)")
        self.assertIn("buildTrack(STAGE_LABELS.length)", APP_JS)
        self.assertIn("(cur && cur.stage_total) || STAGE_LABELS.length", APP_JS)
        self.assertIn("(cur.stage_total || STAGE_LABELS.length)", APP_JS)

    # -- §3.4 mirror: frontend stage tables == run_meeting.STAGE_ORDER -----

    @unittest.skipUnless(HAS_RUN_MEETING, "run_meeting 需要 config.json")
    def test_stage_labels_mirror_stage_order_exactly(self):
        self.assertEqual(js_string_array(APP_JS, "STAGE_LABELS"),
                         [label for _key, label in run_meeting.STAGE_ORDER])

    @unittest.skipUnless(HAS_RUN_MEETING, "run_meeting 需要 config.json")
    def test_stage_keys_cover_every_station(self):
        keys = js_object_keys(APP_JS, "STAGE_KEYS")
        for station, _label in run_meeting.STAGE_ORDER:
            self.assertIn(station, keys,
                          f"STAGE_KEYS 缺少站点 {station}（时间线将退化显示原始键）")

    def test_video_ingest_substages_match_pipeline_emissions(self):
        # Text-level (no import needed): every substage id run_meeting.py
        # emits must equal the pinned five AND carry a STAGE_KEYS label.
        emitted = set(re.findall(
            r'emit_progress\([^\n]*?"(video_ingest\.\w+)"', RUN_MEETING_PY))
        self.assertEqual(emitted, VIDEO_SUBSTAGES,
                         "run_meeting.py 的 video_ingest 子阶段集合与钉死的五个不一致")
        self.assertLessEqual(VIDEO_SUBSTAGES, js_object_keys(APP_JS, "STAGE_KEYS"))

    # -- §3.5/§3.6: five video extensions across filter, server, accept ----

    def test_video_exts_match_server_and_pipeline(self):
        js_exts = js_object_keys(APP_JS, "VIDEO_EXTS")
        self.assertEqual(js_exts, VIDEO_EXTS)  # drag-drop filter literal pin
        self.assertEqual(js_exts, {e.lstrip(".") for e in webapp.VIDEO_EXTS})
        if HAS_MEETING_PIPELINE:  # §3.7 inventory classifier mirror
            self.assertEqual(
                js_exts,
                {e.lstrip(".") for e in meeting_pipeline.VIDEO_EXTENSIONS})

    def test_index_accept_lists_all_video_extensions(self):
        tag = re.search(r'<input[^>]*id="pick-audio"[^>]*>', INDEX_HTML, re.S)
        self.assertIsNotNone(tag, "index.html 缺少 pick-audio 文件输入")
        accept = re.search(r'accept="([^"]+)"', tag.group(0))
        self.assertIsNotNone(accept, "pick-audio 缺少 accept 属性")
        listed = {e.strip().lstrip(".").lower()
                  for e in accept.group(1).split(",") if e.strip()}
        self.assertLessEqual(VIDEO_EXTS, listed,
                             "accept 未覆盖五个视频扩展名（§3.6 拖放区契约）")


if __name__ == "__main__":
    unittest.main()
