"""Builds what the investigator sees: the incident, its evidence (with short ids E1..En) and the source of
files changed by a release in the evidence window. Everything is already redacted at collection time;
release file contents are redacted here."""

from __future__ import annotations

import json
import pathlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Evidence, Incident, Service
from app.modules.catalog.service import find_service
from app.modules.evidence.redaction import redact
from app.modules.evidence.stacktrace import resolve_source_path

MAX_ITEM_CHARS = 3500
MAX_FILE_LINES = 220
MAX_RELEASE_FILES = 3


@dataclass
class InvestigationContext:
    incident: Incident
    service: Service | None
    collection_id: str | None
    items: list[dict]  # {"ref", "id", "kind", "source", "title", "summary", "relevance", "content"}
    release_files: list[dict] = field(default_factory=list)  # {"path", "version", "lines": [(n, text)]}

    def by_ref(self) -> dict[str, dict]:
        return {i["ref"]: i for i in self.items}

    def known_files(self) -> set[str]:
        files = {f["path"] for f in self.release_files}
        for i in self.items:
            c = i["content"] or {}
            if i["kind"] == "code":
                files.add(c.get("file"))
                files.update(x.get("file") for x in c.get("call_chain") or [])
            if i["kind"] == "deployment":
                files.update(c.get("changed_files") or [])
        return {f for f in files if f}

    def file_length(self, path: str) -> int | None:
        for f in self.release_files:
            if f["path"] == path:
                return f["total_lines"]
        return None


def latest_collection_id(session: Session, incident: Incident) -> str | None:
    row = session.scalars(select(Evidence.collection_id).where(Evidence.incident_id == incident.id)
                          .order_by(Evidence.collected_at.desc()).limit(1)).first()
    return str(row) if row else None


def build_context(session: Session, incident: Incident) -> InvestigationContext:
    collection_id = latest_collection_id(session, incident)
    rows = []
    if collection_id:
        rows = session.scalars(select(Evidence).where(Evidence.incident_id == incident.id,
                                                      Evidence.collection_id == uuid.UUID(collection_id))).all()
    order = {"code": 0, "trace": 1, "log": 2, "dependency": 3, "metric": 4, "deployment": 5, "gap": 6}
    rows = sorted(rows, key=lambda e: (order.get(e.kind, 9), -(e.relevance or 0)))
    items = [{"ref": f"E{n}", "id": str(e.id), "kind": e.kind, "source": e.source, "title": e.title,
              "summary": e.summary, "relevance": e.relevance, "content": e.content or {}}
             for n, e in enumerate(rows, start=1)]
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    ctx = InvestigationContext(incident=incident, service=service, collection_id=collection_id, items=items)
    ctx.release_files = _release_files(ctx)
    return ctx


def _release_files(ctx: InvestigationContext) -> list[dict]:
    """Source of files changed by releases shown in the evidence, so the investigator can look for the
    defect where it was introduced, not only where it surfaced."""
    if ctx.service is None or not ctx.service.source_root:
        return []
    root = pathlib.Path(ctx.service.source_root)
    if not root.is_dir():
        return []
    wanted: list[tuple[str, str]] = []
    for i in ctx.items:
        if i["kind"] == "deployment" and (i["relevance"] or 0) >= 0.5:
            for f in i["content"].get("changed_files") or []:
                if (f, i["content"].get("version")) not in wanted:
                    wanted.append((f, i["content"].get("version")))
    out = []
    for rel, version in wanted[:MAX_RELEASE_FILES]:
        path = resolve_source_path(root, rel)  # never outside the source root
        if path is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        out.append({"path": rel, "version": version, "total_lines": len(lines),
                    "lines": [(n, redact(t)) for n, t in enumerate(lines[:MAX_FILE_LINES], start=1)]})
    return out


def _compact(content: dict) -> str:
    c = {k: v for k, v in content.items() if k not in ("series", "trace_ids", "span_attributes")}
    text = json.dumps(c, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_ITEM_CHARS else text[:MAX_ITEM_CHARS] + "…(truncated)"


def render_for_prompt(ctx: InvestigationContext) -> str:
    inc = ctx.incident
    parts = [
        f"INCIDENT {inc.key}: {inc.title}",
        f"service={inc.service} environment={inc.environment} severity={inc.severity} "
        f"detected_at={inc.detected_at.isoformat()}",
    ]
    if ctx.service is not None:
        cfg = ctx.service.config or {}
        parts.append(f"language={cfg.get('language', 'unknown')} dependencies={ctx.service.dependencies or []}")
    parts.append("\nEVIDENCE (cite by id; gaps are sources that had nothing or could not be read):")
    for i in ctx.items:
        parts.append(f"\n[{i['ref']}] kind={i['kind']} source={i['source']} relevance={i['relevance']}\n"
                     f"title: {i['title']}\nsummary: {i['summary']}\ndata: {_compact(i['content'])}")
    for f in ctx.release_files:
        body = "\n".join(f"{n:4d}  {t}" for n, t in f["lines"])
        more = "" if f["total_lines"] <= MAX_FILE_LINES else f"\n… ({f['total_lines'] - MAX_FILE_LINES} more lines)"
        parts.append(f"\nSOURCE OF {f['path']} (changed in release {f['version']}):\n{body}{more}")
    return "\n".join(parts)
