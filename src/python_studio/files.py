"""Safe reading and writing of the edited source file.

Policy (documented in README):
  * Files must be valid UTF-8. Anything else is refused, never "repaired".
  * A UTF-8 BOM is preserved: detected on open, written back on save.
  * Line endings: pure LF and pure CRLF files are supported and the original
    style is preserved on save. Mixed or bare-CR files are refused, because the
    editor works on LF text and could not round-trip them.
  * Saving is atomic (temp file in the same directory + os.replace).
  * A path that does not exist is only created on the first explicit save.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB: reasonable for a phone WebView editor
BOM = "\ufeff"


class FileError(Exception):
    """A problem the user should be told about (not a program bug)."""


def resolve_path(arg: str, cwd: str | None = None) -> str:
    """Resolve *arg* to an absolute path relative to the shell's cwd.

    The argument is treated purely as a path: never evaluated, never passed to
    a shell. Spaces and special characters are kept as they are.
    """
    if not arg:
        raise FileError("empty file name")
    if "\x00" in arg:
        raise FileError("file name contains a NUL character")
    expanded = os.path.expanduser(arg)
    if cwd is not None and not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.abspath(expanded)


@dataclass
class Document:
    path: str
    text: str = ""          # always LF-normalised
    crlf: bool = False
    bom: bool = False
    exists: bool = True     # False -> new file, created on first save

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @classmethod
    def open(cls, path: str) -> "Document":
        if os.path.isdir(path):
            raise FileError(f"{path} is a directory, not a file")
        if not os.path.exists(path):
            parent = os.path.dirname(path) or "."
            if not os.path.isdir(parent):
                raise FileError(f"directory does not exist: {parent}")
            return cls(path=path, exists=False)
        try:
            size = os.path.getsize(path)
            if size > MAX_FILE_BYTES:
                raise FileError(
                    f"file is too large for the editor ({size} bytes, limit {MAX_FILE_BYTES})"
                )
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise FileError(f"cannot read {path}: {exc.strerror or exc}") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileError(f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc
        bom = text.startswith(BOM)
        if bom:
            text = text[1:]
        crlf = False
        if "\r" in text:
            lf_only = text.replace("\r\n", "\n")
            if "\r" in lf_only:
                raise FileError("file contains bare CR line endings; not supported")
            # every "\n" must have been part of a CRLF, otherwise it is mixed
            if lf_only.count("\n") != text.count("\r\n"):
                raise FileError("file has mixed line endings (LF and CRLF); not supported")
            crlf = True
            text = lf_only
        return cls(path=path, text=text, crlf=crlf, bom=bom, exists=True)

    def encode(self, text: str) -> bytes:
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.crlf:
            text = text.replace("\n", "\r\n")
        if self.bom:
            text = BOM + text
        return text.encode("utf-8")

    def save(self, text: str) -> None:
        """Atomically write *text*. On failure the file is untouched and
        FileError is raised; the caller keeps the in-memory text."""
        data = self.encode(text)
        if len(data) > MAX_FILE_BYTES:
            raise FileError("document is too large to save")
        target = os.path.realpath(self.path)
        directory = os.path.dirname(target) or "."
        tmp_path = None
        try:
            mode = None
            if os.path.exists(target):
                mode = stat.S_IMODE(os.stat(target).st_mode)
            fd, tmp_path = tempfile.mkstemp(prefix=".pstudio-", dir=directory)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if mode is not None:
                os.chmod(tmp_path, mode)
            os.replace(tmp_path, target)
            tmp_path = None
        except OSError as exc:
            raise FileError(f"cannot save {self.path}: {exc.strerror or exc}") from exc
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.exists = True
