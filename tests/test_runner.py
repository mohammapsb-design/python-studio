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

    # ---- stdin ----
    def start_script(self, body, **kw):
        c = Collect()
        r = Runner(c.out, c.on_exit, **kw)
        r.start(self.script(body))
        self.addCleanup(r.terminate)
        return c, r

    def wait_text(self, c, needle, timeout=10):
        end = time.time() + timeout
        while time.time() < end and needle not in c.text("out"):
            time.sleep(0.02)
        self.assertIn(needle, c.text("out"))

    def test_input_single(self):
        c, r = self.start_script("name = input('Name: ')\nprint('Hello,', name)\n")
        self.wait_text(c, "Name: ")                       # the prompt arrives before any input
        self.assertFalse(c.done.is_set())                  # ...and the program waits for us
        self.assertTrue(r.send_input("Ali"))
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "Name: Hello, Ali\n")
        self.assertEqual(c.exit[0], 0)

    def test_input_multiple_lines_and_unicode(self):
        c, r = self.start_script("a = input()\nb = input()\nprint(b, a)\n")
        self.assertTrue(r.send_input("one"))
        self.assertTrue(r.send_input("\u0633\u0644\u0627\u0645"))
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "\u0633\u0644\u0627\u0645 one\n")

    def test_eof_ends_read_and_input_after_exit_is_rejected(self):
        c, r = self.start_script("import sys\nprint(repr(sys.stdin.read()))\n")
        self.assertTrue(r.send_input("abc", newline=False))
        self.assertTrue(r.close_input())
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "'abc'\n")
        self.assertFalse(r.send_input("late"))
        self.assertFalse(r.close_input())

    def test_input_when_idle_or_too_big_is_rejected(self):
        r = Runner(lambda *a: None, lambda *a: None)
        self.assertFalse(r.send_input("x"))
        c, r = self.start_script("import time\ntime.sleep(30)\n")
        self.assertFalse(r.send_input("x" * 70000))

    def test_send_input_never_blocks_when_program_does_not_read(self):
        c, r = self.start_script("import time\ntime.sleep(30)\n")
        t0 = time.time()
        results = [r.send_input("y" * 60000) for _ in range(200)]   # far more than a pipe holds
        self.assertLess(time.time() - t0, 2.0)                       # the GUI thread is never stuck
        self.assertIn(False, results)                                # bounded: overflow is refused
        r.stop()
        self.assertTrue(c.done.wait(10))                             # a blocked writer does not hang Stop

    def test_endless_flood_is_stopped_automatically(self):
        c, r = self.start_script("while True:\n    print('x' * 80)\n", max_bytes=1000, flood_limit=200_000)
        self.assertTrue(c.done.wait(20), "flooding program was never stopped")
        self.assertIn("SIGTERM", c.exit[1])
        self.assertIn("keeps printing", c.text("note"))
        self.assertLessEqual(len(c.text("out")), 1000)

    def test_stop_kills_children_that_ignore_sigterm_even_if_leader_died(self):
        pidfile = os.path.join(self._td.name, "child.pid")
        body = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'])\n"
            f"open({pidfile!r}, 'w').write(str(p.pid))\n"
            "time.sleep(300)\n"
        )
        c, r = self.start_script(body)
        end = time.time() + 5
        while time.time() < end and not os.path.exists(pidfile):
            time.sleep(0.05)
        time.sleep(0.3)
        child = int(open(pidfile).read())
        r.stop()
        self.assertTrue(c.done.wait(10))
        end = time.time() + 6                       # SIGKILL escalation comes after STOP_GRACE
        while time.time() < end and self._alive(child):
            time.sleep(0.1)
        self.assertFalse(self._alive(child), "child that ignores SIGTERM survived Stop")

    def test_terminate_is_synchronous_and_leaves_nothing_behind(self):
        body = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('up', flush=True)\nwhile True: time.sleep(1)\n"
        c, r = self.start_script(body)
        self.wait_text(c, "up")
        pid = r._proc.pid
        t0 = time.time()
        self.assertTrue(r.terminate(grace=0.5))
        self.assertLess(time.time() - t0, 3)
        end = time.time() + 3
        while time.time() < end and self._alive(pid):
            time.sleep(0.05)
        self.assertFalse(self._alive(pid))

    @unittest.skipUnless(os.path.exists("/bin/sh") or os.path.exists("/system/bin/sh") or os.path.exists("/usr/bin/sh"), "no sh")
    def test_optional_memory_limit(self):
        body = "try:\n    x = bytearray(900 * 1024 * 1024)\n    print('allocated')\nexcept MemoryError:\n    print('MemoryError')\n"
        c, _ = self.run_script(body, mem_limit_mb=300)
        self.assertEqual(c.text("out"), "MemoryError\n")
        c, _ = self.run_script("print('small ok')\n", mem_limit_mb=300)   # normal programs unaffected
        self.assertEqual(c.text("out"), "small ok\n")

    def test_reusable_after_finish(self):
        c, r = self.run_script("print(1)\n")
        c.done.clear()
        r.start(self.script("print(2)\n", "two.py"))
        self.assertTrue(c.done.wait(10))
        self.assertEqual(c.text("out"), "1\n2\n")


if __name__ == "__main__":
    unittest.main()
