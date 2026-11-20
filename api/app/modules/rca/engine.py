"""The `rca.investigate` job: evidence in, a validated and grounded RCA report out.

Steps (each recorded in the Pipeline view):
  context    gather the latest evidence and the source of files changed by a release
  model      ask the provider (or the rule-based baseline) for a report in a fixed JSON shape
  validate   parse and validate it; one retry with the validation error if it does not fit
  grounding  check every claim points at real evidence: cited ids exist, the blamed file and line exist,
             a blamed release is in the evidence. Ungrounded claims lower confidence, they are not hidden.
  stored     save the report; the incident timeline gets one line
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Incident, IncidentEvent, Job, RcaReport
from app.jobs.queue import enqueue
from app.modules.pipeline.recorder import PipelineRecorder

from . import heuristic
from .context import InvestigationContext, build_context, render_for_prompt
from .providers import ProviderError, make_client
from .schema import SCHEMA_TEXT, RcaReportModel

JOB_TYPE = "rca.investigate"

SYSTEM_PROMPT = f"""You are Nexora's incident investigator. You find the root cause of a production incident
from the evidence you are given, the way a careful senior SRE would.

Rules:
- Use only the evidence and source code provided. Cite evidence by id (E1, E2, ...) in evidence_refs and in
  each hypothesis. Never invent files, lines, versions, metrics or services.
- Separate the symptom from the cause. The place an error is raised (e.g. a timeout) is not always where the
  defect is. When source of a changed file is provided, read it and point at the actual defect line.
- A slow or failing downstream dependency with no exception in our code is a dependency cause, not a code bug.
- If a release in the window changed the failing code, say so in trigger. If no release is in the evidence,
  do not blame one.
- Consider at least two hypotheses; mark the one you accept and why the others are rejected or still open.
- confidence is 0..1: 0.8+ only when the cause, its location and its trigger are all directly supported.
  Lower it when evidence is missing (gaps) or contradicts.
- mitigation_options are suggestions for a human or a later safety check; prefer the least risky.
- Output ONLY one JSON object, no prose and no code fences, matching exactly this shape:
{SCHEMA_TEXT}"""


def maybe_enqueue_investigation(session: Session, settings: Settings, incident: Incident,
                                reason: str) -> Job | None:
    """Queue an investigation unless one is already waiting for this incident (it will read the latest
    evidence when it runs). One that is already running used older evidence, so a new one is queued."""
    if not settings.rca_enabled:
        return None
    pending = session.scalars(select(Job).where(Job.type == JOB_TYPE, Job.status == "queued")).all()
    if any(j.payload.get("incident_id") == str(incident.id) for j in pending):
        return None
    rec = PipelineRecorder.start(session, incident.organization_id, "rca_investigation",
                                 f"RCA for {incident.key}", trigger_id=str(incident.id))
    rec.run.incident_id = incident.id
    rec.step("queued", "Investigation queued", "success", reason)
    return enqueue(session, incident.organization_id, JOB_TYPE,
                   {"incident_id": str(incident.id), "run_id": str(rec.run.id)})


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the response")
    return json.loads(t[start:end + 1])


def ground(report: RcaReportModel, ctx: InvestigationContext) -> tuple[list[dict], float]:
    """Check the report against the evidence. Returns (checks, adjusted confidence)."""
    checks: list[dict] = []
    refs = ctx.by_ref()
    conf = report.confidence

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    cited = set(report.evidence_refs)
    for h in report.hypotheses:
        cited.update(h.supporting + h.contradicting)
    unknown = sorted(r for r in cited if r not in refs)
    add("cited evidence exists", not unknown, f"unknown ids: {', '.join(unknown)}" if unknown
        else f"{len(report.evidence_refs)} cited")
    real = [r for r in report.evidence_refs if r in refs and refs[r]["kind"] != "gap"]
    add("root cause cites evidence", bool(real) or report.root_cause.category == "unknown",
        ", ".join(real) or "nothing cited")
    if not real and report.root_cause.category != "unknown":
        conf = min(conf, 0.3)

    rc = report.root_cause
    if rc.file:
        known = ctx.known_files()
        ok = rc.file in known
        add("blamed file is in the evidence", ok, rc.file if ok else f"{rc.file} not among {sorted(known) or 'none'}")
        if not ok:
            conf = min(conf, 0.3)
        elif rc.line is not None:
            length = ctx.file_length(rc.file)
            code_lines = {i["content"].get("line") for i in ctx.items if i["kind"] == "code"
                          and i["content"].get("file") == rc.file}
            line_ok = (length is not None and rc.line <= length) or rc.line in code_lines
            add("blamed line exists", line_ok, f"{rc.file}:{rc.line}")
            if not line_ok:
                conf = min(conf, 0.4)
    if report.trigger.kind == "release":
        versions = {i["content"].get("version") for i in ctx.items if i["kind"] == "deployment"}
        versions |= {i["content"].get("deployment_version") for i in ctx.items if i["kind"] == "code"}
        ok = report.trigger.release_version in versions
        add("blamed release is in the evidence", ok, report.trigger.release_version or "no version given")
        if not ok:
            conf = min(conf, 0.4)
    if rc.category == "dependency":
        peers = {i["content"].get("peer") for i in ctx.items if i["kind"] == "dependency"}
        ok = any(p and p in rc.component for p in peers)
        add("blamed dependency is in the evidence", ok, rc.component)
        if not ok:
            conf = min(conf, 0.4)
    unavailable = [i["title"] for i in ctx.items if i["kind"] == "gap" and "unavailable" in i["title"]]
    if unavailable:
        conf = min(conf, 0.7)
        add("all evidence sources were readable", False, "; ".join(unavailable))
    return checks, round(conf, 2)


def investigate(session: Session, settings: Settings, incident_id: str, run_id: str | None = None,
                http: httpx.Client | None = None) -> dict:
    incident = session.get(Incident, uuid.UUID(incident_id))
    if incident is None:
        raise LookupError(f"incident {incident_id} not found")
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = PipelineRecorder.start(session, incident.organization_id, "rca_investigation",
                                     f"RCA for {incident.key}", trigger_id=str(incident.id))
    rec.run.incident_id = incident.id

    ctx = build_context(session, incident)
    real = [i for i in ctx.items if i["kind"] != "gap"]
    rec.step("context", "Evidence gathered", "success" if real else "warning",
             f"{len(real)} item(s), {len(ctx.items) - len(real)} gap(s), "
             f"{len(ctx.release_files)} changed file(s) attached")

    provider = settings.rca_provider
    row = RcaReport(organization_id=incident.organization_id, incident_id=incident.id,
                    collection_id=uuid.UUID(ctx.collection_id) if ctx.collection_id else None,
                    run_id=rec.run.id, provider=provider, model="", status="failed")
    t0 = time.perf_counter()
    owns = http is None
    client_http = http or httpx.Client()
    try:
        report, model, usage, attempts = _ask(settings, ctx, client_http, rec)
        row.model = model
        row.input_tokens, row.output_tokens = usage
    except (ProviderError, ValueError) as exc:
        row.error = str(exc)[:1000]
        row.latency_ms = int((time.perf_counter() - t0) * 1000)
        session.add(row)
        _event(session, incident, "rca_failed", f"Investigation failed ({provider}): {row.error[:200]}")
        rec.finish("failed")
        session.commit()
        return {"status": "failed", "error": row.error}
    finally:
        if owns:
            client_http.close()
    row.latency_ms = int((time.perf_counter() - t0) * 1000)

    checks, confidence = ground(report, ctx)
    failed = [c for c in checks if not c["ok"]]
    rec.step("grounding", "Claims checked against evidence", "warning" if failed else "success",
             "; ".join(f"{c['check']}: {c['detail']}" for c in failed) if failed
             else f"{len(checks)} check(s) passed")

    data = report.model_dump()
    data["model_confidence"] = report.confidence
    data["confidence"] = confidence
    data["evidence_index"] = {i["ref"]: {"id": i["id"], "kind": i["kind"], "title": i["title"]} for i in ctx.items}
    data["attempts"] = attempts
    row.report = data
    row.checks = checks
    row.status = "succeeded"
    row.category = report.root_cause.category
    row.confidence = confidence
    row.grounded = not failed
    session.add(row)
    rc = report.root_cause
    where = f" at {rc.file}:{rc.line}" if rc.file and rc.line else f" in {rc.file}" if rc.file else ""
    _event(session, incident, "rca_completed",
           f"Root cause ({rc.category}{where}, confidence {round(confidence * 100)}%): {report.summary}")
    rec.step("stored", "Report stored", "success",
             f"{rc.category}{where}; confidence {round(confidence * 100)}% via {provider}"
             + (f"/{row.model}" if row.model else ""))
    rec.finish()
    session.flush()
    from app.modules.mitigation.policy import maybe_enqueue_plan  # local import: mitigation depends on rca

    maybe_enqueue_plan(session, settings, incident)
    session.commit()
    return {"status": "succeeded", "category": rc.category, "confidence": confidence}


def _ask(settings: Settings, ctx: InvestigationContext, http: httpx.Client,
         rec: PipelineRecorder) -> tuple[RcaReportModel, str, tuple[int | None, int | None], int]:
    if settings.rca_provider == "heuristic":
        report = RcaReportModel.model_validate(heuristic.investigate(ctx))
        rec.step("model", "Rule-based baseline", "success", "no LLM configured (RCA_PROVIDER=heuristic)")
        rec.step("validate", "Report validated", "success", "schema ok")
        return report, "rules-v1", (None, None), 1

    client = make_client(settings, http)
    user = render_for_prompt(ctx)
    messages_user = user
    usage_in = usage_out = 0
    last_error = ""
    for attempt in (1, 2):
        try:
            result = client.complete(SYSTEM_PROMPT, messages_user)
        except ProviderError as exc:
            rec.step("model", f"{client.name} / {client.model}", "failed", str(exc))
            raise
        usage_in += result.input_tokens or 0
        usage_out += result.output_tokens or 0
        rec.step("model", f"{client.name} / {result.model}", "success",
                 f"attempt {attempt}: {result.input_tokens or '?'} in / {result.output_tokens or '?'} out tokens")
        try:
            report = RcaReportModel.model_validate(extract_json(result.text))
            rec.step("validate", "Report validated", "success", "schema ok")
            return report, result.model, (usage_in or None, usage_out or None), attempt
        except (ValueError, ValidationError) as exc:
            last_error = str(exc).replace("\n", " ")[:600]
            rec.step("validate", "Report validated", "failed" if attempt == 2 else "warning",
                     f"attempt {attempt}: {last_error}")
            messages_user = (f"{user}\n\nYour previous answer did not match the required JSON shape: "
                             f"{last_error}\nReturn only the corrected JSON object.")
    raise ValueError(f"model output did not match the report schema after 2 attempts: {last_error}")


def _event(session: Session, incident: Incident, kind: str, message: str) -> None:
    incident.updated_at = datetime.now(timezone.utc)
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id, kind=kind,
                              message=message[:1000]))


def record_investigation_failure(session: Session, run_id: str | None, error: str, final: bool) -> None:
    if not run_id:
        return
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id))
    if rec:
        rec.step("processing", "Investigation failed" + ("" if final else ", will retry"), "failed", error[:300])
        if final:
            rec.finish("failed")
    session.commit()
