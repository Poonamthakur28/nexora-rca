"""Stack trace parsing for Python, Java, Node.js and Go.

Every parser returns frames ordered from the outermost call to the innermost (where the error was
raised), so "deepest application frame" means the same thing in every language. Frames are mapped to
paths relative to the service's source tree when possible; `resolve_source_path` handles the rest.
"""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Frame:
    path: str
    line: int
    function: str
    source_line: str
    is_app: bool
    relative_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- language detection

_PY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
_JAVA_FRAME = re.compile(r"^\s*at (?P<func>[\w$.<>]+)\((?P<file>[\w$.-]+\.(?:java|kt|scala)):(?P<line>\d+)\)")
_NODE_FRAME = re.compile(r"^\s*at (?:(?:async )?(?P<func>[^\s(]+(?: \[as [^\]]+\])?) \()?(?P<path>(?:file://)?[^\s()]+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_GO_LOCATION = re.compile(r"^\t(?P<path>/\S+\.go):(?P<line>\d+)(?: \+0x[0-9a-f]+)?$")


def detect_language(text: str) -> str | None:
    if "Traceback (most recent call last)" in text or _PY_FRAME.search(text or ""):
        return "python"
    for line in (text or "").splitlines():
        if _JAVA_FRAME.match(line):
            return "java"
        if _GO_LOCATION.match(line):
            return "go"
        if _NODE_FRAME.match(line):
            return "node"
    return None


# ---------------------------------------------------------------- helpers

_PY_LIBRARY = ("site-packages", "dist-packages", "/lib/python", "<frozen")
_JAVA_LIBRARY = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "kotlin.", "scala.", "org.springframework.",
                 "org.apache.", "org.hibernate.", "io.netty.", "reactor.", "com.fasterxml.")
_GO_LIBRARY = ("/usr/local/go/src/", "/go/pkg/mod/", "/usr/lib/go")


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


def _posix(path: str) -> str:
    """Windows separators to '/', so frames from a native Windows run map like container paths."""
    return path.replace("\\", "/")


def _relative(path: str, runtime_prefix: str | None) -> str | None:
    """The frame's path below the runtime prefix, '/'-separated, or None when it is outside it.
    Windows drive paths (C:\\..., e:/...) compare case-insensitively; POSIX paths stay case-sensitive."""
    if not runtime_prefix:
        return None
    p, prefix = _posix(path), _posix(runtime_prefix)
    if _WINDOWS_DRIVE.match(prefix):
        return p[len(prefix):] if p.casefold().startswith(prefix.casefold()) else None
    return p[len(prefix):] if p.startswith(prefix) else None


# ---------------------------------------------------------------- Python

def last_traceback(text: str) -> str:
    """For chained exceptions ("During handling of the above exception..."), keep the final traceback."""
    parts = text.split("Traceback (most recent call last):")
    return "Traceback (most recent call last):" + parts[-1] if len(parts) > 1 else text


def _python_frames(text: str, runtime_prefix: str | None) -> list[Frame]:
    lines = last_traceback(text).splitlines()
    frames = []
    for i, line in enumerate(lines):
        m = _PY_FRAME.match(line)
        if not m:
            continue
        path = m.group("path")
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        library = any(marker in path for marker in _PY_LIBRARY)
        rel = _relative(path, runtime_prefix)
        is_app = not library and (rel is not None if runtime_prefix else True)
        frames.append(Frame(path, int(m.group("line")), m.group("func").strip(), nxt, is_app,
                            rel or (_posix(path).lstrip("/") if is_app else None)))
    return frames  # Python prints outermost first already


# ---------------------------------------------------------------- Java / JVM

def _java_root_cause_block(text: str) -> list[str]:
    """The frames of the last 'Caused by:' (the root cause), or of the only exception."""
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("Caused by:")]
    return lines[starts[-1]:] if starts else lines


def _java_frames(text: str, app_namespaces: list[str] | None) -> list[Frame]:
    frames = []
    for line in _java_root_cause_block(text):
        m = _JAVA_FRAME.match(line)
        if not m:
            continue
        func, file = m.group("func"), m.group("file")
        cls = func.rsplit(".", 1)[0]
        package = cls.rsplit(".", 1)[0] if "." in cls else ""
        rel = (package.replace(".", "/") + "/" if package else "") + file
        if app_namespaces:
            is_app = any(func.startswith(ns) for ns in app_namespaces)
        else:
            is_app = not func.startswith(_JAVA_LIBRARY)
        frames.append(Frame(rel, int(m.group("line")), func, "", is_app, rel))
    return list(reversed(frames))  # JVM prints innermost first


# ---------------------------------------------------------------- Node.js

def _node_frames(text: str, runtime_prefix: str | None) -> list[Frame]:
    frames = []
    for line in text.splitlines():
        m = _NODE_FRAME.match(line)
        if not m:
            continue
        path = m.group("path").removeprefix("file://")
        internal = path.startswith("node:") or "node_modules" in path or not path.startswith("/")
        rel = _relative(path, runtime_prefix)
        is_app = not internal and (rel is not None if runtime_prefix else True)
        frames.append(Frame(path, int(m.group("line")), m.group("func") or "<anonymous>", "", is_app,
                            rel or (path.lstrip("/") if is_app else None)))
    return list(reversed(frames))  # V8 prints innermost first


# ---------------------------------------------------------------- Go

def _go_frames(text: str, runtime_prefix: str | None) -> list[Frame]:
    lines = text.splitlines()
    frames = []
    for i, line in enumerate(lines):
        m = _GO_LOCATION.match(line)
        if not m or i == 0:
            continue
        func = lines[i - 1].strip()
        func = re.sub(r"\(.*\)$", "", func) or "?"
        path = m.group("path")
        library = path.startswith(_GO_LIBRARY)
        rel = _relative(path, runtime_prefix)
        is_app = not library and (rel is not None if runtime_prefix else True)
        frames.append(Frame(path, int(m.group("line")), func, "", is_app, rel or (path.lstrip("/") if is_app else None)))
    # Only the panicking goroutine (the first block) matters.
    return list(reversed(frames[: _first_goroutine_len(lines, frames)]))


def _first_goroutine_len(lines: list[str], frames: list[Frame]) -> int:
    count, seen_goroutine = 0, False
    for line in lines:
        if line.startswith("goroutine "):
            if seen_goroutine:
                break
            seen_goroutine = True
        elif _GO_LOCATION.match(line):
            count += 1
    return count or len(frames)


# ---------------------------------------------------------------- public API

def parse_frames(text: str, runtime_prefix: str | None, language: str | None = None,
                 app_namespaces: list[str] | None = None) -> list[Frame]:
    language = language or detect_language(text or "")
    if language == "java":
        return _java_frames(text, app_namespaces)
    if language == "node":
        return _node_frames(text, runtime_prefix)
    if language == "go":
        return _go_frames(text, runtime_prefix)
    return _python_frames(text or "", runtime_prefix)


def exception_line(text: str, language: str | None = None) -> str:
    """The 'ErrorType: message' line (Python: last line; JVM: root cause; Node/Go: first line)."""
    language = language or detect_language(text or "")
    if not text:
        return ""
    if language == "java":
        block = _java_root_cause_block(text)
        return block[0].removeprefix("Caused by:").strip() if block else ""
    if language in ("node", "go"):
        return next((l.strip() for l in text.splitlines() if l.strip()), "")
    for line in reversed(last_traceback(text).strip().splitlines()):
        if line and not line.startswith(" ") and ":" in line:
            return line.strip()
    return ""


def deepest_app_frame(frames: list[Frame]) -> Frame | None:
    """The innermost application frame before library code: usually where the faulty line is."""
    app = [f for f in frames if f.is_app]
    return app[-1] if app else None


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist", "vendor"}


def resolve_source_path(root: Path, relative: str, max_files: int = 50_000) -> Path | None:
    """Finds a frame's file in the source tree: the exact relative path first, otherwise the file
    whose path shares the longest suffix with it (Java packages under src/main/java, absolute
    container paths, and so on). Never returns a path outside `root`."""
    root = root.resolve()
    direct = (root / relative).resolve()
    if root in direct.parents and direct.is_file():
        return direct
    wanted = [p for p in Path(relative).parts if p not in ("..", ".", "/")]
    if not wanted:
        return None
    best, best_score, tie = None, 0, False
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if wanted[-1] not in filenames:
            seen += len(filenames)
            if seen > max_files:
                break
            continue
        candidate = Path(dirpath) / wanted[-1]
        parts = candidate.relative_to(root).parts
        score = 0
        for a, b in zip(reversed(parts), reversed(wanted)):
            if a != b:
                break
            score += 1
        if score > best_score:
            best, best_score, tie = candidate, score, False
        elif score == best_score:
            tie = True
    if best is None or tie:
        return None  # ambiguous: two files match equally well, so do not guess
    return best.resolve()
