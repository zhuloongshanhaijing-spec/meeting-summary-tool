"""Fragment-assembly v2 contracts: semantic pairing, honest degradation.

Same tempdir + synthetic-data style as tests/test_recovery_console.py.
literal_records.jsonl is never written by the module under test; the CLI may
only create --output.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "core" / "scripts"))
sys.path.insert(0, str(WS))

import fragment_assembly as fa  # noqa: E402


def make_run(runs: Path, name: str, sentences, source: bool = True):
    """One fragment event: source/<name>.m4a + literal_records.jsonl."""
    (runs / name / "source").mkdir(parents=True)
    if source:
        (runs / name / "source" / (name + ".m4a")).write_bytes(b"x")
    rows = "".join(
        json.dumps({"clean_literal": text, "raw_text": text, "end_seconds": end},
                   ensure_ascii=False) + "\n"
        for text, end in sentences)
    (runs / name / "literal_records.jsonl").write_text(rows, encoding="utf-8")


def run_cli(runs: Path, output: Path, extra=()):
    code = fa.main(["--runs-dir", str(runs), "--output", str(output), *extra])
    plan = None
    if output.exists():
        plan = json.loads(output.read_text(encoding="utf-8"))
    return code, plan


def group_of(plan, fragment_id):
    for group in plan["groups"]:
        if fragment_id in group["fragment_ids"]:
            return group
    return None


def junction_for(plan, left, right):
    for junction in plan["junctions"]:
        if {junction["left"], junction["right"]} == {left, right}:
            return junction
    return None


def answer_from_judgments(prompt: str, judgments: dict) -> str:
    """Fake a strict-JSON ollama answer for every pair listed in the prompt."""
    body = prompt.split("待判定片段对", 1)[1] if "待判定片段对" in prompt else ""
    out = []
    for a, b in re.findall(r'\["([^"]+)",\s*"([^"]+)"\]', body):
        entry = judgments.get(frozenset((a, b)))
        if entry is None:
            relation, left, right = "different", "", ""
        else:
            relation = entry["relation"]
            left = entry.get("left", "")
            right = entry.get("right", "")
        out.append({"pair": [a, b], "relation": relation, "left": left, "right": right,
                    "left_summary": (entry or {}).get("left_summary", ""),
                    "right_summary": (entry or {}).get("right_summary", "")})
    return json.dumps(out, ensure_ascii=False)


def make_llm(responder):
    calls = []

    def fake(url, model, prompt, timeout):
        calls.append(prompt)
        return responder(prompt)

    return fake, calls


VOLUNTEER_ROWS = {
    "fragment-z-start": [("今天的社团例会开始", 4.0), ("中间讨论了很多事务", 9.0),
                         ("活动安排需要志愿者", 15.0)],
    "fragment-a-end": [("需要志愿者报名", 6.0), ("报名表交给部长", 11.0),
                       ("报名周五截止", 18.0)],
}
BUDGET_ROWS = {
    "fragment-a1": [("预算专题会现在开始", 5.0), ("先过一遍上季度决算", 12.0),
                    ("下一步审批预算方案", 20.0)],
    "fragment-a3": [("最终表决已经完成", 3.0), ("结果将在内网公示", 8.0),
                    ("会议结束", 12.0)],
}
HOMEWORK_ROWS = {
    "fragment-x": [("今天先讲数学作业问题", 2.0), ("几道大题要重点讲", 6.0),
                   ("下周考试复习范围已发", 10.0)],
}


class NoLlmGroupingTest(unittest.TestCase):
    def test_same_meeting_pairs_group_and_gap_fragments_stay_separate(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in {**BUDGET_ROWS, **VOLUNTEER_ROWS, **HOMEWORK_ROWS}.items():
                make_run(runs, name, rows)
            output = runs / ".assembly-plan.json"
            code, plan = run_cli(runs, output, ["--no-llm"])
            self.assertEqual(code, 0)
            self.assertFalse(plan["llm_used"])
            self.assertEqual(plan["llm_skipped_reason"], "disabled_by_flag")
            self.assertEqual(plan["skipped"], [])
            # obvious same-meeting pair grouped, correctly ordered
            pair_group = group_of(plan, "fragment-z-start")
            self.assertIsNotNone(pair_group)
            self.assertEqual(pair_group["fragment_ids"], ["fragment-z-start", "fragment-a-end"])
            junction = junction_for(plan, "fragment-z-start", "fragment-a-end")
            self.assertIsNotNone(junction)
            self.assertEqual(junction["relation"], "same_continuous")
            self.assertFalse(junction["gap"])
            self.assertEqual(junction["left_summary"], "")
            self.assertEqual(junction["right_summary"], "")
            self.assertEqual(junction["source"], "fallback")
            self.assertEqual(pair_group["confidence"], "medium")  # no LLM -> never high
            # missing middle (a1/a3): no false continuous edge, still separated
            self.assertNotEqual(group_of(plan, "fragment-a1"), group_of(plan, "fragment-a3"))
            self.assertIsNone(junction_for(plan, "fragment-a1", "fragment-a3"))
            # third meeting stays its own group
            self.assertEqual(group_of(plan, "fragment-x")["fragment_ids"], ["fragment-x"])
            # different meetings never merge
            all_ids = [set(g["fragment_ids"]) for g in plan["groups"]]
            self.assertIn({"fragment-z-start", "fragment-a-end"}, all_ids)
            for grouped in all_ids:
                if "fragment-a1" in grouped:
                    self.assertEqual(grouped, {"fragment-a1"})


class LlmPathTest(unittest.TestCase):
    MEETING_ROWS = {
        "fragment-m1": [("会议开场介绍议题", 5.0), ("下面讨论项目进度", 20.0)],
        "fragment-m2": [("项目进度按周汇报", 2.0), ("接下来看预算部分", 15.0)],
        "fragment-m2b": [("材料已提前发出", 1.0), ("请大家先看第二页", 10.0)],
        "fragment-m3": [("预算超支需要审批", 3.0), ("散会感谢大家参与", 9.0)],
        "fragment-m4": [("今天天气非常好", 4.0), ("我们出门去郊游", 12.0)],
    }
    JUDGMENTS = {
        frozenset(("fragment-m1", "fragment-m2")): {
            "relation": "same_continuous", "left": "fragment-m1", "right": "fragment-m2"},
        frozenset(("fragment-m2", "fragment-m3")): {
            "relation": "same_continuous", "left": "fragment-m2", "right": "fragment-m3"},
        frozenset(("fragment-m1", "fragment-m3")): {
            "relation": "same_gap", "left": "fragment-m1", "right": "fragment-m3",
            "left_summary": "开场介绍并进入项目进度", "right_summary": "预算超支待审批随后散会"},
        frozenset(("fragment-m1", "fragment-m2b")): {"relation": "same_reorder"},
        frozenset(("fragment-m2", "fragment-m2b")): {"relation": "same_reorder"},
        frozenset(("fragment-m2b", "fragment-m3")): {"relation": "same_reorder"},
    }

    def test_four_classifications_drive_groups_junctions_and_gap_notes(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in self.MEETING_ROWS.items():
                make_run(runs, name, rows)
            fake, calls = make_llm(lambda prompt: answer_from_judgments(prompt, self.JUDGMENTS))
            with mock.patch.object(fa, "ollama_generate", fake):
                code, plan = run_cli(runs, runs / ".assembly-plan.json")
            self.assertEqual(code, 0)
            self.assertTrue(plan["llm_used"])
            self.assertEqual(plan["degraded_pairs"], [])
            four = group_of(plan, "fragment-m1")
            self.assertEqual(four["fragment_ids"],
                             ["fragment-m1", "fragment-m2", "fragment-m2b", "fragment-m3"])
            self.assertEqual(four["confidence"], "medium")  # m2->m2b lacks a continuous edge
            self.assertEqual(group_of(plan, "fragment-m4")["fragment_ids"], ["fragment-m4"])
            self.assertTrue(all(j["relation"] != "different" for j in plan["junctions"]))
            self.assertTrue(all("fragment-m4" not in (j["left"], j["right"])
                                for j in plan["junctions"]))
            gap = junction_for(plan, "fragment-m1", "fragment-m3")
            self.assertIsNotNone(gap)
            self.assertTrue(gap["gap"])
            self.assertEqual(gap["relation"], "same_gap")
            self.assertEqual(gap["left_summary"], "开场介绍并进入项目进度")
            self.assertEqual(gap["right_summary"], "预算超支待审批随后散会")
            reorder = junction_for(plan, "fragment-m1", "fragment-m2b")
            self.assertEqual(reorder["relation"], "same_reorder")
            self.assertFalse(reorder["gap"])
            # LLM was really consulted once per batch of pairs
            self.assertTrue(any("待判定片段对" in c for c in calls))

    def test_fully_continuous_llm_chain_scores_high_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            make_run(runs, "fragment-c1", [("项目立项背景介绍", 5.0), ("首先看需求清单", 18.0)])
            make_run(runs, "fragment-c2", [("首先看需求清单", 2.0), ("需求确认进入设计", 16.0)])
            make_run(runs, "fragment-c3", [("需求确认进入设计", 3.0), ("设计评审结束散会", 14.0)])
            judgments = {
                frozenset(("fragment-c1", "fragment-c2")): {
                    "relation": "same_continuous", "left": "fragment-c1", "right": "fragment-c2"},
                frozenset(("fragment-c1", "fragment-c3")): {
                    "relation": "same_gap", "left": "fragment-c1", "right": "fragment-c3",
                    "left_summary": "项目背景与需求清单", "right_summary": "设计评审与散会"},
                frozenset(("fragment-c2", "fragment-c3")): {
                    "relation": "same_continuous", "left": "fragment-c2", "right": "fragment-c3"},
            }
            fake, _ = make_llm(lambda prompt: answer_from_judgments(prompt, judgments))
            with mock.patch.object(fa, "ollama_generate", fake):
                code, plan = run_cli(runs, runs / ".assembly-plan.json")
            self.assertEqual(code, 0)
            group = plan["groups"][0]
            self.assertEqual(group["fragment_ids"], ["fragment-c1", "fragment-c2", "fragment-c3"])
            self.assertEqual(group["confidence"], "high")


class LlmDegradationTest(unittest.TestCase):
    ROWS = {**VOLUNTEER_ROWS, **HOMEWORK_ROWS}

    def assert_fallback_plan(self, plan, reason):
        self.assertTrue(plan["llm_used"])
        reasons = {(d["left"], d["right"], d["reason"]) for d in plan["degraded_pairs"]}
        self.assertTrue(any(triple[2] == reason for triple in reasons))
        junction = junction_for(plan, "fragment-z-start", "fragment-a-end")
        self.assertIsNotNone(junction)
        self.assertEqual(junction["relation"], "same_continuous")
        self.assertEqual(junction["source"], "fallback")
        self.assertEqual(junction["left_summary"], "")  # never fabricate summaries

    def test_timeout_degrades_pairs_with_one_retry_and_plan_still_writes(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in self.ROWS.items():
                make_run(runs, name, rows)
            calls = []

            def fake(url, model, prompt, timeout):
                calls.append(prompt)
                if "待判定片段对" in prompt:
                    raise TimeoutError("timed out")
                return "ok"

            with mock.patch.object(fa, "ollama_generate", fake):
                code, plan = run_cli(runs, runs / ".assembly-plan.json")
            self.assertEqual(code, 0)
            self.assert_fallback_plan(plan, "llm_timeout")
            batch_calls = sum("待判定片段对" in c for c in calls)
            degraded_n = len(plan["degraded_pairs"])
            # 新契约：批量 1 次 + 超时重试 1 次 + 每个 degraded 对一轮单对救援
            self.assertEqual(batch_calls, 2 + degraded_n)

    def test_bad_json_degrades_pairs_and_plan_still_writes(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in self.ROWS.items():
                make_run(runs, name, rows)

            def fake(url, model, prompt, timeout):
                if "待判定片段对" in prompt:
                    return "模型说明文字，不是 JSON"
                return "ok"

            with mock.patch.object(fa, "ollama_generate", fake):
                code, plan = run_cli(runs, runs / ".assembly-plan.json")
            self.assertEqual(code, 0)
            self.assert_fallback_plan(plan, "llm_bad_json")

    def test_unreachable_ollama_skips_llm_without_degraded_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in self.ROWS.items():
                make_run(runs, name, rows)

            def fake(url, model, prompt, timeout):
                raise urllib.error.URLError(ConnectionRefusedError())

            with mock.patch.object(fa, "ollama_generate", fake):
                code, plan = run_cli(runs, runs / ".assembly-plan.json")
            self.assertEqual(code, 0)
            self.assertFalse(plan["llm_used"])
            self.assertEqual(plan["llm_skipped_reason"], "ollama_unreachable")
            self.assertEqual(plan["degraded_pairs"], [])
            self.assertIsNotNone(junction_for(plan, "fragment-z-start", "fragment-a-end"))


class RobustnessTest(unittest.TestCase):
    def test_broken_fragment_dirs_are_skipped_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in VOLUNTEER_ROWS.items():
                make_run(runs, name, rows)
            broken = runs / "fragment-broken"
            broken.mkdir()
            (broken / "literal_records.jsonl").write_text("{oops\n", encoding="utf-8")
            empty = runs / "fragment-empty"
            empty.mkdir()
            (empty / "literal_records.jsonl").write_text("", encoding="utf-8")
            (runs / "fragment-nojsonl" / "source").mkdir(parents=True)
            output = runs / ".assembly-plan.json"
            code, plan = run_cli(runs, output, ["--no-llm"])
            self.assertEqual(code, 0)
            skipped = {entry["id"]: entry["reason"] for entry in plan["skipped"]}
            self.assertEqual(skipped["fragment-broken"], "broken_jsonl")
            self.assertEqual(skipped["fragment-empty"], "empty_transcript")
            self.assertEqual(skipped["fragment-nojsonl"], "missing_literal_records")
            pair_group = group_of(plan, "fragment-z-start")
            self.assertEqual(pair_group["fragment_ids"], ["fragment-z-start", "fragment-a-end"])

    def test_note_hint_orders_unlinked_groups_without_creating_edge(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            make_run(runs, "fragment-a", [("数学复习完成", 9.0)])
            make_run(runs, "fragment-z", [("预算审批完成", 9.0)])
            refs = runs / ".fragment-references"
            refs.mkdir()
            (refs / "order.md").write_text("预算审批完成\n数学复习完成\n", encoding="utf-8")
            code, plan = run_cli(runs, runs / ".assembly-plan.json", ["--no-llm"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["junctions"], [])
            self.assertEqual([g["fragment_ids"] for g in plan["groups"]],
                             [["fragment-z"], ["fragment-a"]])
            self.assertEqual(plan["group_order_basis"], "note")
            self.assertEqual(plan["groups"][0]["note_order_source"], "order.md")
            self.assertEqual(plan["groups"][0]["confidence"], "low")  # singleton
            fragments = {f["id"]: f for f in plan["fragments"]}
            self.assertEqual(fragments["fragment-z"]["note_hint_index"], 0)
            self.assertEqual(fragments["fragment-a"]["note_hint_index"], 1)
            self.assertGreater(fragments["fragment-z"]["note_hint_score"], 0)

    def test_output_is_valid_json_written_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, rows in VOLUNTEER_ROWS.items():
                make_run(runs, name, rows)
            output = runs / ".assembly-plan.json"
            code, plan = run_cli(runs, output, ["--no-llm"])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))  # valid JSON
            self.assertIsInstance(payload["generated_ts"], float)
            self.assertIn("缺段如实保留为缺口", payload["notice"])
            self.assertEqual(payload["status"], "draft")
            leftovers = [p.name for p in runs.iterdir()
                         if p.is_file() and p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])
            for fragment in payload["fragments"]:
                self.assertTrue(fragment["source"].endswith(".m4a"))
                self.assertGreater(fragment["duration_seconds"], 0)
                self.assertTrue(fragment["head"])


class ExitCodeTest(unittest.TestCase):
    def test_no_fragment_input_returns_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            runs.mkdir()
            (runs / "not-a-fragment").mkdir()
            output = runs / ".assembly-plan.json"
            code = fa.main(["--runs-dir", str(runs), "--output", str(output), "--no-llm"])
            self.assertEqual(code, 2)
            self.assertFalse(output.exists())

    def test_unwritable_output_returns_3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            make_run(runs, "fragment-solo", [("唯一片段的内容", 5.0)])
            blocker = root / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            code = fa.main(["--runs-dir", str(runs),
                            "--output", str(blocker / "plan.json"), "--no-llm"])
            self.assertEqual(code, 3)


class PromptEvidenceTest(unittest.TestCase):
    """提示词带词面证据 + 全 different 时的诚实校准提示。"""

    def test_build_prompt_includes_lexical_evidence(self):
        frags = {"f1": {"head": "预算会议开场", "tail": "收入增长"},
                 "f2": {"head": "预算第一项", "tail": "市场活动"}}
        scores = {("f1", "f2"): {"shared": 2, "shared_sample": ["预算", "会议"],
                                 "fwd_ab": 0.4, "fwd_ba": 0.1, "overall": 0.2}}
        prompt = fa.build_prompt(frags, [("f1", "f2")], scores)
        self.assertIn("词面证据", prompt)
        self.assertIn("共享关键词数 2", prompt)
        self.assertIn("预算", prompt)
        # 无 scores 时也应正常工作（向后兼容）
        prompt2 = fa.build_prompt(frags, [("f1", "f2")])
        self.assertIn("f1", prompt2)

    def test_build_plan_appends_extra_notice(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            make_run(runs, "fragment-a", [("预算会议内容", 4.0)])
            frag, reason = fa.read_fragment_run(runs / "fragment-a")
            self.assertIsNone(reason)
            frag.update({"keywords": [], "note_hint_index": None,
                         "note_hint_source": "", "note_hint_score": None})
            plan = fa.build_plan(types.SimpleNamespace(ollama_model="m"),
                                 [frag], [], [], {}, set(), [],
                                 True, None, extra_notice="追加提示")
        self.assertIn("追加提示", plan["notice"])
        self.assertIn("装配为候选视图", plan["notice"])


if __name__ == "__main__":
    unittest.main()


class GroupOrderingTest(unittest.TestCase):
    """二级排序通道：分组已定后的组内纯排序（判对边常互相矛盾）。"""

    def _frags(self):
        return {
            "f1": {"head": "Good morning everyone", "tail": "first item"},
            "f2": {"head": "the next item", "tail": "second topic"},
            "f3": {"head": "To conclude", "tail": "thank you all"},
        }

    def test_order_adopted_when_valid_json_array(self):
        frags = self._frags()
        with mock.patch.object(fa, "ollama_generate",
                               return_value='["f1", "f2", "f3"]'):
            ordered = fa.order_group_with_llm(frags, ["f3", "f1", "f2"],
                                              "http://x", "m", 5.0)
        self.assertEqual(ordered, ["f1", "f2", "f3"])

    def test_order_rejected_on_id_mismatch(self):
        # 返回的 id 集合与组员不符（幻觉/漏项）→ 弃用，保持拓扑序
        frags = self._frags()
        with mock.patch.object(fa.ollama_generate, "__wrapped__", None, create=True), \
             mock.patch.object(fa, "ollama_generate",
                               return_value='["f1", "f2"]'):
            ordered = fa.order_group_with_llm(frags, ["f1", "f2", "f3"],
                                              "http://x", "m", 5.0)
        self.assertIsNone(ordered)

    def test_order_none_on_ollama_failure(self):
        frags = self._frags()
        with mock.patch.object(fa, "ollama_generate",
                               side_effect=TimeoutError("cold")):
            self.assertIsNone(fa.order_group_with_llm(
                frags, ["f1", "f2"], "http://x", "m", 5.0))

    def test_build_plan_applies_llm_ordering(self):
        # 集成：build_plan 在 llm_used 时对每组重排序；fallback 时不动
        frags = [{"id": "f1", "source": "a1.m4a", "duration_seconds": 1.0,
                  "head": "Good morning", "tail": "first", "keywords": ["x"], "sentence_count": 3,
                  "note_hint_index": 1, "note_hint_source": "", "note_hint_score": None,
                  "note_order_hint": 1},
                 {"id": "f2", "source": "a2.m4a", "duration_seconds": 1.0,
                  "head": "next item", "tail": "second", "keywords": ["x"], "sentence_count": 3,
                  "note_hint_index": 2, "note_hint_source": "", "note_hint_score": None,
                  "note_order_hint": 2},
                 {"id": "f3", "source": "a3.m4a", "duration_seconds": 1.0,
                  "head": "To conclude", "tail": "thanks", "keywords": ["x"], "sentence_count": 3,
                  "note_hint_index": 3, "note_hint_source": "", "note_hint_score": None,
                  "note_order_hint": 3}]
        frag_by_id = {f["id"]: f for f in frags}
        args = types.SimpleNamespace(
            ollama_url="http://x", ollama_model="m", pair_timeout=5.0)
        def rel(r, lft, rgt, ls="", rs=""):
            return {"relation": r, "left": lft, "right": rgt,
                    "left_summary": ls, "right_summary": rs, "source": "llm"}
        relations = {("f1", "f2"): rel("same_continuous", "f1", "f2"),
                     ("f2", "f3"): rel("same_gap", "f2", "f3",
                                       "next item", "To conclude")}
        with mock.patch.object(fa, "ollama_generate",
                               return_value='["f1", "f3", "f2"]'):
            plan = fa.build_plan(args, frags, [], [], relations, set(), [],
                                 True, "", frag_by_id=frag_by_id)
        self.assertEqual(plan["groups"][0]["fragment_ids"],
                         ["f1", "f3", "f2"])


class OrderInvarianceTest(unittest.TestCase):
    """稳定性方案 2026-10-05 建议5a 的回归锁：同一语料换名（=换上传顺序）后，
    判读对序列与提示词字节必须完全一致——上传顺序不得再影响批组成。"""

    def _corpus(self, runs: Path, rename: dict):
        rows = {**BUDGET_ROWS, **VOLUNTEER_ROWS}
        for name, sents in rows.items():
            make_run(runs, rename.get(name, name), sents)

    def _pairs_and_prompt(self, rename: dict):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            self._corpus(runs, rename)
            frags, _skipped = fa.collect_fragments(runs)
            scores = fa.pair_scores(frags)
            pairs = fa.candidate_pairs(frags, scores)
            by_id = {f["id"]: f for f in frags}
            prompts = [fa.build_prompt(by_id, pairs[i:i + fa.LLM_BATCH], scores)
                       for i in range(0, len(pairs), fa.LLM_BATCH)]
            # 语义指纹映射回原名以对齐比较
            inv = {v: k for k, v in rename.items()}
            aliased = [tuple(inv.get(x, x) for x in p) for p in pairs]
            norm = [tuple(sorted(p)) for p in aliased]
            return norm, prompts

    def test_upload_renaming_does_not_change_judging_sequence(self):
        natural_norm, natural_prompts = self._pairs_and_prompt({})
        # 完全逆转命名（模拟逆序上传）：名字变了，内容没变
        names = sorted({*BUDGET_ROWS, *VOLUNTEER_ROWS})
        reversed_rename = {n: f"fragment-99-{len(names) - i:02d}"
                           for i, n in enumerate(sorted(names, reverse=True))}
        reversed_norm, reversed_prompts = self._pairs_and_prompt(reversed_rename)
        self.assertEqual(len(natural_norm), len(reversed_norm),
                         "候选对数量必须与上传顺序无关（选择决胜泄露会改数量）")
        self.assertEqual(natural_norm, reversed_norm,
                         "判读对序列必须与上传顺序无关（内容指纹规范序）")
        # 提示词含 ID 文本，逐字节比较需去 ID——改为比较去 ID 后的规范化形态
        strip = lambda ls: [re.sub(r"fragment-[a-z0-9\-]+", "F", p) for p in ls]
        self.assertEqual(strip(natural_prompts), strip(reversed_prompts),
                         "提示词内容（除 ID 外）必须与上传顺序无关")
