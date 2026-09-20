"""Retrieval-layer tests: three-tier recall, multi-token merge, and CJK
no-space shingle recall — the two real starvation defects found by the
2026-09-20 demo ('社团 招新' 0 hits; '机器人社招新是什么时候' 0 hits).

Hermetic: builds a tiny meeting.db fixture with the production trigram
FTS5 schema — no real-event database is touched.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "mcp_meeting_server", WS / "core" / "scripts" / "mcp_meeting_server.py")
mcp = importlib.util.module_from_spec(_spec)
sys.modules["mcp_meeting_server"] = mcp
_spec.loader.exec_module(mcp)

UNITS = [
    ("U000001", "社团活动/招新", "机器人社团将在10月15号举办招新活动，地点是二楼活动室。", "medium", ["A000001"]),
    ("U000002", "场馆/图书馆", "图书馆的开放时间将从下周一开始延长至晚上九点。", "medium", ["A000001"]),
    ("U000003", "外联/交换项目", "合作伙伴学校确认了11月的交换访问，将派出12名学生。", "medium", ["A000002"]),
    ("U000004", "社团活动/workshop", "The robotics club will host a joint workshop on campus.", "medium", ["A000002"]),
]
LITERALS = [
    ("R000001", 3.0, 8.0, "第二，机器人社团将在十月十五号举办招新活动。"),
    ("R000002", 9.0, 14.0, "关于招新要不要收报名费，会上没有明确说。"),
    ("R000003", 27.0, 38.0, "Hello everyone. The partner school confirmed the visit."),
]


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE units(unit_id TEXT PRIMARY KEY, topic_path TEXT, claim TEXT,
                           certainty TEXT, evidence_ids_json TEXT);
        CREATE VIRTUAL TABLE units_fts USING fts5(unit_id UNINDEXED, topic_path, claim,
                                                  tokenize='trigram');
        CREATE TABLE literal_records(record_id TEXT PRIMARY KEY, start_seconds REAL,
                                     end_seconds REAL, clean_literal TEXT);
    """)
    for uid, topic, claim, cert, evs in UNITS:
        conn.execute("INSERT INTO units VALUES (?,?,?,?,?)",
                     (uid, topic, claim, cert, json.dumps(evs)))
        conn.execute("INSERT INTO units_fts(unit_id, topic_path, claim) VALUES (?,?,?)",
                     (uid, topic, claim))
    for rid, s, e, text in LITERALS:
        conn.execute("INSERT INTO literal_records VALUES (?,?,?,?)", (rid, s, e, text))
    conn.commit()
    conn.close()


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "meeting.db"
        build_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def search(self, query, limit=5):
        return mcp._search_one("fixture", self.db, query, limit)

    def test_english_fts_hit(self):
        res = self.search("robotics")
        self.assertEqual([u["unit_id"] for u in res["units"]], ["U000004"])

    def test_multitoken_no_starvation(self):
        # regression: '社团 招新' must not AND-starve
        res = self.search("社团 招新")
        self.assertIn("U000001", [u["unit_id"] for u in res["units"]])

    def test_cjk_nospace_shingle_recall(self):
        # regression: natural zh question is neither substring nor splittable
        res = self.search("机器人社招新是什么时候")
        self.assertIn("U000001", [u["unit_id"] for u in res["units"]])

    def test_evidence_tier_fallback(self):
        # '报名费' appears only in a literal record, no unit claims it
        res = self.search("报名费")
        self.assertEqual(res["units"], [])
        self.assertEqual([e["record_id"] for e in res["evidence"]], ["R000002"])
        self.assertEqual(res["evidence"][0]["at"], "00:09-00:14")

    def test_shingles_shape(self):
        got = mcp._cjk_shingles("机器人社招新")
        self.assertEqual(set(got), {"机器", "器人", "人社", "社招", "招新"})
        self.assertEqual(mcp._cjk_shingles("robotics club"), [])  # latin: not our lane
        self.assertEqual(mcp._cjk_shingles("语"), [])  # single char run: no bigram


if __name__ == "__main__":
    unittest.main()
