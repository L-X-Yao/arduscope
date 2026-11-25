#!/usr/bin/env python3
"""The one place that says what version a source file holds.

tools/release.py bumps the version in src/arduscope/__init__.py, and
tools/check_release_tag.py reads it back from the tagged commit. Both come
here, so they cannot disagree about which assignment is the version.

The file is read as code, with ast, never as text. A regex finds a comment
that spells an assignment before the real one, and a rewrite of every match
rewrites that comment along with the assignment: the file then agrees with
a tag that is a version older than the one before it.

__version__ has to be bound exactly once in the module, by a plain
assignment of a string at module level. Any other binding of the name, an
augmented assignment or one under an if, an import of it, a tuple target,
would leave the version setuptools reads, the first static value, different
from the one the imported module reports. A star import is refused as well:
it could bind the name with nothing in the file to show it.
"""

from __future__ import annotations

import ast
import re

NAME = "__version__"
VERSION_FILE = "src/arduscope/__init__.py"
# What a version literal may hold, so that rewriting one cannot write code.
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")


class VersionSourceError(Exception):
    """Why the source does not hold one plain version."""


def _tree(source: bytes | str, where: str) -> ast.Module:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise VersionSourceError(f"{where} does not parse: {e}") from None


def _bindings(tree: ast.AST) -> list[tuple[ast.AST, str, int]]:
    """Every node of the module that binds, rebinds or drops the name, with
    what it is and the line it is on. Scopes make no difference: a name
    bound anywhere in the module is bound."""
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id == NAME:
            if isinstance(node.ctx, ast.Store):
                found.append((node, "by assignment", line))
            elif isinstance(node.ctx, ast.Del):
                found.append((node, "by deletion", line))
        elif isinstance(node, ast.Import):
            # import a.b binds a, import a.b as c binds c.
            found += [(alias, "by import", line) for alias in node.names
                      if (alias.asname or alias.name.partition(".")[0])
                      == NAME]
        elif isinstance(node, ast.ImportFrom):
            found += [(alias, "by import", line) for alias in node.names
                      if (alias.asname or alias.name) == NAME]
        elif isinstance(node, ast.arg) and node.arg == NAME:
            found.append((node, "as a parameter", line))
        elif isinstance(node, ast.ExceptHandler) and node.name == NAME:
            found.append((node, "as an exception handler's name", line))
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)) and node.name == NAME):
            found.append((node, "as a definition", line))
        elif isinstance(node, getattr(ast, "MatchAs", ())) and node.name == NAME:
            found.append((node, "as a match pattern's capture", line))
        elif (isinstance(node, getattr(ast, "MatchStar", ()))
              and node.name == NAME):
            found.append((node, "as a match pattern's capture", line))
        elif (isinstance(node, getattr(ast, "MatchMapping", ()))
              and node.rest == NAME):
            found.append((node, "as a match pattern's capture", line))
    return found


def _target(statement: ast.stmt) -> ast.expr | None:
    """The one name a plain assignment assigns to, or None for any other
    target: a tuple, an attribute, a subscript."""
    if isinstance(statement, ast.Assign):
        if len(statement.targets) == 1:
            return statement.targets[0]
        return None
    if isinstance(statement, ast.AnnAssign):
        return statement.target
    return None


def _star_import(tree: ast.AST) -> int:
    """The line of the first star import in the module, or 0 when there is
    none. What a star import binds is settled when the module runs, so the
    name could be among it with nothing here the wiser."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
                alias.name == "*" for alias in node.names):
            return node.lineno
    return 0


def version_assignment(source: bytes | str, where: str) -> ast.stmt:
    """The one assignment of a string to __version__ at module level.

    Refused, rather than guessed at: a star import, which could bind the
    name unseen; no binding at all; more than one, be the second an
    assignment under an if, an augmented one or an import of the name; the
    one binding made by anything but a plain assignment; a value that is
    not a string.
    """
    tree = _tree(source, where)
    star = _star_import(tree)
    if star:
        raise VersionSourceError(
            f"{where} imports * at line {star}: what that binds is not to "
            f"be seen here, {NAME} included")
    bindings = _bindings(tree)
    if not bindings:
        raise VersionSourceError(f"{where} does not bind {NAME}")
    if len(bindings) > 1:
        lines = ", ".join(sorted({str(line) for _, _, line in bindings},
                                 key=int))
        raise VersionSourceError(
            f"{where} binds {NAME} {len(bindings)} times (lines {lines}), "
            f"not once")
    node, how, line = bindings[0]
    statement = next((statement for statement in tree.body
                      if isinstance(statement, (ast.Assign, ast.AnnAssign))
                      and _target(statement) is node), None)
    if statement is None:
        raise VersionSourceError(
            f"{where} binds {NAME} at line {line} {how}, not by one plain "
            f"assignment at module level")
    if not (isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)):
        raise VersionSourceError(
            f"{NAME} at line {line} of {where} is not a plain string")
    return statement


def source_version(source: bytes | str, where: str) -> str:
    """What the one assignment to __version__ in the source says."""
    value = version_assignment(source, where).value
    return value.value  # a plain string, as version_assignment has checked


def _offset(source: bytes, line: int, column: int) -> int:
    """ast counts lines from one and columns in bytes of the line."""
    start = 0
    for _ in range(line - 1):
        start = source.index(b"\n", start) + 1
    return start + column


def replaced(source: str, new: str, where: str = VERSION_FILE) -> str:
    """`source` with the value of the one assignment to __version__ set to
    `new`, and no other character of it touched.

    Only the span the value's literal occupies is rewritten. A comment that
    spells an assignment, above or below, stays as it was; so does the
    quote the assignment uses, when it is a plain one. The result is read
    back, and refused if it does not say `new`.
    """
    if not VERSION.fullmatch(new):
        raise VersionSourceError(f"{new!r} is not a version number")
    data = source.encode("utf-8")
    value = version_assignment(data, where).value
    start = _offset(data, value.lineno, value.col_offset)
    stop = _offset(data, value.end_lineno, value.end_col_offset)
    literal = data[start:stop]
    quote = literal[:1] if literal[:1] in (b'"', b"'") else b'"'
    rewritten = data[:start] + quote + new.encode("ascii") + quote + data[stop:]
    result = rewritten.decode("utf-8")
    if source_version(result, where) != new:
        raise VersionSourceError(
            f"{where} does not read {new!r} once the assignment is rewritten")
    return result
