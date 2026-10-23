import json
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
