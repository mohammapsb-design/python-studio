"""Locate, validate and (on explicit request) download the Monaco Editor runtime.

Monaco is the official `monaco-editor` npm package, AMD build (`min/vs`), pinned
to MONACO_VERSION. At launch monaco_bundle.py sends the needed files to the WebView through
the Termux:GUI bridge (the WebView cannot load them from a URL; see that module). Nothing is downloaded at application start; the only network
access in this project is `python-studio --fetch-monaco` (or
`scripts/fetch_monaco.py`), which the user runs once.

Lookup order for the assets directory (the directory that contains `vs/`):
  1. $PSTUDIO_MONACO_DIR
  2. user data dir:  $XDG_DATA_HOME/python-studio/monaco  (default ~/.local/share/...)
  3. bundled:        <package>/assets/monaco   (for self-contained builds)
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

MONACO_VERSION = "0.52.2"
REGISTRY_URL = "https://registry.npmjs.org/monaco-editor/" + MONACO_VERSION
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
ENV_VAR = "PSTUDIO_MONACO_DIR"

# Files without which the Python editor cannot start (paths relative to the assets dir).
REQUIRED_FILES = (
    "vs/loader.js",
    "vs/editor/editor.main.js",
    "vs/editor/editor.main.css",
    "vs/basic-languages/python/python.js",
)


class AssetError(Exception):
    """Monaco assets are missing, damaged or could not be downloaded."""


@dataclass
class Assets:
    path: str
    origin: str  # "env" | "user" | "bundled"


def bundled_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "monaco")


def user_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "python-studio", "monaco")


def missing_files(path: str) -> List[str]:
    return [rel for rel in REQUIRED_FILES if not os.path.isfile(os.path.join(path, *rel.split("/")))]


def candidates() -> List[tuple]:
    out = []
    env = os.environ.get(ENV_VAR)
    if env:
        out.append((os.path.abspath(os.path.expanduser(env)), "env"))
    out.append((user_dir(), "user"))
    out.append((bundled_dir(), "bundled"))
    return out


def find_assets() -> Optional[Assets]:
    """First complete assets directory, or None. An env override that is set but
    incomplete is NOT skipped silently: see describe_missing()."""
    for path, origin in candidates():
        if not missing_files(path):
            return Assets(path, origin)
    return None


def describe_missing() -> str:
    lines = ["Monaco Editor assets were not found. Looked in:"]
    for path, origin in candidates():
        gaps = missing_files(path)
        if not os.path.isdir(path):
            lines.append(f"  - {path} ({origin}): directory does not exist")
        else:
            lines.append(f"  - {path} ({origin}): incomplete, missing {', '.join(gaps[:3])}")
    lines += [
        "",
        "Download them once (needs internet, about 15 MB):",
        "    python-studio --fetch-monaco",
        "or run the basic textarea editor instead (no Monaco):",
        "    python-studio --basic-editor FILE",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- download

Opener = Callable[..., object]


def _read_limited(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise AssetError("download is unexpectedly large; aborting")
    return data


def _verify_integrity(data: bytes, dist: dict) -> None:
    integrity = dist.get("integrity")
    if integrity:
        algo, _, b64 = integrity.partition("-")
        if algo not in ("sha512", "sha384", "sha256"):
            raise AssetError(f"unsupported integrity algorithm {algo!r}")
        digest = base64.b64encode(hashlib.new(algo, data).digest()).decode("ascii")
        if digest != b64:
            raise AssetError("downloaded archive failed its integrity check (sha-%s mismatch)" % algo[3:])
        return
    shasum = dist.get("shasum")
    if shasum:
        if hashlib.sha1(data).hexdigest() != shasum:
            raise AssetError("downloaded archive failed its integrity check (sha1 mismatch)")
        return
    raise AssetError("registry metadata has no checksum; refusing to install unverified code")


def extract_tarball(data: bytes, dest: str) -> int:
    """Extract package/min/vs and package/LICENSE from an npm tarball into *dest*.

    Only regular files are accepted; links, devices and any path that would leave
    *dest* are rejected. Returns the number of files written."""
    root = os.path.realpath(dest)
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            name = member.name
            if name == "package/LICENSE":
                rel = "LICENSE"
            elif name.startswith("package/min/vs/"):
                rel = name[len("package/min/"):]
            else:
                continue
            if member.isdir():
                continue
            if not member.isreg():
                raise AssetError(f"unexpected non-file entry in archive: {name}")
            parts = rel.split("/")
            if any(p in ("", ".", "..") for p in parts) or "\\" in rel or "\x00" in rel:
                raise AssetError(f"unsafe path in archive: {name}")
            target = os.path.realpath(os.path.join(root, *parts))
            if os.path.commonpath([root, target]) != root:
                raise AssetError(f"unsafe path in archive: {name}")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise AssetError("archive expands to an unexpectedly large size; aborting")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise AssetError(f"cannot read archive entry {name}")
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def fetch(
    dest: Optional[str] = None,
    opener: Opener = urllib.request.urlopen,
    log: Callable[[str], None] = print,
) -> str:
    """Download and install Monaco into *dest* (default: the user data dir).

    The archive is checked against the checksum published by the npm registry,
    extracted into a temp dir next to *dest* and swapped in only when complete and
    valid, so a failed or interrupted fetch never leaves a half-installed editor."""
    dest = os.path.abspath(dest or user_dir())
    parent = os.path.dirname(dest)
    try:
        os.makedirs(parent, exist_ok=True)
        log(f"Fetching metadata for monaco-editor@{MONACO_VERSION} from the npm registry ...")
        with opener(REGISTRY_URL, timeout=30) as resp:
            meta = json.loads(_read_limited(resp, 1024 * 1024).decode("utf-8"))
        dist = meta.get("dist") or {}
        tarball = dist.get("tarball")
        if not isinstance(tarball, str) or not tarball.startswith("https://"):
            raise AssetError("registry metadata did not contain an https tarball URL")
        log("Downloading " + tarball)
        with opener(tarball, timeout=120) as resp:
            data = _read_limited(resp, MAX_DOWNLOAD_BYTES)
        _verify_integrity(data, dist)
        tmp = tempfile.mkdtemp(prefix=".monaco-new-", dir=parent)
        try:
            n = extract_tarball(data, tmp)
            gaps = missing_files(tmp)
            if gaps:
                raise AssetError("downloaded package is missing: " + ", ".join(gaps))
            with open(os.path.join(tmp, "VERSION"), "w", encoding="ascii") as fh:
                fh.write(MONACO_VERSION + "\n")
            old = None
            if os.path.exists(dest):
                old = dest + ".old"
                shutil.rmtree(old, ignore_errors=True)
                os.replace(dest, old)
            os.replace(tmp, dest)
            tmp = None
            if old:
                shutil.rmtree(old, ignore_errors=True)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    except AssetError:
        raise
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise AssetError(f"could not fetch Monaco: {exc}") from exc
    log(f"Installed {n} files to {dest}")
    return dest


def fetch_main(argv: Optional[list] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="fetch_monaco", description="Download the Monaco Editor runtime.")
    p.add_argument("--dest", help="target directory (default: %s)" % user_dir())
    p.add_argument("--bundle", action="store_true",
                   help="install into the package's assets/monaco instead (for building a self-contained wheel)")
    args = p.parse_args(argv)
    try:
        fetch(bundled_dir() if args.bundle else args.dest)
    except AssetError as exc:
        print(f"python-studio: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(fetch_main())
