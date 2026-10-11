"""Run a Python file in a child process without blocking the GUI.

Safety properties:
  * argument list only, never a shell
  * child runs in its own session/process group, so Stop signals only that group
  * stdin is a pipe fed by a small writer thread: the GUI thread never blocks on it,
    even if the program is not reading (see send_input / close_input)
  * total captured output per run is bounded (MAX_OUTPUT_BYTES); once reached, further
    output is drained and discarded and a notice is emitted once. A program that keeps
    flooding (more than FLOOD_LIMIT_BYTES discarded) is stopped automatically, so an
    endless ``while True: print(...)`` cannot burn the battery in the background
  * Stop = SIGTERM to the group, then SIGKILL to the group after STOP_GRACE seconds,
    whether or not the main process already died (children that ignore SIGTERM too)
  * shutdown() (IDE exit) is synchronous, so no program outlives the IDE
  * optional memory cap: PSTUDIO_MEM_MB=<n> (RLIMIT_AS via ``ulimit -v``, off by default
    because libraries such as numpy reserve a lot of address space)
"""

from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import sys
import threading
from typing import Callable, Optional

MAX_OUTPUT_BYTES = 1_000_000
FLOOD_LIMIT_BYTES = 64 * 1024 * 1024   # discarded bytes after the cap before the program is stopped
MAX_INPUT_BYTES = 64 * 1024            # one stdin message
STOP_GRACE = 2.0                       # seconds between SIGTERM and SIGKILL
READER_GRACE = 1.5                     # wait for pipe EOF after the main process exited
_IN_QUEUE_MAX = 64

OutputCb = Callable[[str, str], None]        # (kind: "out"|"err"|"note", text)
ExitCb = Callable[[Optional[int], str], None]  # (returncode, human readable note)


class RunnerError(Exception):
    pass


def _mem_limit_from_env() -> Optional[int]:
    raw = os.environ.get("PSTUDIO_MEM_MB")
    if not raw:
        return None
    try:
        mb = int(raw)
    except ValueError:
        return None
    return mb if mb >= 64 else None


class Runner:
    def __init__(
        self,
        on_output: OutputCb,
        on_exit: ExitCb,
        python: Optional[str] = None,
        max_bytes: int = MAX_OUTPUT_BYTES,
        mem_limit_mb: Optional[int] = None,
        flood_limit: int = FLOOD_LIMIT_BYTES,
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._python = python or sys.executable
        self._max_bytes = max_bytes
        self._flood_limit = flood_limit
        self._mem_mb = mem_limit_mb if mem_limit_mb is not None else _mem_limit_from_env()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._seen = 0
        self._truncated = False
        self._flooded = False
        self._stdin_q: Optional["queue.Queue[Optional[bytes]]"] = None
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
            argv = [self._python, "-u", path]
            if self._mem_mb:
                # fixed script, values passed as arguments: nothing user-controlled reaches the shell
                argv = ["sh", "-c", 'ulimit -v "$0" 2>/dev/null; exec "$@"', str(self._mem_mb * 1024)] + argv
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
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
            self._flooded = False
            q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=_IN_QUEUE_MAX)
            self._stdin_q = q
        done = threading.Event()
        threading.Thread(target=self._feed, args=(proc, q), daemon=True).start()
        readers = [
            threading.Thread(target=self._pump, args=(proc, proc.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(proc, proc.stderr, "err"), daemon=True),
        ]
        for t in readers:
            t.start()
        self._monitor = threading.Thread(target=self._watch, args=(proc, readers, q), daemon=True)
        self._monitor.start()

    # ---- stdin ----
    def send_input(self, text: str, newline: bool = True) -> bool:
        """Queue *text* (plus a newline) for the program's stdin. Never blocks.
        False if nothing is running, the message is too big or the queue is full."""
        data = (text + ("\n" if newline else "")).encode("utf-8", "replace")
        if len(data) > MAX_INPUT_BYTES:
            return False
        with self._lock:
            q = self._stdin_q if self._proc is not None else None
        if q is None:
            return False
        try:
            q.put_nowait(data)
        except queue.Full:
            return False
        return True

    def close_input(self) -> bool:
        """Close the program's stdin (end-of-file, like Ctrl-D)."""
        with self._lock:
            q = self._stdin_q if self._proc is not None else None
        if q is None:
            return False
        try:
            q.put_nowait(None)
        except queue.Full:
            return False
        return True

    @staticmethod
    def _feed(proc: subprocess.Popen, q: "queue.Queue[Optional[bytes]]") -> None:
        # the only thread that ever writes to the pipe; it may block if the program
        # is not reading, which is harmless: Stop/exit kills the program -> EPIPE
        try:
            while True:
                data = q.get()
                if data is None:
                    break
                view = memoryview(data)
                while view:
                    n = proc.stdin.write(view)
                    view = view[n:]
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    # ---- output ----
    def _pump(self, proc: subprocess.Popen, stream, kind: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = stream.read(65536 if self._seen >= self._max_bytes else 4096)
                if not chunk:
                    break
                self._emit(proc, kind, chunk, decoder)
            tail = decoder.decode(b"", final=True)
            if tail:
                self._emit_text(proc, kind, tail)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _emit(self, proc: subprocess.Popen, kind: str, chunk: bytes, decoder) -> None:
        notes = []
        flood = False
        with self._lock:
            if self._proc is not proc:
                return  # a stale reader of an earlier run must never write into this console
            room = self._max_bytes - self._seen
            self._seen += len(chunk)
            if room <= 0:
                chunk = b""
                if not self._truncated:
                    self._truncated = True
                    notes.append(self._truncation_note())
                if self._seen - self._max_bytes > self._flood_limit and not self._flooded:
                    self._flooded = True
                    flood = True
                    notes.append("\n[program stopped: it keeps printing far more than the console can hold]\n")
            elif len(chunk) > room:
                chunk = chunk[:room]
                if not self._truncated:
                    self._truncated = True
                    notes.append(self._truncation_note())
        text = decoder.decode(chunk) if chunk else ""
        if text:
            self._on_output(kind, text)
        for n in notes:
            self._on_output("note", n)
        if flood:
            self.stop()

    def _truncation_note(self) -> str:
        return f"\n[output truncated after {self._max_bytes} bytes]\n"

    def _emit_text(self, proc: subprocess.Popen, kind: str, text: str) -> None:
        with self._lock:
            if self._proc is not proc or self._seen >= self._max_bytes:
                return
        self._on_output(kind, text)

    def _watch(self, proc: subprocess.Popen, readers, q) -> None:
        code = proc.wait()
        try:
            q.put_nowait(None)  # let the stdin writer finish
        except queue.Full:
            pass
        for t in readers:
            t.join(timeout=READER_GRACE)
        with self._lock:
            self._proc = None
            self._stdin_q = None
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = str(-code)
            note = f"terminated by signal {name}"
        else:
            note = f"exit code {code}"
        self._on_exit(code, note)

    # ---- stopping ----
    def stop(self) -> bool:
        """Terminate the running program's process group. Returns False if idle."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        self._signal_group(proc, signal.SIGTERM)

        def escalate() -> None:
            # unconditional: the main process may be gone while a child that ignores
            # SIGTERM is still alive in the same group (ESRCH is ignored if not)
            self._signal_group(proc, signal.SIGKILL)

        timer = threading.Timer(STOP_GRACE, escalate)
        timer.daemon = True
        timer.start()
        return True

    def terminate(self, grace: float = 1.0) -> bool:
        """Synchronous stop for IDE exit: SIGTERM, wait up to *grace*, then SIGKILL the
        whole group. Returns False if idle."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        self._signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(proc, signal.SIGKILL)
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
