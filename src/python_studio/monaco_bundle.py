"""Prepare Monaco's files for delivery through the Termux:GUI bridge.

Why this exists: the Termux:GUI WebView cannot load Monaco from a URL. http://127.0.0.1 is
refused by Android's cleartext policy (net::ERR_CLEARTEXT_NOT_PERMITTED, a property of the
Termux:GUI app that a Python package cannot change), file:// is disabled and there is no
switch for it, and a local https server would need a certificate the WebView trusts. What
Termux:GUI does support is setData() (a data: page), evaluateJS() and console messages. So:

  1. the small editor page is loaded with setData(),
  2. Python sends Monaco's files to it in base64 chunks with evaluateJS(),
  3. the page checks them (size + Adler-32), executes them and starts Monaco.

Monaco's AMD bundle files use *named* define() calls, so executing every file once registers
all modules; the loader then never needs to fetch a script. The fixed list below is the whole
runtime the Python editor needs. Nothing here is chosen by the page, so there is no path input.
"""

from __future__ import annotations

import base64
import os
import re
import zlib
from dataclasses import dataclass
from typing import Iterator, List, Tuple

from .monaco_assets import AssetError

CHUNK_RAW = 3 * 65536            # 192 KiB raw -> 256 KiB base64; multiple of 3 so chunks need no padding
MAX_BUNDLE_BYTES = 24 * 1024 * 1024
MAX_INLINED_RESOURCE = 1024 * 1024

# (relative path, required) in execution order. loader.js must be first.
JS_FILES: Tuple[Tuple[str, bool], ...] = (
    ("vs/loader.js", True),
    ("vs/editor/editor.main.nls.js", False),
    ("vs/editor/editor.main.js", True),
    ("vs/basic-languages/python/python.js", True),
)
CSS_FILE = "vs/editor/editor.main.css"

MIME = {".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2", ".svg": "image/svg+xml",
        ".png": "image/png", ".gif": "image/gif"}
_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+?)\1\s*\)")


@dataclass
class BundleFile:
    name: str
    kind: str       # "js" | "css"
    data: bytes


def _safe_read(root: str, rel: str) -> bytes:
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or "\\" in rel or "\x00" in rel:
        raise AssetError(f"unsafe asset path {rel!r}")
    real_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(real_root, *parts))
    if os.path.commonpath([real_root, target]) != real_root or not os.path.isfile(target):
        raise FileNotFoundError(rel)
    with open(target, "rb") as fh:
        return fh.read()


def inline_css_resources(css: bytes, root: str, css_rel: str = CSS_FILE) -> bytes:
    """Replace url(<local file>) in the stylesheet with data: URIs (the page has no base URL).
    References that are absolute, already data:, missing, outside *root* or too big stay as is."""
    base_dir = css_rel.rsplit("/", 1)[0]
    text = css.decode("utf-8")

    def repl(m):
        ref = m.group(2).strip()
        if re.match(r"^(data:|https?:|//|#)", ref, re.I):
            return m.group(0)
        path = ref.split("#", 1)[0].split("?", 1)[0]
        segs: List[str] = []
        for part in (base_dir + "/" + path).split("/"):
            if part == "..":
                if not segs:
                    return m.group(0)
                segs.pop()
            elif part not in ("", "."):
                segs.append(part)
        ext = os.path.splitext(path)[1].lower()
        if ext not in MIME:
            return m.group(0)
        try:
            data = _safe_read(root, "/".join(segs))
        except (FileNotFoundError, AssetError, OSError):
            return m.group(0)
        if len(data) > MAX_INLINED_RESOURCE:
            return m.group(0)
        return 'url("data:%s;base64,%s")' % (MIME[ext], base64.b64encode(data).decode("ascii"))

    return _URL_RE.sub(repl, text).encode("utf-8")


class AssetBundle:
    def __init__(self, files: List[BundleFile]) -> None:
        self.files = files

    @classmethod
    def build(cls, root: str) -> "AssetBundle":
        files: List[BundleFile] = []
        total = 0
        try:
            for rel, required in JS_FILES:
                try:
                    data = _safe_read(root, rel)
                except FileNotFoundError:
                    if required:
                        raise AssetError(f"Monaco file missing: {rel} (run: python-studio --fetch-monaco)")
                    continue
                files.append(BundleFile(rel, "js", data))
            try:
                css = _safe_read(root, CSS_FILE)
            except FileNotFoundError:
                raise AssetError(f"Monaco file missing: {CSS_FILE} (run: python-studio --fetch-monaco)")
            files.append(BundleFile(CSS_FILE, "css", inline_css_resources(css, root)))
        except OSError as exc:
            raise AssetError(f"cannot read Monaco files: {exc}") from exc
        total = sum(len(f.data) for f in files)
        if total > MAX_BUNDLE_BYTES:
            raise AssetError(f"Monaco bundle is unexpectedly large ({total} bytes)")
        return cls(files)

    @property
    def total_bytes(self) -> int:
        return sum(len(f.data) for f in self.files)

    def _chunk_count(self, f: BundleFile) -> int:
        return max(1, -(-len(f.data) // CHUNK_RAW))

    def manifest(self) -> List[dict]:
        return [
            {"name": f.name, "kind": f.kind, "size": len(f.data),
             "chunks": self._chunk_count(f), "adler": zlib.adler32(f.data) & 0xFFFFFFFF}
            for f in self.files
        ]

    def chunks(self) -> Iterator[Tuple[int, int, str]]:
        """Yield (file index, chunk index, base64 text); concatenating a file's chunks restores it."""
        for fi, f in enumerate(self.files):
            for ci in range(self._chunk_count(f)):
                piece = f.data[ci * CHUNK_RAW:(ci + 1) * CHUNK_RAW]
                yield fi, ci, base64.b64encode(piece).decode("ascii")
