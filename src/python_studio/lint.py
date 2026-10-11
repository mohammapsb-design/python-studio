"""Lightweight Python lint for the editor (no extra dependencies).

Reports:
  * SyntaxError from ast.parse
  * undefined names (e.g. primt instead of print) by walking the AST

Returns a list of marker dicts for Monaco:
  {message, severity, startLineNumber, startColumn, endLineNumber, endColumn}
"""

from __future__ import annotations

import ast
import builtins
from typing import Any

# Monaco MarkerSeverity.Error == 8, Warning == 4
SEV_ERROR = 8
SEV_WARN = 4

_BUILTIN_NAMES = set(dir(builtins)) | {
    "True", "False", "None",
    "__name__", "__file__", "__doc__", "__package__", "__builtins__",
    "__annotations__", "__loader__", "__spec__",
}


def _marker(
    message: str,
    line: int,
    col: int,
    end_col: int | None = None,
    severity: int = SEV_ERROR,
) -> dict[str, Any]:
    line = max(1, line or 1)
    col = max(1, col or 1)
    if end_col is None:
        end_col = col + 1
    return {
        "message": message,
        "severity": severity,
        "startLineNumber": line,
        "startColumn": col,
        "endLineNumber": line,
        "endColumn": max(col + 1, end_col),
    }


def _collect_defined(tree: ast.AST) -> set[str]:
    defined: set[str] = set(_BUILTIN_NAMES)

    class DefVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            defined.add(node.name)
            for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                defined.add(a.arg)
            if node.args.vararg:
                defined.add(node.args.vararg.arg)
            if node.args.kwarg:
                defined.add(node.args.kwarg.arg)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            defined.add(node.name)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for a in node.names:
                if a.name == "*":
                    continue
                defined.add(a.asname or a.name)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del, ast.Param)):
                defined.add(node.id)

        def visit_arg(self, node: ast.arg) -> None:
            defined.add(node.arg)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                defined.add(node.name)
            self.generic_visit(node)

        # match statement captures (3.10+): `case (a, b)`, `case [x, *rest]`, `case {"k": v, **others}`
        def visit_MatchAs(self, node) -> None:
            if node.name:
                defined.add(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node) -> None:
            if node.name:
                defined.add(node.name)

        def visit_MatchMapping(self, node) -> None:
            if node.rest:
                defined.add(node.rest)
            self.generic_visit(node)

        # PEP 695 type parameters (3.12+): `def f[T](x: T)`, `class C[T]`, `type A[T] = ...`
        def visit_TypeVar(self, node) -> None:
            defined.add(node.name)
            self.generic_visit(node)

        def visit_ParamSpec(self, node) -> None:
            defined.add(node.name)

        def visit_TypeVarTuple(self, node) -> None:
            defined.add(node.name)

        def visit_Global(self, node: ast.Global) -> None:
            for n in node.names:
                defined.add(n)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            for n in node.names:
                defined.add(n)

    DefVisitor().visit(tree)
    return defined


def _undefined_names(tree: ast.AST, defined: set[str]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()

    class UseVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id not in defined:
                key = (node.id, node.lineno, node.col_offset)
                if key not in seen:
                    seen.add(key)
                    markers.append(
                        _marker(
                            f"undefined name '{node.id}'",
                            node.lineno,
                            node.col_offset + 1,
                            node.col_offset + 1 + len(node.id),
                            SEV_ERROR,
                        )
                    )

    UseVisitor().visit(tree)
    return markers


def lint_python(text: str) -> list[dict[str, Any]]:
    """Return Monaco-compatible markers for *text*. Empty list if clean."""
    if not text or not text.strip():
        return []
    try:
        return _lint(text)
    except (RecursionError, MemoryError):
        # absurdly nested source (e.g. 50 000 chained operators) overflows the parser or the
        # visitors; one warning instead of an exception on every edit
        return [_marker("source is too deeply nested to analyse", 1, 1, 2, SEV_WARN)]


def _lint(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        line = exc.lineno or 1
        col = exc.offset or 1
        # Prefer pointing at the token; if offset is past line end, still show something
        end = col + 1
        if exc.text:
            end = max(col + 1, min(len(exc.text.rstrip("\n")) + 1, col + 20))
        msg = exc.msg or "invalid syntax"
        return [_marker(msg, line, col, end, SEV_ERROR)]
    except ValueError as exc:
        # e.g. null bytes
        return [_marker(str(exc) or "invalid source", 1, 1, 2, SEV_ERROR)]

    defined = _collect_defined(tree)
    return _undefined_names(tree, defined)
