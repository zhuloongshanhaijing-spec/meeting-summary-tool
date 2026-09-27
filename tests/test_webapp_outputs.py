"""HTTP-level tests for the /api/outputs/* browsing routes (webapp/server.py).

Covers the output-package contract consumed by webapp/static/results.js:
raw file serving with traversal rejection (encoded and unencoded), file
listing shape/order, and whole-package zip download.

Reuses PatchedWorkspace from tests/test_webapp_server.py to redirect every
module-level path constant of the server into a temp workspace.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "webapp"))

from tests.test_webapp_server import PatchedWorkspace  # noqa: E402

import server as webapp  # noqa: E402

EVENT_ZH = "讲座一"     # 中文事件名（走 URL 编码路径）
EVENT_EN = "standup"    # 纯 ASCII 事件名（可发未编码的穿越串）

REPORT_MD = "# 报告\n\n正文包含 <tag>、& 与 \"引号\"。\n"
DB_BYTES = b"SQLite fake \x00\x01\x02"
EVIDENCE_MD = "佐证"
AGENDA_MD = "secret agenda"


class OutputsRouteTest(unittest.TestCase):
    """真实 ThreadingHTTPServer + urllib 实测 HTTP 层。"""

    def setUp(self):
        self.pw = PatchedWorkspace()
        self.pw.__enter__()
        self.addCleanup(self._teardown_fixture)
        httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.ConsoleHandler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpd = httpd
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self._make_fixture_outputs()

    def _teardown_fixture(self):
        self.httpd.server_close()
        self.pw.__exit__(None, None, None)

    def _make_fixture_outputs(self):
        ev1 = self.pw.root / "outputs" / EVENT_ZH
        ev1.mkdir(parents=True)
        (ev1 / "04_会议报告.md").write_text(REPORT_MD, encoding="utf-8")
        (ev1 / "meeting.db").write_bytes(DB_BYTES)
        (ev1 / "evidence").mkdir()
        (ev1 / "evidence" / "06_笔记佐证与冲突.md").write_text(EVIDENCE_MD,
                                                                encoding="utf-8")
        ev2 = self.pw.root / "outputs" / EVENT_EN
        ev2.mkdir()
        (ev2 / "00_使用说明.md").write_text("usage note", encoding="utf-8")
        (ev2 / "agenda.md").write_text(AGENDA_MD, encoding="utf-8")

    # -- helpers -----------------------------------------------------------

    def _get(self, path_and_query: str):
        """GET that returns (status, headers, body) even for 4xx/5xx."""
        url = self.base + path_and_query
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _event_url(self, event: str, tail: str) -> str:
        return f"/api/outputs/{urllib.parse.quote(event, safe='')}/{tail}"

    def _raw_url(self, event: str, path: str, quote_path: bool = True) -> str:
        p = urllib.parse.quote(path, safe="") if quote_path else path
        return self._event_url(event, "raw") + f"?path={p}"

    # -- 1. 目录穿越：跨事件读取必须 404 ------------------------------------

    def test_raw_rejects_traversal_into_other_event(self):
        cases = [
            ("../standup/agenda.md", True),    # URL 编码（%2E%2E%2F...）
            ("../standup/agenda.md", False),   # 未编码
            ("../standup/00_使用说明.md", True),  # 编码 + 非ASCII 文件名
        ]
        for path, quoted in cases:
            with self.subTest(path=path, quoted=quoted):
                code, _, body = self._get(self._raw_url(EVENT_ZH, path, quoted))
                self.assertEqual(code, 404, f"{path!r} must be rejected")
                self.assertNotIn(b"secret", body.lower())
                self.assertNotIn(AGENDA_MD.encode("utf-8"), body)

    # -- 2. 绝对路径 / 上级目录穿越（编码与未编码）→ 404 ----------------------

    def test_raw_rejects_absolute_and_parent_traversal(self):
        cases = [
            ("/etc/passwd", True),
            ("/etc/passwd", False),
            ("../../etc/passwd", True),
            ("../../etc/passwd", False),
            ("..", True),
            ("../", False),
        ]
        for path, quoted in cases:
            with self.subTest(path=path, quoted=quoted):
                code, _, body = self._get(self._raw_url(EVENT_ZH, path, quoted))
                self.assertEqual(code, 404, f"{path!r} must be rejected")
                self.assertNotIn(b"root:", body)

    # -- 3. 正常 raw：200 且内容一致 -----------------------------------------

    def test_raw_serves_file_content_verbatim(self):
        code, headers, body = self._get(self._raw_url(EVENT_ZH, "04_会议报告.md"))
        self.assertEqual(code, 200)
        self.assertEqual(body, REPORT_MD.encode("utf-8"))
        self.assertTrue(headers.get("Content-Type", "")
                        .startswith("text/plain"))

    def test_raw_serves_nested_and_ascii_events(self):
        code, _, body = self._get(self._raw_url(EVENT_ZH,
                                                "evidence/06_笔记佐证与冲突.md"))
        self.assertEqual(code, 200)
        self.assertEqual(body.decode("utf-8"), EVIDENCE_MD)
        code, _, body = self._get(self._raw_url(EVENT_EN, "agenda.md"))
        self.assertEqual(code, 200)
        self.assertEqual(body.decode("utf-8"), AGENDA_MD)

    def test_raw_unknown_event_or_file_404(self):
        code, _, _ = self._get(self._raw_url("不存在的事件", "x.md"))
        self.assertEqual(code, 404)
        code, _, _ = self._get(self._raw_url(EVENT_ZH, "05_缺失.md"))
        self.assertEqual(code, 404)

    # -- 4. /files：{name,size} 形状，按名称排序 ----------------------------

    def test_files_listing_shape_and_sorted_by_name(self):
        code, _, body = self._get(self._event_url(EVENT_ZH, "files"))
        self.assertEqual(code, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["event"], EVENT_ZH)
        self.assertEqual(sorted(payload), ["event", "files"])
        names = [f["name"] for f in payload["files"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names, ["04_会议报告.md",
                                 "evidence/06_笔记佐证与冲突.md",
                                 "meeting.db"])
        for f in payload["files"]:
            self.assertEqual(sorted(f), ["name", "size"])
            self.assertIsInstance(f["size"], int)
        sizes = {f["name"]: f["size"] for f in payload["files"]}
        self.assertEqual(sizes["meeting.db"], len(DB_BYTES))
        self.assertEqual(sizes["04_会议报告.md"],
                         len(REPORT_MD.encode("utf-8")))

    def test_files_unknown_event_404(self):
        code, _, _ = self._get(self._event_url("no-such-event", "files"))
        self.assertEqual(code, 404)

    # -- 5. /zip：200 + application/zip + 可解析且含全部文件 -----------------
    # 中文事件名的 /zip 曾因 Content-Disposition 非 ASCII 在 http.server
    # latin-1 编码处断连（已按 RFC 5987 filename*= 修复，下有回归用例）。

    def test_zip_download_contains_all_files(self):
        code, headers, body = self._get(self._event_url(EVENT_EN, "zip"))
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"), "application/zip")
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIsNone(zf.testzip())  # CRC 全部有效
            got = sorted(zf.namelist())
            expect = sorted(["00_使用说明.md", "agenda.md"])
            self.assertEqual(got, expect)
            self.assertEqual(zf.read("00_使用说明.md").decode("utf-8"),
                             "usage note")
            self.assertEqual(zf.read("agenda.md").decode("utf-8"), AGENDA_MD)

    def test_zip_chinese_event_name_rfc5987(self):
        """回归：中文事件名 zip 不再断连；filename* 携带 UTF-8 编码名。"""
        code, headers, body = self._get(self._event_url(EVENT_ZH, "zip"))
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"), "application/zip")
        disposition = headers.get("Content-Disposition", "")
        self.assertIn("filename*=UTF-8''", disposition)
        from urllib.parse import unquote
        starred = disposition.split("filename*=UTF-8''", 1)[1]
        self.assertTrue(starred.endswith(".zip"))
        self.assertEqual(unquote(starred), f"{EVENT_ZH}.zip")
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("meeting.db", zf.namelist())

    def test_zip_unknown_event_404(self):
        code, _, _ = self._get(self._event_url("no-such-event", "zip"))
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
