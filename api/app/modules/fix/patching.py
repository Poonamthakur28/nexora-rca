"""Plain-code checks on what the fix provider writes: a safe test, and a small patch to allowed files only.

The provider's diff is never trusted as text. Each hunk is located by its exact context lines in the
original file (hunk header numbers are often wrong in model output, context lines are not); a hunk that
does not match exactly once is rejected, never fuzzed. The stored patch is regenerated from the result.
"""

from __future__ import annotations

import ast
import difflib
import posixpath
import re
from dataclasses import dataclass, field

# Imports and calls a reproduction test has no reason to use. The sandbox is the real boundary; this
# catches obvious misuse before anything runs.
FORBIDDEN_MODULES = {"subprocess", "socket", "shutil", "ctypes", "multiprocessing", "requests", "httpx", "urllib",
                     "http", "ftplib", "smtplib", "telnetlib", "paramiko", "pty", "signal", "webbrowser", "winreg"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input"}
FORBIDDEN_ATTR_CALLS = {("os", "system"), ("os", "popen"), ("os", "remove"), ("os", "unlink"), ("os", "rmdir"),
                        ("os", "removedirs"), ("os", "kill"), ("os", "fork"), ("os", "execv"), ("os", "execve"),
                        ("os", "spawnl"), ("os", "startfile"), ("os", "chmod"), ("os", "rename"), ("os", "replace")}


class PatchError(ValueError):
    """The proposed patch cannot be used. The message is shown to responders and fed back to the model."""


def check_test_safety(code: str) -> list[str]:
    """Problems that make a reproduction test unacceptable (empty list = acceptable)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"test does not parse: {exc.msg} (line {exc.lineno})"]
    problems: list[str] = []
    tests = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith("test")]
    if not tests:
        problems.append("no test function (a function named test_...)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(f"imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in FORBIDDEN_MODULES:
            problems.append(f"imports from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                problems.append(f"calls {fn.id}()")
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                    and (fn.value.id, fn.attr) in FORBIDDEN_ATTR_CALLS:
                problems.append(f"calls {fn.value.id}.{fn.attr}()")
    return sorted(set(problems))


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[list[tuple[str, str]]] = field(default_factory=list)  # [(" "|"-"|"+", line)]

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    def changed_lines(self) -> int:
        return sum(1 for h in self.hunks for tag, _ in h if tag in "+-")


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def _clean_path(raw: str) -> str | None:
    path = raw.strip().split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_unified_diff(text: str) -> list[FileDiff]:
    text = text.replace("\r\n", "\n").strip("\n")
    if text.startswith("```"):  # tolerate a fenced block around the diff
        text = re.sub(r"^```\w*\n|\n```$", "", text)
    lines = text.split("\n")
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: list[tuple[str, str]] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FileDiff(_clean_path(line[4:]), _clean_path(lines[i + 1][4:]))
            files.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("@@"):
            if current is None or not _HUNK.match(line):
                raise PatchError(f"malformed hunk header: {line[:80]}")
            hunk = []
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.append((line[0], line[1:]))
        elif hunk is not None and line == "":
            hunk.append((" ", ""))  # some writers drop the space on empty context lines
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        elif line.startswith(("diff --git", "index ", "new file mode", "deleted file mode")):
            pass
        elif line.strip():
            raise PatchError(f"unexpected line in diff: {line[:80]}")
        i += 1
    if not files or not any(f.hunks for f in files):
        raise PatchError("no file changes found in the diff")
    return files


def validate_scope(diffs: list[FileDiff], allowed: set[str], max_lines: int) -> list[str]:
    """Problems with where and how much the patch changes (empty list = within scope)."""
    problems: list[str] = []
    total = 0
    for d in diffs:
        if d.old_path is None or d.new_path is None:
            problems.append(f"{d.path}: creating or deleting files is not allowed")
            continue
        if d.old_path != d.new_path:
            problems.append(f"{d.old_path}: renaming files is not allowed")
            continue
        norm = posixpath.normpath(d.path)
        if norm != d.path or norm.startswith(("../", "/")) or ".." in norm.split("/") or re.match(r"^[A-Za-z]:", norm):
            problems.append(f"{d.path}: path must be relative to the repository and stay inside it")
            continue
        name = posixpath.basename(norm)
        if norm.startswith("tests/") or "/tests/" in norm or name.startswith("test_") or name == "conftest.py":
            problems.append(f"{d.path}: tests may not be changed")
            continue
        if norm not in allowed:
            problems.append(f"{d.path}: not one of the files the RCA or the release points at "
                            f"({', '.join(sorted(allowed)) or 'none'})")
            continue
        if not d.hunks:
            problems.append(f"{d.path}: no hunks")
        total += d.changed_lines()
    if total > max_lines:
        problems.append(f"patch changes {total} lines; the limit is {max_lines}")
    if len({d.path for d in diffs}) != len(diffs):
        problems.append("a file appears more than once in the diff")
    return problems


def apply_file_diff(original: str, diff: FileDiff) -> str:
    """Apply one file's hunks to `original`, keeping its newline style and final newline."""
    newline = "\r\n" if "\r\n" in original else "\n"
    ends_with_newline = original.endswith(("\n", "\r"))
    lines = original.replace("\r\n", "\n").split("\n")
    if ends_with_newline:
        lines = lines[:-1]
    cursor = 0
    for n, hunk in enumerate(diff.hunks, start=1):
        old = [text for tag, text in hunk if tag in " -"]
        new = [text for tag, text in hunk if tag in " +"]
        if not old:
            raise PatchError(f"{diff.path} hunk {n}: no context lines; cannot locate it safely")
        matches = [i for i in range(cursor, len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
        if not matches:
            stripped = [i for i in range(cursor, len(lines) - len(old) + 1)
                        if [x.rstrip() for x in lines[i:i + len(old)]] == [x.rstrip() for x in old]]
            if len(stripped) != 1:
                raise PatchError(f"{diff.path} hunk {n}: context does not match the current file")
            matches = stripped
        if len(matches) > 1:
            raise PatchError(f"{diff.path} hunk {n}: context matches {len(matches)} places; ambiguous")
        at = matches[0]
        lines[at:at + len(old)] = new
        cursor = at + len(new)
    result = newline.join(lines)
    return result + newline if ends_with_newline else result


def canonical_diff(path: str, before: str, after: str) -> str:
    """A clean unified diff (LF, correct headers), for storage, artifacts and the PR."""
    a = before.replace("\r\n", "\n").splitlines(keepends=True)
    b = after.replace("\r\n", "\n").splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n"
    fromfile = f"a/{path}" if before else "/dev/null"
    return "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=f"b/{path}"))
