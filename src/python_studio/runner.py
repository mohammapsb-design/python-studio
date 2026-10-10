"""Run a Python file in a child process without blocking the GUI.

Safety properties:
  * argument list only, never a shell
  * child runs in its own session/process group, so Stop signals only that group
  * stdin is /dev/null (the console is output-only in this release)
  * total captured output per run is bounded (MAX_OUTPUT_BYTES); once reached,
    further output is drained and discarded, and a notice is emitted once
"""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import sys
import threading
from typing import Callable, Optional

MAX_OUTPUT_BYTES = 1_000_000

OutputCb = Callable[[str, str], None]        # (kind: "out"|"err"|"note", text)
ExitCb = Callable[[Optional[int], str], None]  # (returncode, human readable note)


class RunnerError(Exception):
    pass


class Runner:
    def __init__(
        self,
        on_output: OutputCb,
        on_exit: ExitCb,
        python: Optional[str] = None,
        max_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._python = python or sys.executable
        self._max_bytes = max_bytes
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._seen = 0
        self._truncated = False
        self._monitor: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None

    def start(self, path: str) -> None:
        with self._lock:
            if self._proc is not None:
                raise RunnerError("a program is already running")
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                proc = subprocess.Popen(
                    [self._python, "-u", path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=os.path.dirname(path) or None,
                    env=env,
                    bufsize=0,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RunnerError(f"could not start {self._python}: {exc.strerror or exc}") from exc
            self._proc = proc
            self._seen = 0
            self._truncated = False
        readers = [
            threading.Thread(target=self._pump, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, "err"), daemon=True),
        ]
        for t in readers:
            t.start()
        self._monitor = threading.Thread(target=self._watch, args=(proc, readers), daemon=True)
        self._monitor.start()

    def _pump(self, stream, kind: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._emit(kind, chunk, decoder)
            tail = decoder.decode(b"", final=True)
            if tail:
                self._emit_text(kind, tail)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _emit(self, kind: str, chunk: bytes, decoder) -> None:
        with self._lock:
            room = self._max_bytes - self._seen
            self._seen += len(chunk)
            if room <= 0:
                self._note_truncation_locked()
                return
            if len(chunk) > room:
                chunk = chunk[:room]
                over = True
            else:
                over = False
        text = decoder.decode(chunk)
        if text:
            self._on_output(kind, text)
        if over:
            with self._lock:
                self._note_truncation_locked()

    def _emit_text(self, kind: str, text: str) -> None:
        with self._lock:
            if self._seen >= self._max_bytes:
                return
        self._on_output(kind, text)

    def _note_truncation_locked(self) -> None:
        if not self._truncated:
            self._truncated = True
            # callback outside lock would be cleaner, but the callbacks only enqueue
            self._on_output("note", f"\n[output truncated after {self._max_bytes} bytes]\n")

    def _watch(self, proc: subprocess.Popen, readers) -> None:
        code = proc.wait()
        for t in readers:
            t.join(timeout=5)
        with self._lock:
            self._proc = None
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = str(-code)
            note = f"terminated by signal {name}"
        else:
            note = f"exit code {code}"
        self._on_exit(code, note)

    def stop(self) -> bool:
        """Terminate the running program's process group. Returns False if idle."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        self._signal_group(proc, signal.SIGTERM)

        def escalate() -> None:
            if proc.poll() is None:
                self._signal_group(proc, signal.SIGKILL)

        timer = threading.Timer(2.0, escalate)
        timer.daemon = True
        timer.start()
        return True

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            # start_new_session=True made the child its own group leader, so
            # pgid == pid; this never touches Termux or this application.
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def wait(self, timeout: Optional[float] = None) -> bool:
        m = self._monitor
        if m is None:
            return True
        m.join(timeout)
        return not m.is_alive()
