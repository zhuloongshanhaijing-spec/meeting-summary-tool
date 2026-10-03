"""Tests for core/scripts/suggest_ocr_corrections.py — annotation-only OCR
term-correction suggestions (spec §3.3 → §4.6).

Green under BOTH interpreters:
  * vendor/tools-venv/bin/python → pypinyin present → full coverage;
  * system python3 (zero third-party packages) → pinyin-dependent tests
    skip via unittest.skipUnless; edit-distance, gating, stopword,
    immutability and empty-input tests still run everywhere.

The script is exercised through its real CLI (subprocess, sys.executable)
so the interpreter-dependent lazy-pypinyin behavior is tested as shipped.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
SCRIPT = WS / "core" / "scripts" / "suggest_ocr_corrections.py"

try:
    HAS_PYPINYIN = importlib.util.find_spec("pypinyin") is not None
except (ImportError, ValueError):
    HAS_PYPINYIN = False

SPEC_FIELDS = {"record_id", "original", "suggested", "ocr_page_id",
               "ocr_evidence_id", "basis", "engines_disagreed"}

DISAGREEMENT_MARK = ("acoustic_route_disagreement; "
                     "note text must not repair this transcript")


def make_record(record_id: str, literal: str, disagreed: bool = True) -> dict:
    """A record shaped exactly like run_meeting.stage_literal_records output:
    the qwen route writes certainty/uncertainty/route_agreement_min from the
    measured cross-route agreement (run_meeting.py:608-617); the whisper
    route writes certainty=medium / uncertainty=null / agreement=1.0
    (run_meeting.py:573-574) — that is the `disagreed=False` shape."""
    return {
        "record_id": record_id,
        "source_id": f"F000001_{record_id}_0000000000_000020000",
        "start_seconds": 0.0, "end_seconds": 20.0,
        "clean_literal": literal, "raw_text": literal,
        "certainty": "low" if disagreed else "medium",
        "uncertainty": DISAGREEMENT_MARK if disagreed else None,
        "route_agreement_min": 0.8123 if disagreed else 1.0,
        "engine": "qwen-window-zh" if disagreed else "whisper-auto",
        "evidence_ids": ["A000001"],
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.td = Path(self.tmp.name)
        self.records = self.td / "literal_records.jsonl"
        self.ocr = self.td / "ocr.jsonl"
        self.output = self.td / "ocr_corrections.jsonl"
        self.receipt = self.td / "ocr_corrections_receipt.json"

    def run_script(self, *extra: str) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPT),
               "--records", str(self.records), "--slides-ocr", str(self.ocr),
               "--output", str(self.output), "--receipt", str(self.receipt),
               *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def read_output(self) -> list[dict]:
        return [json.loads(line)
                for line in self.output.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def read_receipt(self) -> dict:
        return json.loads(self.receipt.read_text(encoding="utf-8"))


class PinyinSuggestionTest(ScriptTestCase):
    @unittest.skipUnless(HAS_PYPINYIN, "pypinyin 未安装（系统 python3 走近形路径）")
    def test_homophone_with_disagreement_suggests(self):
        """spec §4.6 示例对：张菁(ASR) vs 张京(OCR)，同音 zhang jing + 该记录
        存在实测路由分歧 → 产出 pinyin 建议；字段集与 §4.6 完全一致。"""
        from pypinyin import lazy_pinyin
        self.assertEqual(lazy_pinyin("张菁"), lazy_pinyin("张京"))  # 测试前提核验
        write_jsonl(self.records, [make_record("R000001", "下面请张菁老师发言。")])
        write_jsonl(self.ocr, [{"file": "P3.png",
                                "items": [{"text": "主讲人：张京", "confidence": 0.9}],
                                "error": None}])
        self.run_script()
        rows = self.read_output()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), SPEC_FIELDS)
        self.assertEqual(row["record_id"], "R000001")
        self.assertEqual(row["original"], "张菁")
        self.assertEqual(row["suggested"], "张京")
        self.assertEqual(row["ocr_page_id"], "P3")       # file stem, 无扩展名
        self.assertIsNone(row["ocr_evidence_id"])        # 未提供 evidence-map
        self.assertEqual(row["basis"], "pinyin")
        self.assertTrue(row["engines_disagreed"])
        receipt = self.read_receipt()
        self.assertEqual(receipt, {"schema_version": 1, "status": "complete",
                                   "records_scanned": 1, "corrections_count": 1,
                                   "pypinyin_available": True})

    @unittest.skipUnless(HAS_PYPINYIN, "pypinyin 未安装（系统 python3 走近形路径）")
    def test_pinyin_only_pair_needs_pinyin_path(self):
        """余静 vs 俞敬：同音（yu jing）但编辑距离=2 —— 只有同音路径能产出。
        这对组合被 --disable-pinyin 测试用作『关掉拼音则不产出』的证据。"""
        from pypinyin import lazy_pinyin
        self.assertEqual(lazy_pinyin("余静"), lazy_pinyin("俞敬"))  # 测试前提核验
        write_jsonl(self.records, [make_record("R000002", "这个项目由余静负责。")])
        write_jsonl(self.ocr, [{"file": "P5.png", "items": [{"text": "俞敬"}], "error": None}])
        self.run_script()
        rows = self.read_output()
        self.assertEqual([(r["original"], r["suggested"], r["basis"]) for r in rows],
                         [("余静", "俞敬", "pinyin")])


class EditDistanceSuggestionTest(ScriptTestCase):
    def test_near_shape_latin_with_disagreement_suggests(self):
        """GPT4(ASR) vs GPT5(OCR)：编辑距离 1 → edit_distance 建议（双解释器均跑）。"""
        write_jsonl(self.records, [make_record("R000003", "我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [{"file": "P1.png",
                                "items": [{"text": "GPT5 基线对比"}], "error": None}])
        self.run_script()
        rows = self.read_output()
        self.assertEqual([(r["original"], r["suggested"], r["basis"], r["ocr_page_id"])
                          for r in rows],
                         [("GPT4", "GPT5", "edit_distance", "P1")])
        self.assertTrue(rows[0]["engines_disagreed"])

    def test_latin_case_difference_is_not_a_correction(self):
        """仅大小写差异（fold 后相同）→ original == suggested，不产出。"""
        write_jsonl(self.records, [make_record("R000004", "我们先用gpt4跑一遍基线。")])
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT4"}], "error": None}])
        self.run_script()
        self.assertEqual(self.read_output(), [])
        self.assertEqual(self.read_receipt()["corrections_count"], 0)


class IdenticalTermSkipTest(ScriptTestCase):
    def test_cjk_identical_term_never_self_suggests(self):
        """I-2：OCR 词项与 ASR 词面逐字相同（常见情形：幻灯片有人名、路由有
        分歧、但 ASR 本来就写对了）→ 零建议。CJK 侧 no-self-suggestion 守卫，
        与 test_latin_case_difference_is_not_a_correction 对称。"""
        write_jsonl(self.records, [make_record("R000018", "下面请张京老师发言。")])
        write_jsonl(self.ocr, [{"file": "P3.png",
                                "items": [{"text": "主讲人：张京"}], "error": None}])
        self.run_script()
        self.assertEqual(self.read_output(), [])
        self.assertEqual(self.read_receipt()["corrections_count"], 0)


class DisagreementGateTest(ScriptTestCase):
    """铁律 #10：没有实测分歧就绝不产出建议——分歧信号只来自记录字段
    （uncertainty / route_agreement_min，run_meeting.py:608-617）或外部
    --asr-disagreements 工件，绝不臆造。"""

    def test_no_disagreement_zero_output(self):
        write_jsonl(self.records,
                    [make_record("R000005", "我们先用GPT4跑一遍基线。", disagreed=False)])
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}])
        self.run_script()
        self.assertEqual(self.read_output(), [])
        receipt = self.read_receipt()
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["records_scanned"], 1)
        self.assertEqual(receipt["corrections_count"], 0)

    def test_low_agreement_alone_qualifies(self):
        """仅 route_agreement_min < 0.93（uncertainty=null）也算实测分歧。"""
        record = make_record("R000006", "我们先用GPT4跑一遍基线。", disagreed=False)
        record["route_agreement_min"] = 0.9255
        write_jsonl(self.records, [record])
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}])
        self.run_script()
        self.assertEqual([(r["original"], r["suggested"]) for r in self.read_output()],
                         [("GPT4", "GPT5")])

    def test_uncertainty_alone_qualifies(self):
        """I-3：uncertainty 带实测分歧标记而 route_agreement_min=1.0 → 仍合格。
        隔离 uncertainty 分支（disagreed=True 夹具同时带 0.8123 低 agreement，
        单删 uncertainty 检查的变异体此前可存活）。"""
        record = make_record("R000019", "我们先用GPT4跑一遍基线。", disagreed=False)
        record["uncertainty"] = DISAGREEMENT_MARK
        record["route_agreement_min"] = 1.0
        write_jsonl(self.records, [record])
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}])
        self.run_script()
        self.assertEqual([(r["original"], r["suggested"]) for r in self.read_output()],
                         [("GPT4", "GPT5")])

    def test_asr_disagreements_file_qualifies_record(self):
        """记录自身无分歧标记，但外部引擎分歧工件列出了它 → 允许产出。"""
        write_jsonl(self.records,
                    [make_record("R000007", "我们先用GPT4跑一遍基线。", disagreed=False)])
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}])
        disagreements = self.td / "asr_disagreements.jsonl"
        write_jsonl(disagreements, [{"record_id": "R000007"}])
        self.run_script("--asr-disagreements", str(disagreements))
        self.assertEqual([(r["original"], r["suggested"]) for r in self.read_output()],
                         [("GPT4", "GPT5")])


class StopwordTest(ScriptTestCase):
    def test_stopword_ocr_term_ignored(self):
        """OCR 词项『现在』是停用词：即使与 ASR 词面『观在』近形（编辑距离 1）
        且记录有分歧，也不产出（spec §3.3 条件 3）。"""
        write_jsonl(self.records, [make_record("R000008", "观在开始汇报。")])
        write_jsonl(self.ocr, [{"file": "P2.png", "items": [{"text": "现在"}], "error": None}])
        self.run_script()
        self.assertEqual(self.read_output(), [])
        self.assertEqual(self.read_receipt()["corrections_count"], 0)


class ImmutabilityTest(ScriptTestCase):
    def test_input_files_sha256_unchanged(self):
        """输入文件（records/ocr）运行前后 sha256 逐字节一致——建议是标注式，
        ASR 原文永不被改写（决策⑤铁律）。"""
        write_jsonl(self.records, [
            make_record("R000009", "下面请张菁老师发言。"),
            make_record("R000010", "我们先用GPT4跑一遍基线。"),
        ])
        write_jsonl(self.ocr, [{"file": "P3.png",
                                "items": [{"text": "主讲人：张京"}, {"text": "GPT5"}],
                                "error": None}])
        before = {p: sha256(p) for p in (self.records, self.ocr)}
        self.run_script()
        for path, digest in before.items():
            self.assertEqual(sha256(path), digest, f"{path.name} 被改写了")
        # 建议确实产出了（不是空跑造成的『未修改』假象）
        self.assertGreaterEqual(self.read_receipt()["corrections_count"], 1)


class EmptyAndDegradedInputTest(ScriptTestCase):
    def test_empty_inputs_valid_receipt(self):
        self.records.write_text("", encoding="utf-8")
        self.ocr.write_text("", encoding="utf-8")
        self.run_script()
        self.assertEqual(self.read_output(), [])
        receipt = self.read_receipt()
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["records_scanned"], 0)
        self.assertEqual(receipt["corrections_count"], 0)
        self.assertEqual(receipt["pypinyin_available"], HAS_PYPINYIN)

    def test_ocr_error_page_and_missing_items_never_crash(self):
        write_jsonl(self.records, [make_record("R000011", "我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [
            {"file": "P1.png", "items": [], "error": "vision_request_failed: boom"},
            {"file": "P2.png"},                       # items 缺失
            {"file": "P3.png", "items": [{"text": "GPT5"}], "error": None},
        ])
        self.run_script()
        self.assertEqual([(r["original"], r["suggested"], r["ocr_page_id"])
                          for r in self.read_output()],
                         [("GPT4", "GPT5", "P3")])


class DisablePinyinTest(ScriptTestCase):
    def test_disable_pinyin_receipt_and_behavior(self):
        """--disable-pinyin：receipt pypinyin_available=false；同音对
        （余静/俞敬，编辑距离 2 → 近形路径抓不到）不产出；近形对
        （GPT4/GPT5）照常产出。双解释器行为一致（系统 python3 本就无 pypinyin）。"""
        write_jsonl(self.records, [make_record(
            "R000012", "这个项目由余静负责，我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [{"file": "P5.png",
                                "items": [{"text": "俞敬"}, {"text": "GPT5"}],
                                "error": None}])
        self.run_script("--disable-pinyin")
        self.assertFalse(self.read_receipt()["pypinyin_available"])
        pairs = {(r["original"], r["suggested"]) for r in self.read_output()}
        self.assertNotIn(("余静", "俞敬"), pairs)   # 同音路径已关闭
        self.assertIn(("GPT4", "GPT5"), pairs)      # 近形路径仍在

    @unittest.skipUnless(HAS_PYPINYIN, "pypinyin 未安装（系统 python3 无从对照）")
    def test_enabled_pinyin_finds_the_same_pair(self):
        """对照组：同一数据不关拼音 → 余静/俞敬 以 basis=pinyin 产出。"""
        write_jsonl(self.records, [make_record("R000013", "这个项目由余静负责。")])
        write_jsonl(self.ocr, [{"file": "P5.png", "items": [{"text": "俞敬"}], "error": None}])
        self.run_script()
        self.assertTrue(self.read_receipt()["pypinyin_available"])
        self.assertEqual([(r["original"], r["suggested"], r["basis"])
                          for r in self.read_output()],
                         [("余静", "俞敬", "pinyin")])


class EvidenceMapTest(ScriptTestCase):
    def test_evidence_map_populates_ocr_evidence_id(self):
        write_jsonl(self.records, [make_record("R000014", "我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [{"file": "P3.png", "items": [{"text": "GPT5"}], "error": None}])
        evidence_map = self.td / "evidence_map.jsonl"
        write_jsonl(evidence_map, [{"page_id": "P3", "evidence_id": "I000003"},
                                   {"page_id": "P9", "evidence_id": "I000009"}])
        self.run_script("--evidence-map", str(evidence_map))
        rows = self.read_output()
        self.assertEqual([r["ocr_evidence_id"] for r in rows], ["I000003"])

    def test_unmapped_page_keeps_null_evidence_id(self):
        write_jsonl(self.records, [make_record("R000015", "我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [{"file": "P3.png", "items": [{"text": "GPT5"}], "error": None}])
        evidence_map = self.td / "evidence_map.jsonl"
        write_jsonl(evidence_map, [{"page_id": "P7", "evidence_id": "I000007"}])
        self.run_script("--evidence-map", str(evidence_map))
        self.assertEqual([r["ocr_evidence_id"] for r in self.read_output()], [None])


class DedupeAndCapTest(ScriptTestCase):
    def test_per_record_cap_five_and_pair_dedupe(self):
        """7 组近形候选 → 每记录上限 5；重复页的同名候选按
        (record_id, original, suggested) 去重，排序最小页胜出。"""
        literal = "aaa bbb ccc ddd eee fff ggg"
        write_jsonl(self.records, [make_record("R000016", literal)])
        write_jsonl(self.ocr, [
            {"file": "P2.png", "items": [{"text": "aab bbc ccd dde eef ffg ggh"}], "error": None},
            {"file": "P9.png", "items": [{"text": "aab"}], "error": None},  # 跨页重复
        ])
        self.run_script()
        rows = self.read_output()
        self.assertEqual(len(rows), 5)
        keys = [(r["record_id"], r["original"], r["suggested"]) for r in rows]
        self.assertEqual(len(set(keys)), 5)                       # 无重复对
        self.assertEqual(keys, [("R000016", o, s) for o, s in
                                [("aaa", "aab"), ("bbb", "bbc"), ("ccc", "ccd"),
                                 ("ddd", "dde"), ("eee", "eef")]])  # 确定性扫描序
        self.assertTrue(all(r["ocr_page_id"] == "P2" for r in rows))


class CrossPageDedupeTest(ScriptTestCase):
    def test_shared_term_cites_natural_sort_first_page(self):
        """I-1 + M-4：同一 OCR 词项出现在 P2 与 P10（总匹配数 1 < 上限 5，
        排除 cap 遮蔽）→ 恰一行且引用自然序最小页 P2（数字感知排序；字典序
        会让时间上更晚的 P10 胜出）。OCR 行故意 P10 在前，证明是排序而非
        输入顺序决定归属。"""
        write_jsonl(self.records, [make_record("R000017", "我们先用GPT4跑一遍基线。")])
        write_jsonl(self.ocr, [
            {"file": "P10.png", "items": [{"text": "GPT5"}], "error": None},
            {"file": "P2.png", "items": [{"text": "GPT5"}], "error": None},
        ])
        self.run_script()
        rows = self.read_output()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["original"], rows[0]["suggested"]), ("GPT4", "GPT5"))
        self.assertEqual(rows[0]["ocr_page_id"], "P2")


class CliFailurePathTest(ScriptTestCase):
    """M-9：CLI 失败路径钉死——坏行/缺文件都 exit 1、报路径、零写出。"""

    def run_script_raw(self, *extra: str) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPT),
               "--records", str(self.records), "--slides-ocr", str(self.ocr),
               "--output", str(self.output), "--receipt", str(self.receipt),
               *extra]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_malformed_jsonl_line_exits_1_without_writing_outputs(self):
        self.records.write_text(
            '{"record_id": "R000020", "clean_literal": "GPT4"\n这不是JSON\n',
            encoding="utf-8")
        write_jsonl(self.ocr, [{"file": "P1.png", "items": [{"text": "GPT5"}], "error": None}])
        proc = self.run_script_raw()
        self.assertEqual(proc.returncode, 1)
        self.assertIn(str(self.records), proc.stderr)   # 消息带路径前缀
        self.assertIn("JSON", proc.stderr)
        self.assertFalse(self.output.exists())          # 失败时不写任何输出
        self.assertFalse(self.receipt.exists())

    def test_missing_input_file_exits_1(self):
        missing = self.td / "no_such_records.jsonl"
        write_jsonl(self.ocr, [])
        proc = self.run_script_raw("--records", str(missing))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("input file not found", proc.stderr)
        self.assertIn(str(missing), proc.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()
