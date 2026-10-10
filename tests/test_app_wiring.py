"""app.run() lifecycle against a fake `termuxgui` (no Android): which page loading
path is used, that the loopback server is up before navigation and gone after exit,
and that failures are reported with the right exit codes."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from unittest import mock

from python_studio import app, monaco_assets
from python_studio.files import Document


def make_fake_tg(events_script, nav_methods=("loaduri",)):
    tg = types.ModuleType("termuxgui")
    rec = {"setdata": [], "nav": [], "js": [], "finished": 0, "totermux": 0, "fetched": None}

    class Event:
        webviewConsoleMessage = "webviewConsoleMessage"
        back = "back"
        webviewDestroyed = "webviewDestroyed"
        destroy = "destroy"

    class View:
        MATCH_PARENT = -1

    class Ev:
        def __init__(self, t, v=None):
            self.type, self.value = t, v

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def totermux(self):
            rec["totermux"] += 1
        def events(self):
            return events_script(rec, Ev)

    class Activity:
        def __init__(self, c, intercept=False):
            pass
        def setinputmode(self, m):
            pass
        def finish(self):
            rec["finished"] += 1

    class WebView:
        def __init__(self, a):
            pass
        def setheight(self, h): pass
        def setwidth(self, w): pass
        def allowjavascript(self, b): pass
        def evaluatejs(self, code): rec["js"].append(code)
        def setdata(self, d): rec["setdata"].append(d)

    for name in nav_methods:
        setattr(WebView, name, lambda self, url, _n=name: rec["nav"].append((_n, url)))

    tg.Event, tg.View, tg.Connection, tg.Activity, tg.WebView = Event, View, Connection, Activity, WebView
    return tg, rec


def msg(op, **kw):
    d = {"op": op}
    d.update(kw)
    return {"msg": "PSBRIDGE:" + json.dumps(d)}


class AppWiringTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = os.path.join(self._td.name, "p.py")
        with open(self.path, "w") as fh:
            fh.write("print('hi')\n")
        self.monaco = os.path.join(self._td.name, "monaco")
        for rel, data in (("vs/loader.js", "L;"), ("vs/editor/editor.main.js", "M;"),
                          ("vs/editor/editor.main.css", "a{}"), ("vs/basic-languages/python/python.js", "P;")):
            full = os.path.join(self.monaco, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(data)
        env = mock.patch.dict(os.environ, {monaco_assets.ENV_VAR: self.monaco})
        env.start()
        self.addCleanup(env.stop)

    def run_app(self, tg, **kw):
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"termuxgui": tg}), contextlib.redirect_stderr(err):
            rc = app.run(Document.open(self.path), **kw)
        return rc, err.getvalue()

    def test_monaco_uses_only_setdata_and_evaluatejs_and_opens_no_socket(self):
        def script(rec, Ev):
            yield Ev("webviewConsoleMessage", msg("ready"))
            yield Ev("webviewConsoleMessage", msg("exit"))
            yield Ev("destroy", {"finishing": True})

        tg, rec = make_fake_tg(script, nav_methods=("loaduri",))   # even if loaduri exists it must not be used
        with mock.patch("socket.socket.bind", side_effect=AssertionError("no listening socket allowed")), \
             mock.patch("socket.socket.listen", side_effect=AssertionError("no listening socket allowed")):
            rc, err = self.run_app(tg)
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["nav"], [])                           # no URL navigation (cleartext-blocked on Android)
        self.assertEqual(len(rec["setdata"]), 1)
        page = urllib.parse.unquote(rec["setdata"][0])
        self.assertIn("monaco.editor.create", page)
        self.assertNotIn("http://", page)
        kinds = [j.split("(")[0] for j in rec["js"]]
        self.assertEqual(kinds[0], "PS.assetsBegin")               # Monaco first,
        self.assertEqual(kinds[kinds.index("PS.assetsEnd") + 1], "PS.load")   # then the document
        self.assertFalse(any("PS.notice" in j for j in rec["js"]))
        self.assertEqual((rec["finished"], rec["totermux"]), (1, 1))

    def test_save_text_from_bridge_written_and_webview_kill_reported(self):
        def script(rec, Ev):
            yield Ev("webviewConsoleMessage", msg("ready"))
            yield Ev("webviewConsoleMessage", msg("save", text="x = 1\n"))
            yield Ev("webviewDestroyed")

        tg, rec = make_fake_tg(script)
        rc, err = self.run_app(tg)
        self.assertEqual(rc, 4)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "x = 1\n")
        self.assertIn("killed the WebView", err)

    def test_run_executes_the_text_sent_by_the_page(self):
        outputs = []

        def script(rec, Ev):
            yield Ev("webviewConsoleMessage", msg("ready"))
            yield Ev("webviewConsoleMessage", msg("run", text="print('from-model')\n"))
            import time
            for _ in range(100):
                if any("runEnded" in j for j in rec["js"]):
                    break
                time.sleep(0.05)
            outputs.extend(rec["js"])
            yield Ev("webviewConsoleMessage", msg("exit"))
            yield Ev("destroy", {"finishing": True})

        tg, rec = make_fake_tg(script)
        rc, err = self.run_app(tg)
        self.assertEqual(rc, 0, err)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "print('from-model')\n")      # save-before-run wrote the model text
        joined = "\n".join(outputs)
        self.assertIn("from-model", joined)
        self.assertIn("PS.runEnded(0", joined)

    def test_basic_editor_uses_setdata_no_assets_and_shows_notice(self):
        def script(rec, Ev):
            yield Ev("webviewConsoleMessage", msg("ready"))
            yield Ev("webviewConsoleMessage", msg("exit"))
            yield Ev("destroy", {"finishing": True})

        tg, rec = make_fake_tg(script)
        rc, err = self.run_app(tg, basic_editor=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(rec["setdata"]), 1)
        self.assertFalse(any("assetsBegin" in j for j in rec["js"]))
        self.assertTrue(any("PS.notice" in j and "NOT Monaco" in j for j in rec["js"]))

    def test_missing_assets_exit_3_with_instructions_before_touching_gui(self):
        os.environ[monaco_assets.ENV_VAR] = os.path.join(self._td.name, "absent")
        with mock.patch.object(monaco_assets, "user_dir", return_value=os.path.join(self._td.name, "u")), \
             mock.patch.object(monaco_assets, "bundled_dir", return_value=os.path.join(self._td.name, "b")):
            tg, rec = make_fake_tg(lambda r, E: iter(()))
            rc, err = self.run_app(tg)
        self.assertEqual(rc, 3)
        self.assertIn("--fetch-monaco", err)
        self.assertIn("--basic-editor", err)
        self.assertEqual(rec["setdata"], [])

    def test_unreadable_bundle_exit_3(self):
        os.unlink(os.path.join(self.monaco, "vs", "editor", "editor.main.css"))
        # still "found"? css is required, so find_assets rejects it
        tg, rec = make_fake_tg(lambda r, E: iter(()))
        rc, err = self.run_app(tg)
        self.assertEqual(rc, 3)

    def test_termuxgui_missing_message(self):
        with mock.patch.dict(sys.modules, {"termuxgui": None}), contextlib.redirect_stderr(io.StringIO()) as e:
            rc = app.run(Document.open(self.path))
        self.assertEqual(rc, 2)
        self.assertIn("pip install termuxgui", e.getvalue())

    def test_watchdog_message_when_page_never_reports_ready(self):
        def script(rec, Ev):
            import time
            time.sleep(0.6)
            yield Ev("destroy", {"finishing": True})

        tg, rec = make_fake_tg(script)
        with mock.patch.object(app, "READY_TIMEOUT", 0.2):
            rc, err = self.run_app(tg)
        self.assertIn("did not start", err)
        self.assertIn("allow Javascript", err)
        self.assertGreaterEqual(rec["finished"], 1)


class CliFlagsTest(unittest.TestCase):
    def test_basic_editor_flag_and_default(self):
        from python_studio.cli import main
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.py")
            with open(p, "w") as fh:
                fh.write("")
            calls = []
            main([p], run_gui=lambda doc, **kw: calls.append(kw) or 0)
            main(["--basic-editor", p], run_gui=lambda doc, **kw: calls.append(kw) or 0)
        self.assertEqual(calls, [{}, {"basic_editor": True}])

    def test_file_still_required(self):
        from python_studio.cli import main
        with contextlib.redirect_stderr(io.StringIO()) as e, self.assertRaises(SystemExit) as cm:
            main([], run_gui=lambda d: 0)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("file", e.getvalue())

    def test_fetch_monaco_flag_calls_fetcher_without_a_file(self):
        from python_studio import cli
        with mock.patch("python_studio.monaco_assets.fetch", side_effect=monaco_assets.AssetError("offline")) as f, \
             contextlib.redirect_stderr(io.StringIO()) as e:
            rc = cli.main(["--fetch-monaco"], run_gui=lambda d: 0)
        self.assertEqual(rc, 1)
        self.assertTrue(f.called)
        self.assertIn("offline", e.getvalue())


if __name__ == "__main__":
    unittest.main()
