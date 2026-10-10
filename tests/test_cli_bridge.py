import json
import os
import tempfile
import threading
import time
import unittest

from python_studio.bridge import BRIDGE_PREFIX, Controller, js_call, parse_bridge_message
from python_studio.cli import main
from python_studio.files import Document


def bridge(obj):
    return BRIDGE_PREFIX + json.dumps(obj)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.old = os.getcwd()
        os.chdir(self._td.name)
        self.addCleanup(os.chdir, self.old)

    def test_relative_path_resolved_against_cwd(self):
        open("hello.py", "w").write("print(1)\n")
        seen = []
        rc = main(["hello.py"], run_gui=lambda d: seen.append(d) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(os.path.realpath(seen[0].path), os.path.realpath("hello.py"))
        self.assertEqual(seen[0].text, "print(1)\n")

    def test_filename_with_spaces(self):
        open("my script.py", "w").write("x = 1\n")
        seen = []
        main(["my script.py"], run_gui=lambda d: seen.append(d) or 0)
        self.assertEqual(seen[0].name, "my script.py")

    def test_missing_file_declined(self):
        seen = []
        rc = main(["nope.py"], run_gui=lambda d: seen.append(d) or 0, confirm=lambda p: False)
        self.assertEqual(rc, 1)
        self.assertEqual(seen, [])
        self.assertFalse(os.path.exists("nope.py"))

    def test_missing_file_accepted_not_created_until_save(self):
        seen = []
        rc = main(["new.py"], run_gui=lambda d: seen.append(d) or 0, confirm=lambda p: True)
        self.assertEqual(rc, 0)
        self.assertFalse(seen[0].exists)
        self.assertFalse(os.path.exists("new.py"))

    def test_directory_and_bad_file_errors(self):
        os.mkdir("d")
        open("bad.py", "wb").write(b"\xff\xfe")
        self.assertEqual(main(["d"], run_gui=lambda d: 0, confirm=lambda p: True), 1)
        self.assertEqual(main(["bad.py"], run_gui=lambda d: 0), 1)

    def test_exit_code_passthrough(self):
        open("a.py", "w").write("")
        self.assertEqual(main(["a.py"], run_gui=lambda d: 3), 3)


class BridgeParseTest(unittest.TestCase):
    def test_accepts_known_ops(self):
        self.assertEqual(parse_bridge_message(bridge({"op": "ready"}))["op"], "ready")
        self.assertEqual(parse_bridge_message(bridge({"op": "save", "text": "x"}))["text"], "x")

    def test_rejects_unknown_and_malformed(self):
        for raw in (
            bridge({"op": "shell", "cmd": "id"}),
            bridge({"op": "run"}),                      # run needs text
            bridge({"op": "save", "text": 5}),
            bridge({"op": "save", "text": "x", "then_exit": "yes"}),
            bridge(["op", "save"]),
            BRIDGE_PREFIX + "{not json",
            "just a normal console.log line",
            None,
            42,
        ):
            self.assertIsNone(parse_bridge_message(raw), raw)

    def test_rejects_oversized_text(self):
        self.assertIsNone(parse_bridge_message(bridge({"op": "save", "text": "a" * (2 * 1024 * 1024 + 1)})))

    def test_js_call_is_ascii_and_escaped(self):
        code = js_call("out", "out", 'he said "hi"\n</script>\u2028 سلام')
        code.encode("ascii")
        self.assertNotIn("\n", code)
        payload = code[len("PS.out("):-2]
        self.assertEqual(json.loads("[" + payload + "]"), ["out", 'he said "hi"\n</script>\u2028 سلام'])


class SetDataEncodingTest(unittest.TestCase):
    def test_html_survives_loaddata_url_decoding(self):
        from urllib.parse import unquote
        from python_studio.app import encode_for_setdata, load_editor_html
        html = load_editor_html()
        self.assertIn("#", html)
        self.assertIn("%", html)  # the characters that broke raw loadData
        enc = encode_for_setdata(html)
        self.assertNotIn("#", enc)
        self.assertNotIn(" ", enc)
        enc.encode("ascii")
        self.assertEqual(unquote(enc), html)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = os.path.join(self._td.name, "prog.py")
        open(self.path, "w", encoding="utf-8").write("print('old')\n")
        self.js = []
        self.finished = []
        self.ctl = Controller(
            Document.open(self.path),
            eval_js=self.js.append,
            finish=lambda: self.finished.append(True),
        )
        self.addCleanup(self.ctl.shutdown)

    def wait_for(self, needle, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.ctl.pump()
            if any(needle in c for c in self.js):
                return
            time.sleep(0.02)
        self.fail(f"never saw {needle!r} in {self.js}")

    def test_ready_sends_document(self):
        self.ctl.handle(bridge({"op": "ready"}))
        self.assertIn("PS.load(", self.js[0])
        self.assertIn("prog.py", self.js[0])

    def test_save_writes_file(self):
        self.ctl.handle(bridge({"op": "save", "text": "print('new')\n"}))
        self.assertEqual(open(self.path).read(), "print('new')\n")
        self.assertIn("PS.saved(true", self.js[-1])

    def test_run_saves_first_then_runs_current_text(self):
        self.ctl.handle(bridge({"op": "run", "text": "print('fresh')\nimport sys\nprint('err', file=sys.stderr)\n"}))
        self.wait_for("PS.runEnded")
        self.assertEqual(open(self.path).read().splitlines()[0], "print('fresh')")
        joined = "\n".join(self.js)
        self.assertLess(joined.index("PS.runStarted"), joined.index("PS.out("))
        import json as _json
        streams = {"out": "", "err": ""}   # output may arrive in several chunks: compare the concatenation
        for line in self.js:
            if line.startswith("PS.out("):
                kind, payload = _json.loads("[" + line[len("PS.out("):-2] + "]")
                streams[kind] += payload
        self.assertEqual(streams, {"out": "fresh\n", "err": "err\n"})
        self.assertIn('PS.runEnded(0, "exit code 0")', joined)

    def test_run_shows_traceback_and_status(self):
        self.ctl.handle(bridge({"op": "run", "text": "raise RuntimeError('bad')\n"}))
        self.wait_for("PS.runEnded")
        joined = "\n".join(self.js)
        self.assertIn("RuntimeError: bad", joined)
        self.assertIn('PS.runEnded(1, "exit code 1")', joined)

    def test_run_not_started_if_save_fails(self):
        os.chmod(self._td.name, 0o500)
        try:
            if os.access(self._td.name, os.W_OK):
                self.skipTest("privileged user ignores directory permissions")
            self.ctl.handle(bridge({"op": "run", "text": "print('x')\n"}))
        finally:
            os.chmod(self._td.name, 0o700)
        self.assertIn("PS.saved(false", self.js[-1])
        self.assertFalse(any("runStarted" in c for c in self.js))
        self.assertEqual(open(self.path).read(), "print('old')\n")

    def test_stop(self):
        self.ctl.handle(bridge({"op": "run", "text": "import time\nprint('go', flush=True)\ntime.sleep(60)\n"}))
        self.wait_for('"go\\n"')
        self.ctl.handle(bridge({"op": "stop"}))
        self.wait_for("PS.runEnded")
        self.assertIn("SIGTERM", "\n".join(self.js))

    def test_second_run_while_running_rejected(self):
        self.ctl.handle(bridge({"op": "run", "text": "import time\ntime.sleep(30)\n"}))
        self.ctl.handle(bridge({"op": "run", "text": "print(1)\n"}))
        self.assertTrue(any("already running" in c for c in self.js))

    def test_save_then_exit_finishes(self):
        self.ctl.handle(bridge({"op": "save", "text": "a=1\n", "then_exit": True}))
        self.assertEqual(self.finished, [True])

    def test_exit_stops_running_program(self):
        self.ctl.handle(bridge({"op": "run", "text": "import time\ntime.sleep(60)\n"}))
        self.ctl.handle(bridge({"op": "exit"}))
        self.assertEqual(self.finished, [True])
        self.assertTrue(self.ctl.runner.wait(10))

    def test_invalid_message_reported_not_executed(self):
        self.ctl.handle(bridge({"op": "shell", "cmd": "touch /tmp/pwned_by_studio"}))
        self.assertTrue(any("invalid bridge message" in c for c in self.js))
        self.assertFalse(os.path.exists("/tmp/pwned_by_studio"))

    def test_plain_console_output_ignored(self):
        self.ctl.handle("some page console.log")
        self.assertEqual(self.js, [])

    def test_output_limit_reaches_gui_bounded(self):
        ctl = Controller(Document.open(self.path), eval_js=self.js.append, finish=lambda: None,
                         max_output_bytes=3000)
        self.addCleanup(ctl.shutdown)
        ctl.handle(bridge({"op": "run", "text": "print('z' * 50000)\n"}))
        end = time.time() + 15
        while time.time() < end and not any("runEnded" in c for c in self.js):
            ctl.pump()
            time.sleep(0.02)
        sent = sum(len(c) for c in self.js)
        self.assertLess(sent, 10000)
        self.assertTrue(any("output truncated" in c for c in self.js))


if __name__ == "__main__":
    unittest.main()
