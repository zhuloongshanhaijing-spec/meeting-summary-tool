"""Recovery-console contracts: a fresh clone can serve, but compile remains
locally gated until dependencies and a multi-meeting order are confirmed."""
from __future__ import annotations

import json
import os
import time
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "webapp"))
sys.path.insert(0, str(WS))
import server  # noqa: E402
import config  # noqa: E402


class CandidatePlanTest(unittest.TestCase):
    def test_completed_runs_survive_input_cleanup_and_can_be_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            for name, stamp in (("later", 200), ("earlier", 100)):
                d = input_dir / name
                d.mkdir(parents=True)
                f = d / "talk.m4a"
                f.write_bytes(b"x")
                f.touch()
                import os
                os.utime(f, (stamp, stamp))
                run = root / "runs" / name
                run.mkdir(parents=True)
                (run / "literal_records.jsonl").write_text('{"text":"local"}\n', encoding="utf-8")
                (run / "manifest.json").write_text('{"file_count": 1}', encoding="utf-8")
                # normal successful cleanup removes every input event dir
                import shutil
                shutil.rmtree(d)
            with mock.patch.object(server, "INPUT_DIR", input_dir), \
                 mock.patch.object(server, "RUNS_DIR", root / "runs"), \
                 mock.patch.object(server, "PROGRESS_JSONL", root / "runs" / "progress.jsonl"), \
                 mock.patch.object(server, "DEPENDENCY_PLAN", input_dir / ".meeting-plan.json"):
                (root / "runs" / "progress.jsonl").write_text(
                    '{"kind":"event_start","event":"later","ts":200}\n'
                    '{"kind":"event_start","event":"earlier","ts":100}\n', encoding="utf-8")
                plan = server.candidate_plan()
                self.assertEqual([x["event"] for x in plan["candidates"]], ["earlier", "later"])
                self.assertTrue(all(x["independent"] for x in plan["candidates"]))
                self.assertEqual(list(input_dir.iterdir()), [])
                self.assertTrue(plan["ready_to_confirm"])
                self.assertFalse(plan["confirmed"])
                code, _ = server.confirm_candidate_plan(["earlier", "later"])
                self.assertEqual(code, 200)
                self.assertTrue(server.candidate_plan()["confirmed"])

    def test_fragment_plan_keeps_unrelated_and_gap_fragments_separate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            rows = {
                "fragment-a1": ("预算讨论今天开始", "下一步审批预算方案"),
                "fragment-a3": ("最终表决已经完成", "会议结束"),  # missing middle: no false edge
                "fragment-z-start": ("社团活动安排", "活动安排需要志愿者"),
                "fragment-a-end": ("需要志愿者报名", "报名周五截止"),
                "fragment-x": ("数学作业问题", "下周考试复习"),
            }
            for name, (head, tail) in rows.items():
                d = runs / name / "source"; d.mkdir(parents=True)
                (d / (name + ".m4a")).write_bytes(b"x")
                (d.parent / "literal_records.jsonl").write_text(
                    '{"clean_literal": "%s", "end_seconds": 12}\n{"clean_literal": "%s", "end_seconds": 28}\n' % (head, tail), encoding="utf-8")
            with mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "FRAGMENT_PLAN", runs / ".fragment-reading-order.json"):
                refs = runs / ".fragment-references"; refs.mkdir()
                (refs / "agenda.md").write_text("志愿者报名是参考，不补写内容", encoding="utf-8")
                plan = server.fragment_reading_plan()
            grouped = [set(f["id"] for f in g["fragments"]) for g in plan["groups"]]
            self.assertIn({"fragment-z-start", "fragment-a-end"}, grouped)
            pair = next(g for g in plan["groups"] if len(g["fragments"]) == 2)
            self.assertEqual([f["id"] for f in pair["fragments"]], ["fragment-z-start", "fragment-a-end"])
            self.assertEqual(plan["references"], ["agenda.md"])
            self.assertIn({"fragment-a1"}, grouped)
            self.assertIn({"fragment-a3"}, grouped)
            self.assertIn({"fragment-x"}, grouped)

    def test_note_hint_orders_unlinked_groups_without_creating_edge(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            for name, text in (("fragment-a", "数学复习完成"), ("fragment-z", "预算审批完成")):
                d = runs / name / "source"; d.mkdir(parents=True)
                (d / (name + ".m4a")).write_bytes(b"x")
                (d.parent / "literal_records.jsonl").write_text('{"clean_literal":"%s","end_seconds":10}\n' % text, encoding="utf-8")
            refs = runs / ".fragment-references"; refs.mkdir()
            (refs / "order.md").write_text("预算审批完成\n数学复习完成\n", encoding="utf-8")
            with mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "FRAGMENT_PLAN", runs / ".fragment-reading-order.json"):
                plan = server.fragment_reading_plan()
            self.assertEqual(plan["edges"], [])
            self.assertEqual([g["fragments"][0]["id"] for g in plan["groups"]], ["fragment-z", "fragment-a"])
            self.assertEqual(plan["groups"][0]["confidence"], "low")
            self.assertGreater(plan["groups"][0]["fragments"][0]["note_hint_score"], 0)


    def test_fragment_runs_are_not_meeting_candidates(self):
        """Fragment events belong to the assembly workbench; they must never
        pollute the whole-meeting candidate list (and trip its confirm gate)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            runs = root / "runs"
            for name in ("fragment-1000-1", "meeting-a"):
                d = input_dir / name
                d.mkdir(parents=True)
                f = d / "clip.m4a"
                f.write_bytes(b"x")
                run = runs / name
                run.mkdir(parents=True)
                (run / "literal_records.jsonl").write_text('{"text":"x"}\n', encoding="utf-8")
                (run / "manifest.json").write_text('{"file_count": 1}', encoding="utf-8")
            with mock.patch.object(server, "INPUT_DIR", input_dir), \
                 mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "PROGRESS_JSONL", root / "runs" / "progress.jsonl"), \
                 mock.patch.object(server, "DEPENDENCY_PLAN", input_dir / ".meeting-plan.json"):
                plan = server.candidate_plan()
                self.assertEqual([x["event"] for x in plan["candidates"]], ["meeting-a"])


class DependencyStatusTest(unittest.TestCase):
    def setUp(self):
        # 依赖状态带 20s TTL 进程内缓存（探测是秒级开销，/api/status 每秒轮询
        # 不能每次冷启动子进程）；单测需要每次都真实探测，先清缓存。
        server._dep_cache.update({"ts": 0.0, "payload": None, "dirty": False})

    def test_status_has_local_only_purpose_required_and_official_link(self):
        payload = server.dependency_status()
        self.assertTrue(payload["local_only"])
        self.assertEqual([x["id"] for x in payload["items"]], list(server.DEPENDENCY_IDS))
        for item in payload["items"]:
            self.assertTrue(item["purpose"])
            self.assertTrue(item["official_url"].startswith("https://"))
            self.assertIsInstance(item["required"], bool)

    def test_items_carry_detail_and_consequence_for_honest_ui(self):
        for item in server.dependency_status()["items"]:
            self.assertTrue(item["detail"], item["id"])
            self.assertTrue(item["consequence"], item["id"])
        # qwen 是中文增强件而非「仅录屏可选」——文案口径不得再误导
        qwen = next(x for x in server.dependency_status()["items"] if x["id"] == "qwen")
        self.assertIn("中文", qwen["purpose"])
        self.assertFalse(qwen["required"])

    def test_whisper_model_and_ollama_tag_are_required_for_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "whisper"; binary.write_text("x"); binary.chmod(0o755)
            model = root / "model.gguf"; model.write_bytes(b"model")
            cfg = {"whisper_bin": str(binary), "whisper_model": str(model),
                   "ollama_url": "http://local", "ollama_model": "qwen3:8b"}
            response = mock.MagicMock()
            response.read.return_value = b'{"models": [{"name": "qwen3:8b"}]}'
            response.__enter__.return_value = response
            with mock.patch.object(config, "resolve", return_value=cfg), \
                 mock.patch.object(server.urllib.request, "urlopen", return_value=response):
                ready = {x["id"]: x["ready"] for x in server.dependency_status(force=True)["items"]}
            self.assertTrue(ready["whisper"])
            self.assertTrue(ready["ollama"])
            model.unlink()
            response.read.return_value = b'{"models": []}'
            with mock.patch.object(config, "resolve", return_value=cfg), \
                 mock.patch.object(server.urllib.request, "urlopen", return_value=response):
                missing = {x["id"]: x["ready"] for x in server.dependency_status(force=True)["items"]}
            self.assertFalse(missing["whisper"])
            self.assertFalse(missing["ollama"])

    def test_qwen_requires_packages_and_local_model_cache_probe(self):
        with tempfile.TemporaryDirectory() as td:
            qwen = Path(td) / "qwen-python"; qwen.write_text("x"); qwen.chmod(0o755)
            cfg = {"qwen_python": str(qwen)}
            failed = mock.Mock(returncode=1)
            passed = mock.Mock(returncode=0)
            with mock.patch.object(config, "resolve", return_value=cfg), \
                 mock.patch.object(server.subprocess, "run", return_value=failed):
                states = {x["id"]: x["ready"] for x in server.dependency_status(force=True)["items"]}
            self.assertFalse(states["qwen"])
            with mock.patch.object(config, "resolve", return_value=cfg), \
                 mock.patch.object(server.subprocess, "run", return_value=passed) as probe:
                states = {x["id"]: x["ready"] for x in server.dependency_status(force=True)["items"]}
            self.assertTrue(states["qwen"])
            self.assertIn("local_files_only=True", probe.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()


class AssemblyPlanTest(unittest.TestCase):
    """A2 wiring: v2 plan state, build gate, and confirm sidecar."""

    def _seed_plan(self, root):
        runs = root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        plan = {
            "kind": "fragment_assembly_plan", "status": "draft", "generated_ts": 1.0,
            "fragments": [{"id": "fragment-1", "source": "a.m4a"}],
            "groups": [{"group_id": "g01", "fragment_ids": ["fragment-1"],
                        "confidence": "high"}],
            "junctions": [], "notice": "n",
        }
        (runs / ".assembly-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return runs

    def test_absent_plan_reports_honest_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(server, "RUNS_DIR", root / "runs"):
                payload = server.assembly_plan()
        self.assertEqual(payload["status"], "absent")
        self.assertFalse(payload.get("confirmed", False))

    def test_confirm_writes_sidecar_and_reflects_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = self._seed_plan(root)
            with mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "ROOT", root):
                code, payload = server.confirm_assembly(["fragment-1"])
                self.assertEqual(code, 200)
                state = server.assembly_plan()
                self.assertTrue(state["confirmed"])
                # mismatch → 400 and state unchanged
                code2, _ = server.confirm_assembly(["fragment-2"])
                self.assertEqual(code2, 400)
                self.assertTrue(server.assembly_plan()["confirmed"])

    def test_build_refuses_without_fragment_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            runs.mkdir(parents=True)
            with mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "ROOT", root):
                code, payload = server.build_assembly()
        self.assertEqual(code, 409)
        self.assertIn("fragment", payload["error"])

    def test_build_passes_ollama_model_from_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            (runs / "fragment-x").mkdir(parents=True)
            captured = {}
            with mock.patch.object(server, "RUNS_DIR", runs), \
                 mock.patch.object(server, "ROOT", root), \
                 mock.patch.object(server.subprocess, "Popen",
                                   wraps=server.subprocess.Popen) as popen:
                def capture(cmd, **kw):
                    captured["cmd"] = cmd
                    return mock.Mock(poll=lambda: None)
                popen.side_effect = capture
                old = os.environ.pop("MST_OLLAMA_MODEL", None)
                try:
                    code, _ = server.build_assembly()
                    self.assertEqual(code, 202)
                    self.assertNotIn("--ollama-model", captured["cmd"])
                    os.environ["MST_OLLAMA_MODEL"] = "qwen3-vl:4b-instruct"
                    code, _ = server.build_assembly()  # 409: already building
                    # 仍需验证 env 生效路径：重置 proc 后再来一次
                    server._assembly["proc"] = None
                    code, _ = server.build_assembly()
                    self.assertEqual(code, 202)
                    self.assertIn("--ollama-model", captured["cmd"])
                    self.assertEqual(
                        captured["cmd"][captured["cmd"].index("--ollama-model") + 1],
                        "qwen3-vl:4b-instruct")
                finally:
                    server._assembly["proc"] = None   # 不污染后续用例的全局构建态
                    if old is None:
                        os.environ.pop("MST_OLLAMA_MODEL", None)
                    else:
                        os.environ["MST_OLLAMA_MODEL"] = old


class AsyncDepSummaryTest(unittest.TestCase):
    """P0 回归：/api/status 绝不为依赖探测阻塞（冷读返回探测中占位）。"""

    def test_cold_read_is_instant_and_probing(self):
        server._dep_cache.update({"ts": 0.0, "payload": None, "dirty": False})
        t0 = time.time()
        d = server.dependency_summary_cached()
        self.assertLess(time.time() - t0, 0.5)
        self.assertTrue(d["probing"])
        self.assertEqual(d["items"], [])

    def test_warm_read_serves_cache_without_probing(self):
        server._dep_cache.update({
            "ts": time.time(),
            "payload": {"items": [{"id": "ffmpeg", "ready": True}], "local_only": True},
            "dirty": False})
        d = server.dependency_summary_cached()
        self.assertNotIn("probing", d)
        self.assertEqual(d["items"][0]["id"], "ffmpeg")
        server._dep_cache.update({"ts": 0.0, "payload": None, "dirty": False})
