"""Termux:GUI front end. Needs the Termux:GUI Android app + `termuxgui` module.

Layout: a single Activity holding one WebView that renders the editor, toolbar
and console. Python <-> WebView:
  * WebView -> Python: console.log("PSBRIDGE:{json}") arrives as a
    `webviewConsoleMessage` event (Protocol.md, Events).
  * Python -> WebView: WebView.evaluatejs("PS.fn(json args)").

How the page gets into the WebView (only documented Termux:GUI calls are used):
  * the small editor page goes in with WebView.setdata();
  * Monaco's files are then pushed through evaluatejs() in checked chunks (monaco_bundle.py,
    Controller._push_assets). A URL cannot be used: http://127.0.0.1 is refused by the
    Termux:GUI app with net::ERR_CLEARTEXT_NOT_PERMITTED, file:// is disabled and cannot be
    enabled. No server is started and nothing listens on the network.
  * --basic-editor: assets/basic_editor.html through setdata(), no Monaco.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from importlib import resources
from typing import Optional
from urllib.parse import quote

from . import monaco_assets
from .monaco_bundle import AssetBundle
from .bridge import Controller
from .files import Document

PUMP_INTERVAL = 0.05
READY_TIMEOUT = 10.0


def load_page(name: str) -> str:
    return resources.files("python_studio").joinpath("assets/" + name).read_text(encoding="utf-8")


def load_editor_html(name: str = "basic_editor.html") -> str:
    """A setdata() page; must stay ASCII-only, see encode_for_setdata."""
    html = load_page(name)
    # WebView.setdata() has no base64 option in the bindings: stay ASCII-only.
    html.encode("ascii")
    return html


def encode_for_setdata(html: str) -> str:
    """WebView.setdata() ends up in Android's WebView.loadData(), which treats the
    string as a URL-encoded data: URI body: a raw '#' cuts the page off and a raw '%'
    is an invalid escape. Both are everywhere in CSS, so percent-encode everything."""
    return quote(html, safe="")


def run(doc: Document, basic_editor: bool = False) -> int:
    try:
        import termuxgui as tg
    except ModuleNotFoundError:
        print(
            "python-studio: the 'termuxgui' module is missing.\n"
            "Install it with:  pip install termuxgui\n"
            "(You also need the Termux:GUI Android app, see the README.)",
            file=sys.stderr,
        )
        return 2

    bundle: Optional[AssetBundle] = None
    notice = None
    if basic_editor:
        html = load_editor_html("basic_editor.html")
        notice = "basic fallback editor active (--basic-editor): this is NOT Monaco"
    else:
        assets = monaco_assets.find_assets()
        if assets is None:
            print("python-studio: " + monaco_assets.describe_missing(), file=sys.stderr)
            return 3
        try:
            bundle = AssetBundle.build(assets.path)  # read + validate before touching the GUI
        except monaco_assets.AssetError as exc:
            print(f"python-studio: {exc}", file=sys.stderr)
            return 3
        html = load_editor_html("editor.html")

    try:
        conn_cm = tg.Connection()
    except Exception as exc:  # the library raises RuntimeError with its own hint
        print(f"python-studio: cannot connect to Termux:GUI: {exc}", file=sys.stderr)
        return 2

    gui_lock = threading.Lock()  # the bindings need GUI calls serialised between threads
    stop_pump = threading.Event()
    exit_code = 0
    debug = bool(os.environ.get("PSTUDIO_DEBUG"))

    with conn_cm as c:
        a = tg.Activity(c, intercept=True)  # back button -> event, so unsaved edits can't vanish
        a.setinputmode("resize")  # keyboard shrinks the window instead of covering it
        wv = tg.WebView(a)
        wv.setheight(tg.View.MATCH_PARENT)
        wv.setwidth(tg.View.MATCH_PARENT)

        # allowjavascript() blocks until the user answers the Android dialog. Its
        # return value proved unreliable on a real device (False even after the user
        # tapped "allow"), so it is not trusted; the "ready" watchdog below is the
        # real check that JavaScript is running.
        wv.allowjavascript(True)

        def eval_js(code: str) -> None:
            with gui_lock:
                wv.evaluatejs(code)

        ctl = Controller(doc, eval_js=eval_js, finish=a.finish, notice=notice, assets=bundle)

        def pump() -> None:
            while not stop_pump.is_set():
                try:
                    ctl.pump()
                except Exception as exc:
                    print(f"python-studio: output pump failed: {exc!r}", file=sys.stderr)
                time.sleep(PUMP_INTERVAL)

        threading.Thread(target=pump, daemon=True).start()

        with gui_lock:
            wv.setdata(encode_for_setdata(html))

        ready = threading.Event()

        def watchdog() -> None:
            if not ready.wait(READY_TIMEOUT):
                print(
                    "python-studio: the editor page did not start within "
                    f"{READY_TIMEOUT:.0f} s (JavaScript denied, or the WebView failed to load). "
                    "Run again and tap 'allow Javascript'.",
                    file=sys.stderr,
                )
                with gui_lock:
                    a.finish()

        threading.Thread(target=watchdog, daemon=True).start()

        try:
            for ev in c.events():
                if debug:
                    print(f"[debug] event {ev.type}: {str(ev.value)[:200]}", file=sys.stderr)
                if ev.type == tg.Event.webviewConsoleMessage:
                    msg = ev.value.get("msg") if isinstance(ev.value, dict) else None
                    if isinstance(msg, str):
                        if '"ready"' in msg:
                            ready.set()
                        ctl.handle(msg)
                elif ev.type == tg.Event.back:
                    eval_js("PS.back();")
                elif ev.type == tg.Event.webviewDestroyed:
                    print("python-studio: Android killed the WebView process.", file=sys.stderr)
                    exit_code = 4
                    break
                elif ev.type == tg.Event.destroy:
                    value = ev.value if isinstance(ev.value, dict) else {}
                    if value.get("finishing", value.get("finish", True)):
                        break
        except KeyboardInterrupt:
            pass
        finally:
            stop_pump.set()
            ctl.shutdown()
            try:
                c.totermux()  # bring the normal Termux terminal back
            except Exception:
                pass
    return exit_code
