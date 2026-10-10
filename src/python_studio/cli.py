"""`python-studio` command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional, Sequence

from . import __version__
from .files import Document, FileError, resolve_path


def _confirm_create(path: str) -> bool:
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    try:
        answer = input(f"{path} does not exist. Create it (on first save)? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def main(
    argv: Optional[Sequence[str]] = None,
    run_gui: Optional[Callable[..., int]] = None,
    confirm: Callable[[str], bool] = _confirm_create,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python-studio",
        description="Full-screen Python editor (Monaco) for Termux (Termux:GUI).",
    )
    parser.add_argument("file", nargs="?", help="Python file to edit (created on first save if new)")
    parser.add_argument("--basic-editor", action="store_true",
                        help="use the basic textarea editor instead of Monaco (deliberate fallback)")
    parser.add_argument("--fetch-monaco", action="store_true",
                        help="download the Monaco Editor runtime once (the only network use), then exit")
    parser.add_argument("--version", action="version", version=f"python-studio {__version__}")
    args = parser.parse_args(argv)

    if args.fetch_monaco:
        from .monaco_assets import fetch_main

        return fetch_main([])
    if args.file is None:
        parser.error("the following arguments are required: file")

    try:
        path = resolve_path(args.file, os.getcwd())
        if not os.path.exists(path):
            if not confirm(path):
                print(f"python-studio: {path} does not exist; not creating it.", file=sys.stderr)
                return 1
        doc = Document.open(path)
    except FileError as exc:
        print(f"python-studio: {exc}", file=sys.stderr)
        return 1

    if run_gui is None:
        from .app import run as run_gui  # imported late: needs no GUI for --help/tests
    if args.basic_editor:
        return run_gui(doc, basic_editor=True)
    return run_gui(doc)


if __name__ == "__main__":
    sys.exit(main())
