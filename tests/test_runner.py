import os
import sys
import tempfile
import threading
import time
import unittest

from python_studio.runner import Runner, RunnerError


class Collect:
    def __init__(self):
        self.chunks = []
        self.exit = None
        self.done = threading.Event()
        self.lock = threading.Lock()

    def out(self, kind, text):
        with self.lock:
            self.chunks.append((kind, text))

    def on_exit(self, code, note):
        self.exit = (code, note)
        self.done.set()

    def text(self, kind):
        with self.lock:
            return "".join(t for k, t in self.chunks if k == kind)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def script(self, body, name="prog.py"):
        path = os.path.join(self._td.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def run_script(self, body, name="prog.py", timeout=15, **kw):
        c = Collect()
        r = Runner(c.out, c.on_exit, **kw)
        r.start(self.script(body, name))
        self.assertTrue(c.done.wait(timeout), "process did not finish")
        return c, r

    def test_success(self):
        c, _ = self.run_script("print('hello')\n")
        self.assertEqual(c.text("out"), "hello\n")
        self.assertEqual(c.exit[0], 0)

    def test_filename_with_spaces_and_shell_chars(self):
        c, _ = self.run_script("print('ok')\n", name="a b; echo PWNED $(id).py")
        self.assertEqual(c.text("out"), "ok\n")
        self.assertNotIn("PWNED", c.text("out"))

    def test_syntax_error(self):
        c, _ = self.run_script("def f(:\n")
        self.assertIn("SyntaxError", c.text("err"))
        self.assertEqual(c.exit[0], 1)

    def test_runtime_error_traceback(self):
        c, _ = self.run_script("print('before')\nraise ValueError('boom')\n")
        self.assertEqual(c.text("out"), "before\n")
        self.assertIn("Traceback", c.text("err"))
        self.assertIn("ValueError: boom", c.text("err"))
        self.assertEqual(c.exit[0], 1)

    def test_import_error(self):
        c, _ = self.run_script("import definitely_not_a_module_xyz\n")
        self.assertIn("ModuleNotFoundError", c.text("err"))

    def test_nonzero_exit_code(self):
        c, _ = self.run_script("import sys\nsys.exit(7)\n")
        self.assertEqual(c.exit, (7, "exit code 7"))

    def test_unicode_output(self):
        c, _ = self.run_script("print('سلام دنیا ✓ \\u00e9')\n")
        self.assertEqual(c.text("out"), "سلام دنیا ✓ é\n")

    def test_stdout_stderr_separate(self):
        c, _ = self.run_script("import sys\nprint('o')\nprint('e', file=sys.stderr)\n")
        self.assertEqual(c.text("out"), "o\n")
        self.assertEqual(c.text("err"), "e\n")

    def test_output_over_time(self):
        c = Collect()
        r = Runner(c.out, c.on_exit)
        r.start(self.script("import time\nprint('a')\ntime.sleep(0.6)\nprint('b')\n"))
        deadline = time.time() + 5
        while time.time() < deadline and "a" not in c.text("out"):
            time.sleep(0.02)
        self.assertEqual(c.text("out"), "a\n")  # seen while still running
        self.assertFalse(c.done.is_set())
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "a\nb\n")

    def test_long_running_does_not_block_and_can_stop(self):
        c = Collect()
        r = Runner(c.out, c.on_exit)
        t0 = time.time()
        r.start(self.script("import time\nprint('start')\ntime.sleep(60)\n"))
        self.assertLess(time.time() - t0, 2)  # start() returns immediately
        self.assertTrue(r.running)
        time.sleep(0.5)
        self.assertTrue(r.stop())
        self.assertTrue(c.done.wait(10))
        self.assertIn("SIGTERM", c.exit[1])
        self.assertFalse(r.running)

    def test_stop_kills_grandchildren_group_only(self):
        pidfile = os.path.join(self._td.name, "child.pid")
        body = (
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({pidfile!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        c = Collect()
        r = Runner(c.out, c.on_exit)
        r.start(self.script(body))
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(pidfile):
            time.sleep(0.05)
        time.sleep(0.2)
        child = int(open(pidfile).read())
        r.stop()
        self.assertTrue(c.done.wait(10))
        time.sleep(0.3)
        self.assertFalse(self._alive(child), "grandchild survived Stop")
        os.kill(os.getpid(), 0)  # the test process itself was not touched

    @staticmethod
    def _alive(pid):
        # a killed process whose parent is gone may linger as a zombie until
        # init reaps it; a zombie is not running
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def test_stop_when_idle(self):
        r = Runner(lambda *a: None, lambda *a: None)
        self.assertFalse(r.stop())

    def test_output_limit(self):
        c, _ = self.run_script("print('x' * 100000)\nprint('y' * 100000)\n", max_bytes=5000)
        total = c.text("out")
        self.assertLessEqual(len(total), 5000)
        self.assertEqual(c.text("note").count("[output truncated"), 1)
        self.assertEqual(c.exit[0], 0)  # program still ran to completion (no pipe deadlock)

    def test_busy_rejected(self):
        c = Collect()
        r = Runner(c.out, c.on_exit)
        r.start(self.script("import time\ntime.sleep(30)\n"))
        try:
            with self.assertRaises(RunnerError):
                r.start(self.script("print(1)\n", "other.py"))
        finally:
            r.stop()
            c.done.wait(10)

    def test_startup_failure(self):
        r = Runner(lambda *a: None, lambda *a: None, python="/nonexistent/python-xyz")
        with self.assertRaises(RunnerError):
            r.start(self.script("print(1)\n"))
        self.assertFalse(r.running)

    def test_cwd_is_script_directory(self):
        c, _ = self.run_script("import os\nprint(os.getcwd())\n")
        self.assertEqual(os.path.realpath(c.text("out").strip()), os.path.realpath(self._td.name))

    def test_stdin_is_closed(self):
        c, _ = self.run_script("import sys\nprint(repr(sys.stdin.read()))\n")
        self.assertEqual(c.text("out"), "''\n")

    def test_reusable_after_finish(self):
        c, r = self.run_script("print(1)\n")
        c.done.clear()
        r.start(self.script("print(2)\n", "two.py"))
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "1\n2\n")


if __name__ == "__main__":
    unittest.main()
