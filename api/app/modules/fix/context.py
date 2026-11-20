"""What the fix provider sees, and which files a patch may touch.

Built from the incident's latest grounded RCA report and evidence:
  - the blamed file, the call chain and files changed by a release in the window (allowed to change)
  - repository modules those files import (read-only context, e.g. flags or settings they depend on)
  - the existing tests (read-only; they are the regression suite)
All source text is redacted. Paths never leave the service's source root.
"""

from __future__ import annotations

import ast
import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Evidence, Incident, RcaReport, Service
from app.modules.evidence.redaction import redact
from app.modules.evidence.stacktrace import resolve_source_path

ELIGIBLE_CATEGORIES = ("code", "resource")
SUPPORTED_LANGUAGES = ("python",)
MAX_SOURCE_CHARS = 12_000
MAX_TEST_CHARS = 8_000
REPRO_TEST_PATH = "tests/test_nexora_repro.py"


@dataclass
class FixContext:
    incident: Incident
    service: Service
    report: RcaReport
    target_file: str
    allowed_files: set[str]
    originals: dict[str, str]  # allowed file -> current text (unredacted; never sent as-is, never stored)
    context_files: dict[str, str] = field(default_factory=dict)  # path -> redacted text for the prompt
    test_files: dict[str, str] = field(default_factory=dict)  # existing tests, redacted
    exception_type: str | None = None
    exception_message: str = ""
    stacktrace: str = ""


def eligibility(report: RcaReport | None, service: Service | None) -> str | None:
    """Why a fix cannot be attempted for this RCA (None = it can)."""
    if report is None or report.status != "succeeded":
        return "no succeeded RCA report"
    if report.category not in ELIGIBLE_CATEGORIES:
        return f"RCA category is {report.category}; fixes are attempted for code and resource defects only"
    rc = (report.report or {}).get("root_cause") or {}
    if not rc.get("file"):
        return "the RCA names no file"
    file_check = next((c for c in report.checks or [] if c.get("check") == "blamed file is in the evidence"), None)
    if not file_check or not file_check.get("ok"):
        return f"the blamed file {rc.get('file')} is not grounded in the evidence"
    if service is None:
        return "service is not in the catalog"
    language = (service.config or {}).get("language") or "unknown"
    if language not in SUPPORTED_LANGUAGES:
        return f"language {language} is not supported by the fix sandbox yet"
    if not service.source_root or not Path(service.source_root).is_dir():
        return f"source root {service.source_root} is not readable"
    return None


def _read(root: Path, rel: str) -> tuple[str, str] | None:
    path = resolve_source_path(root, rel)  # never outside the source root
    if path is None:
        return None
    try:
        return path.relative_to(root.resolve()).as_posix(), path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _local_imports(rel: str, text: str, root: Path) -> list[str]:
    """Repository modules a file imports (relative or by package path), as repo-relative paths."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    package = posixpath.dirname(rel)
    out: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            base = package
            for _ in range(max(node.level - 1, 0)):
                base = posixpath.dirname(base)
            mod = (node.module or "").replace(".", "/")
            if node.level:
                names.append(posixpath.join(base, mod) if mod else base)
            elif mod:
                names.append(mod)
        elif isinstance(node, ast.Import):
            names += [a.name.replace(".", "/") for a in node.names]
        for name in names:
            candidate = f"{name}.py"
            if (root / candidate).is_file() and candidate != rel:
                out.append(candidate)
    return out


def build_fix_context(session: Session, incident: Incident, report: RcaReport, service: Service) -> FixContext:
    root = Path(service.source_root or "")
    rc = (report.report or {}).get("root_cause") or {}
    rows = session.scalars(select(Evidence).where(Evidence.incident_id == incident.id,
                                                  Evidence.collection_id == report.collection_id)).all() \
        if report.collection_id else []

    wanted = [rc["file"]]
    exception_type, exception_message, stacktrace = None, "", ""
    for e in sorted(rows, key=lambda e: -(e.relevance or 0)):
        c = e.content or {}
        if e.kind == "code":
            wanted += [x.get("file") for x in c.get("call_chain") or [] if x.get("file")]
        if e.kind == "deployment" and (e.relevance or 0) >= 0.5:
            wanted += list(c.get("changed_files") or [])
        if e.kind in ("trace", "log") and not exception_type and c.get("exception_type"):
            exception_type = c.get("exception_type")
            exception_message = c.get("exception_message") or ""
            stacktrace = c.get("stacktrace") or ""

    originals: dict[str, str] = {}
    for rel in dict.fromkeys(w for w in wanted if w):
        found = _read(root, rel)
        if found is None:
            continue
        path, text = found
        name = posixpath.basename(path)
        if path.startswith("tests/") or name.startswith("test_") or name == "conftest.py":
            continue
        originals[path] = text
    target = _read(root, rc["file"])
    if target is None or target[0] not in originals:
        raise LookupError(f"blamed file {rc['file']} is not in the source tree")

    context_files = {p: redact(t) for p, t in originals.items()}
    for path, text in list(originals.items()):
        for dep in _local_imports(path, text, root):
            if dep not in context_files:
                found = _read(root, dep)
                if found:
                    context_files[found[0]] = redact(found[1])
    tests: dict[str, str] = {}
    if (root / "tests").is_dir():
        for p in sorted((root / "tests").rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "__pycache__" not in rel:
                tests[rel] = redact(p.read_text(encoding="utf-8", errors="replace"))

    # Release-changed files and the blamed file may change; call-chain-only files (e.g. a web handler) may not.
    release = {posixpath.normpath(f) for e in rows if e.kind == "deployment" and (e.relevance or 0) >= 0.5
               for f in (e.content or {}).get("changed_files") or []}
    allowed = {p for p in originals if p == target[0] or p in release}
    return FixContext(incident=incident, service=service, report=report, target_file=target[0],
                      allowed_files=allowed, originals={p: originals[p] for p in allowed},
                      context_files=context_files, test_files=tests, exception_type=exception_type,
                      exception_message=redact(exception_message), stacktrace=redact(stacktrace)[-3000:])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n# …(truncated)"


def render_case(ctx: FixContext) -> str:
    """The shared part of both prompts: what failed, where, and the code involved."""
    data = ctx.report.report or {}
    rc = data.get("root_cause") or {}
    trigger = data.get("trigger") or {}
    parts = [
        f"INCIDENT {ctx.incident.key}: {ctx.incident.title} (service {ctx.incident.service})",
        f"RCA SUMMARY: {data.get('summary', '')}",
        "ROOT CAUSE: " + json.dumps({k: rc.get(k) for k in ("category", "file", "line", "function", "explanation")}),
        "TRIGGER: " + json.dumps({k: trigger.get(k) for k in ("kind", "release_version", "explanation")}),
        f"EXCEPTION IN PRODUCTION: {ctx.exception_type or 'none recorded'}: {ctx.exception_message}",
    ]
    if ctx.stacktrace:
        parts.append(f"STACK TRACE (redacted, end):\n{ctx.stacktrace}")
    for path, text in ctx.context_files.items():
        role = "MAY BE CHANGED" if path in ctx.allowed_files else "READ-ONLY CONTEXT"
        parts.append(f"\nFILE {path} ({role}):\n```python\n{_clip(text, MAX_SOURCE_CHARS)}\n```")
    if ctx.test_files:
        for path, text in ctx.test_files.items():
            parts.append(f"\nEXISTING TEST {path} (READ-ONLY):\n```python\n{_clip(text, MAX_TEST_CHARS)}\n```")
    else:
        parts.append("\nThere are no existing tests.")
    return "\n".join(parts)
