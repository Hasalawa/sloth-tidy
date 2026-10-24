import json
import tempfile
import threading
import socket
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sloth_tidy as st

REPLY = {}  # filename -> dict returned by fake model


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["format"]["required"] == ["category", "new_name"]
        info = json.loads(body["messages"][-1]["content"])
        out = REPLY.get(info["name"], {"category": "Other", "new_name": "x"})
        data = json.dumps({"message": {"content": out if isinstance(out, str) else json.dumps(out)}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        REPLY.clear()

    def tearDown(self):
        self._t.cleanup()

    def test_ai_plan_apply_undo(self):
        (self.root / "IMG_001.txt").write_text("Invoice from ACME for 42 USD")
        (self.root / "notes.md").write_text("# meeting")
        REPLY["IMG_001.txt"] = {"category": "Receipts", "new_name": "ACME Invoice (March)!!"}
        REPLY["notes.md"] = {"category": "Documents", "new_name": "meeting-notes"}
        moves = st.plan(self.root, self.host, "m", True)
        self.assertTrue(all(m.used_ai for m in moves))
        st.apply(self.root, moves)
        self.assertTrue((self.root / "Receipts" / "acme-invoice-march.txt").exists())
        self.assertTrue((self.root / "Documents" / "meeting-notes.md").exists())
        self.assertFalse((self.root / "notes.md").exists())
        self.assertEqual(st.undo(self.root), 2)
        self.assertTrue((self.root / "IMG_001.txt").exists())
        self.assertTrue((self.root / "notes.md").exists())
        self.assertFalse((self.root / "Receipts").exists())  # empty folder cleaned

    def test_path_traversal_and_bad_category_are_neutralised(self):
        (self.root / "a.txt").write_text("hi")
        REPLY["a.txt"] = {"category": "../../etc", "new_name": "../../evil/name"}
        (m,) = st.plan(self.root, self.host, "m", True)
        self.assertEqual(m.dst.parent, self.root / "Documents")  # rule fallback category
        self.assertEqual(m.dst.name, "evil-name.txt")

    def test_collisions_get_suffixes(self):
        (self.root / "a.txt").write_text("1")
        (self.root / "b.txt").write_text("2")
        REPLY["a.txt"] = REPLY["b.txt"] = {"category": "Documents", "new_name": "same"}
        moves = st.plan(self.root, self.host, "m", True)
        self.assertEqual(sorted(m.dst.name for m in moves), ["same-1.txt", "same.txt"])
        st.apply(self.root, moves)
        self.assertEqual(len(list((self.root / "Documents").iterdir())), 2)

    def test_garbage_model_output_falls_back_for_that_file(self):
        (self.root / "pic.png").write_bytes(b"\x89PNG")
        REPLY["pic.png"] = "not json at all"
        (m,) = st.plan(self.root, self.host, "m", True)
        self.assertFalse(m.used_ai)
        self.assertEqual(m.dst, self.root / "Images" / "pic.png")

    def test_ollama_down_falls_back_to_rules(self):
        (self.root / "Screenshot 2026.png").write_bytes(b"x")
        (self.root / "song.mp3").write_bytes(b"x")
        moves = st.plan(self.root, "http://127.0.0.1:9", "m", True)
        cats = {m.src.name: m.category for m in moves}
        self.assertEqual(cats, {"Screenshot 2026.png": "Screenshots", "song.mp3": "Audio"})

    def test_dry_run_changes_nothing_and_skips_hidden_dirs_log(self):
        (self.root / "a.zip").write_bytes(b"x")
        (self.root / ".hidden").write_text("h")
        (self.root / "sub").mkdir()
        rc = st.main(["--no-ai", "tidy", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "a.zip").exists())
        self.assertEqual(len(st.plan(self.root, "", "", False)), 1)

    def test_settle_time_skips_fresh_files(self):
        (self.root / "fresh.txt").write_text("now")
        self.assertEqual(st.plan(self.root, "", "", False, min_age=3600), [])

    def test_two_runs_undo_in_order(self):
        (self.root / "a.txt").write_text("1")
        st.apply(self.root, st.plan(self.root, "", "", False))
        (self.root / "b.zip").write_bytes(b"x")
        st.apply(self.root, st.plan(self.root, "", "", False))
        st.undo(self.root)
        self.assertTrue((self.root / "b.zip").exists())
        self.assertTrue((self.root / "Documents" / "a.txt").exists())

    # ---- robustness (things that go wrong on a real Windows Downloads folder)
    def test_locked_file_is_skipped_and_everything_else_stays_undoable(self):
        for n in ("a.txt", "b.txt", "c.txt"):
            (self.root / n).write_text(n)
        moves = st.plan(self.root, "", "", False)
        real = Path.rename
        def flaky(self_, target):
            if self_.name == "b.txt":
                raise PermissionError(13, "The process cannot access the file")
            return real(self_, target)
        with mock.patch.object(Path, "rename", flaky):
            moved, failed = st.apply(self.root, moves)
        self.assertEqual(sorted(m.src.name for m in moved), ["a.txt", "c.txt"])
        self.assertEqual([f[0] for f in failed], ["b.txt"])
        self.assertTrue((self.root / "b.txt").exists())
        self.assertEqual(st.undo(self.root), 2)           # the log was written despite the failure
        self.assertTrue((self.root / "a.txt").exists() and (self.root / "c.txt").exists())

    def test_unfinished_downloads_and_system_files_are_left_alone(self):
        for n in ("movie.mp4.crdownload", "x.part", "desktop.ini", "Thumbs.db", "~$report.docx", "real.pdf"):
            (self.root / n).write_text("x")
        names = [m.src.name for m in st.plan(self.root, "", "", False)]
        self.assertEqual(names, ["real.pdf"])

    def test_windows_reserved_names_are_avoided(self):
        self.assertEqual(st.slugify("NUL", "file"), "nul-file")
        self.assertEqual(st.slugify("com1", "file"), "com1-file")
        self.assertEqual(st.slugify("console", "file"), "console")

    def test_undo_keeps_entries_it_could_not_restore(self):
        (self.root / "a.txt").write_text("1")
        st.apply(self.root, st.plan(self.root, "", "", False))
        (self.root / "a.txt").write_text("someone made a new a.txt")   # the original spot is taken
        self.assertEqual(st.undo(self.root), 0)
        (self.root / "a.txt").unlink()
        self.assertEqual(st.undo(self.root), 1)                         # still possible afterwards
        self.assertEqual((self.root / "a.txt").read_text(), "1")

    def test_one_slow_file_does_not_switch_the_model_off(self):
        (self.root / "a.txt").write_text("1"); (self.root / "b.txt").write_text("2")
        calls = []
        def fake(path, host, model, timeout=0):
            calls.append(path.name)
            if path.name == "a.txt":
                raise socket.timeout("slow")
            return {"category": "Documents", "new_name": "bee"}
        warns = []
        with mock.patch.object(st, "ask_ollama", fake):
            moves = st.plan(self.root, "h", "m", True, on_warn=warns.append)
        by = {m.src.name: m for m in moves}
        self.assertFalse(by["a.txt"].used_ai)
        self.assertTrue(by["b.txt"].used_ai)
        self.assertEqual(calls, ["a.txt", "b.txt"])
        self.assertTrue(warns and "a.txt" in warns[0])


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.ollama = f"http://127.0.0.1:{cls.fake.server_port}"
        cls.gui, cls.token = st.make_gui_server(0, cls.ollama, "m", True)
        threading.Thread(target=cls.gui.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.gui.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.gui.shutdown()
        cls.fake.shutdown()

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        REPLY.clear()

    def tearDown(self):
        self._t.cleanup()

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode())
        if token:
            req.add_header("X-Token", self.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_page_served_with_token(self):
        with urllib.request.urlopen(self.base + "/") as r:
            html = r.read().decode()
        self.assertIn(self.token, html)
        self.assertNotIn("__TOKEN__", html)
        self.assertNotIn("__DEFAULTS__", html)

    def test_api_requires_token_and_loopback_host(self):
        self.assertEqual(self.call("/api/ls?path=", token=False)[0], 403)
        self.assertEqual(self.call("/api/ls?path=", host="evil.example.com")[0], 403)

    def test_plan_apply_undo_over_http(self):
        (self.root / "scan.txt").write_text("Electricity bill")
        (self.root / "keep.zip").write_bytes(b"x")
        REPLY["scan.txt"] = {"category": "Receipts", "new_name": "Electricity Bill"}
        code, plan = self.call("/api/plan", {"folder": str(self.root), "model": "m", "host": self.ollama})
        self.assertEqual(code, 200)
        by_name = {m["name"]: m for m in plan["moves"]}
        self.assertEqual(by_name["scan.txt"]["new_name"], "electricity-bill.txt")
        self.assertTrue(by_name["scan.txt"]["used_ai"])
        self.assertTrue((self.root / "scan.txt").exists())  # planning moved nothing
        # user unticks keep.zip: only scan.txt is applied
        code, res = self.call("/api/apply", {"folder": str(self.root), "moves": [by_name["scan.txt"]]})
        self.assertEqual((code, res["moved"]), (200, 1))
        self.assertTrue((self.root / "Receipts" / "electricity-bill.txt").exists())
        self.assertTrue((self.root / "keep.zip").exists())
        code, res = self.call("/api/undo", {"folder": str(self.root)})
        self.assertEqual(res["restored"], 1)
        self.assertTrue((self.root / "scan.txt").exists())

    def test_apply_rejects_tampered_requests(self):
        (self.root / "a.txt").write_text("x")
        bad = [
            {"name": "../a.txt", "category": "Documents", "new_name": "a.txt"},
            {"name": "a.txt", "category": "../../tmp", "new_name": "a.txt"},
            {"name": "ghost.txt", "category": "Documents", "new_name": "a.txt"},
        ]
        for mv in bad:
            code, _ = self.call("/api/apply", {"folder": str(self.root), "moves": [mv]})
            self.assertEqual(code, 400, mv)
        self.assertTrue((self.root / "a.txt").exists())

    def test_refuses_non_folder_and_filesystem_root(self):
        self.assertEqual(self.call("/api/plan", {"folder": "/definitely/not/here"})[0], 400)
        self.assertEqual(self.call("/api/plan", {"folder": "/"})[0], 400)

    def test_plan_reports_ollama_down_warning(self):
        (self.root / "a.mp3").write_bytes(b"x")
        code, plan = self.call("/api/plan", {"folder": str(self.root), "host": "http://127.0.0.1:9"})
        self.assertEqual(code, 200)
        self.assertTrue(plan["warnings"])
        self.assertEqual(plan["moves"][0]["category"], "Audio")

    def test_apply_reports_files_that_could_not_move(self):
        (self.root / "a.txt").write_text("1"); (self.root / "b.txt").write_text("2")
        moves = [{"name": "a.txt", "category": "Documents", "new_name": "a.txt"},
                 {"name": "b.txt", "category": "Documents", "new_name": "b.txt"}]
        real = Path.rename
        def flaky(self_, target):
            if self_.name == "b.txt":
                raise PermissionError(13, "in use")
            return real(self_, target)
        with mock.patch.object(Path, "rename", flaky):
            code, res = self.call("/api/apply", {"folder": str(self.root), "moves": moves})
        self.assertEqual(code, 200)
        self.assertEqual(res["moved"], 1)
        self.assertEqual(res["failed"][0]["name"], "b.txt")


if __name__ == "__main__":
    unittest.main()
