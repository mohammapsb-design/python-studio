"""GUI-independent controller between the WebView editor and Python.

The WebView can only talk to Python through console.log lines that start with
BRIDGE_PREFIX followed by JSON (Termux:GUI delivers them as
`webviewConsoleMessage` events). Only a tiny fixed set of operations is
accepted; there is no generic "execute this command" path, and the file that
can be saved or run is always the one chosen on the command line.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Callable, Optional

from .files import MAX_FILE_BYTES, Document, FileError
from .lint import lint_python
from .runner import Runner, RunnerError

BRIDGE_PREFIX = "PSBRIDGE:"
ALLOWED_OPS = {"ready", "save", "run", "stop", "exit", "lint", "stdin", "eof"}
JS_CHUNK_CHARS = 32 * 1024  # one evaluatejs() never carries more console text than this


def js_call(fn: str, *args: Any) -> str:
    """Build a JS call with JSON-encoded arguments (ASCII-only, injection safe)."""
    return f"PS.{fn}({', '.join(json.dumps(a) for a in args)});"


def parse_bridge_message(raw: str) -> Optional[dict]:
    """Return the validated message dict, or None if *raw* is not for us."""
    if not isinstance(raw, str):
        return None
    idx = raw.find(BRIDGE_PREFIX)
    if idx < 0:
        return None
    try:
        msg = json.loads(raw[idx + len(BRIDGE_PREFIX):])
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    op = msg.get("op")
    if op not in ALLOWED_OPS:
        return None
    if "text" in msg:
        text = msg["text"]
        if not isinstance(text, str) or len(text) > MAX_FILE_BYTES:
            return None
    for flag in ("then_exit",):
        if flag in msg and not isinstance(msg[flag], bool):
            return None
    if op in ("save", "run", "lint", "stdin") and "text" not in msg:
        return None
    return msg


class Controller:
    def __init__(
        self,
        doc: Document,
        eval_js: Callable[[str], None],
        finish: Callable[[], None],
        python: Optional[str] = None,
        max_output_bytes: Optional[int] = None,
        notice: Optional[str] = None,
        assets: Optional[Any] = None,
    ) -> None:
        self.doc = doc
        self._assets = assets  # AssetBundle (Monaco files) pushed to the page before the document
        self._notice = notice  # shown once after the document is loaded (e.g. "fallback editor active")
        self._eval_js = eval_js
        self._finish = finish
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []
        self._outbox: list[str] = []  # JS calls built but not yet delivered (retried if eval_js fails)
        kwargs = {}
        if max_output_bytes is not None:
            kwargs["max_bytes"] = max_output_bytes
        self.runner = Runner(self._on_output, self._on_exit, python=python, **kwargs)

    # ---- runner callbacks (called from reader/monitor threads) ----
    def _on_output(self, kind: str, text: str) -> None:
        with self._lock:
            self._pending.append((kind, text))

    def _on_exit(self, code: Optional[int], note: str) -> None:
        with self._lock:
            self._pending.append(("end", json.dumps([code, note])))

    def pump(self) -> None:
        """Forward queued output to the WebView, batching same-kind chunks.

        Calls are built into an outbox and removed only after eval_js() succeeded, so a
        transient failure cannot drop output or, worse, the final "run ended" event
        (which would leave the UI stuck in the running state)."""
        with self._lock:
            items, self._pending = self._pending, []
        batch_kind: Optional[str] = None
        batch: list[str] = []

        def flush() -> None:
            nonlocal batch_kind, batch
            if batch_kind is not None and batch:
                text = "".join(batch)
                for i in range(0, len(text), JS_CHUNK_CHARS):
                    self._outbox.append(js_call("out", batch_kind, text[i:i + JS_CHUNK_CHARS]))
            batch_kind, batch = None, []

        for kind, text in items:
            if kind == "end":
                flush()
                code, note = json.loads(text)
                self._outbox.append(js_call("runEnded", code, note))
                continue
            if kind != batch_kind:
                flush()
                batch_kind = kind
            batch.append(text)
        flush()
        while self._outbox:
            self._eval_js(self._outbox[0])  # raises -> the call stays queued for the next tick
            del self._outbox[0]

    # ---- messages from the WebView ----
    def handle(self, raw: str) -> None:
        msg = parse_bridge_message(raw)
        if msg is None:
            if isinstance(raw, str) and raw.find(BRIDGE_PREFIX) >= 0:
                self._eval_js(js_call("fail", "ignored an invalid bridge message"))
            return
        try:
            getattr(self, "_op_" + msg["op"])(msg)
        except Exception as exc:  # never swallow silently: show in the console
            print(f"pyo: internal error: {exc!r}", file=sys.stderr)
            self._eval_js(js_call("fail", f"internal error: {exc!r}"))

    def _push_assets(self) -> None:
        """Deliver Monaco's files to the page (setData pages cannot load them from a URL)."""
        self._eval_js(js_call("assetsBegin", self._assets.manifest()))
        for file_index, chunk_index, b64 in self._assets.chunks():
            self._eval_js(js_call("assetChunk", file_index, chunk_index, b64))
        self._eval_js(js_call("assetsEnd"))

    def _op_ready(self, msg: dict) -> None:
        if self._assets is not None:
            self._push_assets()
        self._eval_js(js_call("load", self.doc.text, self.doc.name, self.doc.exists))
        if self._notice:
            self._eval_js(js_call("notice", self._notice))

    def _save(self, text: str) -> bool:
        try:
            self.doc.save(text)
        except FileError as exc:
            self._eval_js(js_call("saved", False, str(exc)))
            return False
        self._eval_js(js_call("saved", True, "saved " + self.doc.name))
        return True

    def _op_save(self, msg: dict) -> None:
        ok = self._save(msg["text"])
        if ok and msg.get("then_exit"):
            self.shutdown()
            self._finish()

    def _op_run(self, msg: dict) -> None:
        if self.runner.running:
            self._eval_js(js_call("fail", "a program is already running"))
            return
        if not self._save(msg["text"]):
            return  # never run a stale file
        # announce first: runStarted clears the console, so it must precede any output
        self._eval_js(js_call("runStarted", self.doc.name))
        try:
            self.runner.start(self.doc.path)
        except RunnerError as exc:
            self._eval_js(js_call("fail", str(exc)))
            self._eval_js(js_call("runEnded", None, "could not start"))

    def _op_stop(self, msg: dict) -> None:
        self.runner.stop()

    def _op_stdin(self, msg: dict) -> None:
        """One line typed into the console's input box (a newline is added)."""
        if not self.runner.running:
            self._eval_js(js_call("fail", "no program is running, input ignored"))
        elif not self.runner.send_input(msg["text"]):
            self._eval_js(js_call("fail", "input rejected (too long, or the program is not reading its input)"))

    def _op_eof(self, msg: dict) -> None:
        self.runner.close_input()

    def _op_lint(self, msg: dict) -> None:
        """Syntax + undefined-name check; results become Monaco red markers."""
        markers = lint_python(msg["text"])
        self._eval_js(js_call("markers", markers))

    def _op_exit(self, msg: dict) -> None:
        self.shutdown()
        self._finish()

    def shutdown(self) -> None:
        self.runner.terminate()  # synchronous: nothing may outlive the IDE
