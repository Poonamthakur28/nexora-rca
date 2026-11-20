"""The `fix.propose` job: from a grounded RCA to a verified patch, or a clear reason why not.

Stages (each recorded in the Pipeline view):
  context       eligible RCA, files the patch may change, read-only context and existing tests
  baseline      the existing tests pass on the current code (otherwise regressions cannot be checked)
  repro         the provider writes one pytest test; plain code checks it is safe
  repro_fails   Gate 1: the test FAILS on the current code, with the exception seen in production
  patch         the provider writes a unified diff
  patch_checks  only allowed files, no tests, size limit, applies by exact context, still parses
  verify        Gate 2: the test PASSES with the patch, and the existing tests still pass
  artifacts     patch, test, sandbox log and PR text written to disk
  pr            optional draft PR (GITHUB_TOKEN); never merged
Each provider stage gets at most one retry, with the failure fed back. The job itself is never retried.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import FixAttempt, Incident, IncidentEvent, Job, RcaReport, Service
from app.jobs.queue import enqueue
from app.modules.catalog.service import find_service
from app.modules.pipeline.recorder import PipelineRecorder
from app.modules.rca.engine import extract_json
from app.modules.rca.providers import LLMClient, ProviderError, make_client

from .adapters import GitHubDraftPr, PrError, PrRequest, write_artifacts
from .context import REPRO_TEST_PATH, FixContext, build_fix_context, eligibility, render_case
from .patching import PatchError, apply_file_diff, canonical_diff, check_test_safety, parse_unified_diff, validate_scope
from .sandbox import RunResult, SandboxError, Workspace, run_pytest
from .schema import PATCH_SHAPE, REPRO_SHAPE, PatchProposal, ReproTest

JOB_TYPE = "fix.propose"
PENDING = ("queued", "running")
TERMINAL = ("skipped", "repro_failed", "patch_rejected", "verify_failed", "verified", "pr_opened", "failed")
NEEDS_LLM = ("the fix track needs an LLM provider (set FIX_PROVIDER or RCA_PROVIDER to anthropic, openai or "
             "ollama); the rule-based baseline cannot write tests or patches")

REPRO_SYSTEM = f"""You are Nexora's fix engineer. Write ONE pytest test that reproduces the production defect
described by the incident's root-cause analysis, so the test FAILS on the current code for the same reason
production failed, and PASSES once the defect is fixed.

Rules:
- The test is saved as {REPRO_TEST_PATH} and run with `python -m pytest {REPRO_TEST_PATH}` from the
  repository root, which is on sys.path. Import application code by its path in the repository
  (for a file app/pricing.py: `from app.pricing import ...`).
- Recreate the production condition (inputs, configuration or runtime flags that were active) using only the
  code you are shown. Restore any global state you change (try/finally or a fixture).
- Assert the correct behaviour. Do not catch the production exception to make the test pass.
- Standard library, pytest and the repository's own modules only. No network, subprocesses, file writes
  outside pytest's tmp_path, or sleeps longer than a few seconds.
- Output ONLY one JSON object, no prose and no code fences:
{REPRO_SHAPE}"""

PATCH_SYSTEM = f"""You are Nexora's fix engineer. Write the smallest code change that fixes the root cause, so
the reproduction test passes and the existing tests keep passing.

Rules:
- Change only files marked MAY BE CHANGED. Never change tests or files marked READ-ONLY CONTEXT, and never
  create, delete or rename files.
- Fix the defect itself. Do not hide the exception (no broad try/except), do not special-case the test's
  inputs, and do not switch off the condition the test sets up.
- Return a unified diff with paths relative to the repository root (--- a/<path> and +++ b/<path>), and
  context lines copied exactly from the file shown.
- Output ONLY one JSON object, no prose and no code fences:
{PATCH_SHAPE}"""


# ---------------------------------------------------------------------------------------- queueing

def fix_settings(settings: Settings) -> Settings:
    """The provider settings used for fixes: FIX_PROVIDER/FIX_MODEL, else the RCA provider."""
    provider = settings.fix_provider or settings.rca_provider
    model = settings.fix_model or (settings.rca_model if provider == settings.rca_provider else "")
    return settings.model_copy(update={"rca_provider": provider, "rca_model": model,
                                       "rca_max_output_tokens": settings.fix_max_output_tokens})


def latest_report(session: Session, incident: Incident) -> RcaReport | None:
    return session.scalars(select(RcaReport).where(
        RcaReport.organization_id == incident.organization_id, RcaReport.incident_id == incident.id,
        RcaReport.status == "succeeded").order_by(RcaReport.created_at.desc()).limit(1)).first()


def pending_attempt(session: Session, incident: Incident) -> FixAttempt | None:
    return session.scalars(select(FixAttempt).where(
        FixAttempt.organization_id == incident.organization_id, FixAttempt.incident_id == incident.id,
        FixAttempt.status.in_(PENDING)).limit(1)).first()


def maybe_enqueue_fix(session: Session, settings: Settings, incident: Incident, report: RcaReport | None = None,
                      requested_by: str = "auto") -> tuple[FixAttempt | None, str | None]:
    """Queue a fix attempt for the incident's latest RCA. Returns (attempt, reason it was not queued).

    Automatic requests run once per RCA report; a person can ask again for the same report."""
    if not settings.fix_enabled:
        return None, "the fix track is disabled (FIX_ENABLED=false)"
    if fix_settings(settings).rca_provider == "heuristic":
        return None, NEEDS_LLM
    report = report or latest_report(session, incident)
    service = find_service(session, incident.organization_id, incident.service, incident.environment)
    reason = eligibility(report, service)
    if reason:
        return None, reason
    if pending_attempt(session, incident):
        return None, "a fix attempt is already queued or running"
    if requested_by == "auto" and session.scalars(select(FixAttempt.id).where(
            FixAttempt.rca_report_id == report.id).limit(1)).first():
        return None, "a fix was already attempted for this RCA report"
    rec = PipelineRecorder.start(session, incident.organization_id, "fix_proposal", f"Fix for {incident.key}",
                                 trigger_id=str(incident.id))
    rec.run.incident_id = incident.id
    attempt = FixAttempt(organization_id=incident.organization_id, incident_id=incident.id,
                         rca_report_id=report.id, run_id=rec.run.id, status="queued", requested_by=requested_by)
    session.add(attempt)
    session.flush()
    rec.step("queued", "Fix queued", "success",
             f"{'after RCA' if requested_by == 'auto' else f'requested by {requested_by}'} for {incident.key}")
    enqueue(session, incident.organization_id, JOB_TYPE, {"attempt_id": str(attempt.id), "run_id": str(rec.run.id)},
            max_attempts=1)
    return attempt, None


# ---------------------------------------------------------------------------------------- the job

class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class _FixRun:
    def __init__(self, session: Session, settings: Settings, attempt: FixAttempt, incident: Incident,
                 rec: PipelineRecorder, http: httpx.Client):
        self.session, self.settings, self.attempt, self.incident, self.rec, self.http = (
            session, settings, attempt, incident, rec, http)
        self.gates: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.client: LLMClient | None = None

    # -- bookkeeping
    def gate(self, name: str, ok: bool, detail: str) -> None:
        self.gates.append({"gate": name, "ok": ok, "detail": detail[:500]})
        self.attempt.gates = list(self.gates)

    def record(self, result: RunResult) -> RunResult:
        self.runs[result.name] = result.to_dict()
        self.attempt.runs = dict(self.runs)
        return result

    def save(self) -> None:
        self.session.commit()

    # -- provider
    def ask(self, system: str, user: str, shape: type[ReproTest] | type[PatchProposal]):
        assert self.client is not None
        try:
            result = self.client.complete(system, user)
        except ProviderError as exc:
            raise _Stop("failed", f"fix provider error: {exc}") from exc
        a = self.attempt
        a.llm_calls += 1
        a.model = result.model or a.model
        if result.input_tokens is not None:
            a.input_tokens = (a.input_tokens or 0) + result.input_tokens
        if result.output_tokens is not None:
            a.output_tokens = (a.output_tokens or 0) + result.output_tokens
        try:
            return shape.model_validate(extract_json(result.text))
        except (ValueError, ValidationError) as exc:
            raise ValueError("answer did not match the required JSON shape: "
                             + str(exc).replace("\n", " ")[:400]) from exc

    def sandbox(self, name: str, ctx: FixContext, files: dict[str, str], targets: list[str],
                ignore: list[str] | None = None) -> RunResult:
        try:
            with Workspace(ctx.service.source_root or "") as ws:
                for path, text in files.items():
                    ws.write(path, text)
                return self.record(run_pytest(ws, self.settings, name, targets, ignore))
        except SandboxError as exc:
            raise _Stop("failed", f"sandbox: {exc}") from exc

    # -- stages
    def reproduce(self, ctx: FixContext) -> tuple[ReproTest, RunResult]:
        case = render_case(ctx)
        feedback = ""
        for n in (1, 2):
            try:
                repro = self.ask(REPRO_SYSTEM, case + feedback, ReproTest)
            except ValueError as exc:
                feedback = f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {exc}. Return only the JSON object."
                continue
            problems = check_test_safety(repro.test_code)
            self.attempt.repro_test_path, self.attempt.repro_test_code = REPRO_TEST_PATH, repro.test_code
            if problems:
                self.gate("repro_test_safe", False, "; ".join(problems))
                feedback = (f"\n\nYOUR PREVIOUS TEST WAS REJECTED: {'; '.join(problems)}.\n"
                            f"Previous test:\n```python\n{repro.test_code}\n```")
                continue
            self.gate("repro_test_safe", True, "no network, process or file-system calls")
            self.rec.step("repro", "Reproduction test written", "success", f"provider call {self.attempt.llm_calls}")
            self.save()
            run = self.sandbox("repro_before", ctx, {REPRO_TEST_PATH: repro.test_code}, [REPRO_TEST_PATH])
            expected = ctx.exception_type
            seen = expected is None or expected in run.output
            if run.tests_failed and seen:
                self.gate("repro_fails_before", True,
                          f"fails on the current code: {run.summary()}"
                          + (f", raising {expected} as in production" if expected else ""))
                return repro, run
            why = ("it passed, so it does not reproduce the defect" if run.all_passed
                   else "it timed out" if run.timed_out
                   else f"it could not run ({run.summary()})" if not run.tests_failed
                   else f"it failed, but not with {expected}")
            self.gate("repro_fails_before", False, f"attempt {n}: {why}")
            feedback = (f"\n\nYOUR PREVIOUS TEST DID NOT REPRODUCE THE FAILURE: {why}. It must fail on the current "
                        f"code{f' with {expected}' if expected else ''}.\nPrevious test:\n```python\n"
                        f"{repro.test_code}\n```\npytest output:\n{run.output[-2500:]}")
        raise _Stop("repro_failed", "no reproduction test failed the way production did: "
                    + next((g["detail"] for g in reversed(self.gates) if not g["ok"]), "the provider gave no usable test"))

    def fix(self, ctx: FixContext, repro: ReproTest, before: RunResult) -> tuple[PatchProposal, dict[str, str]]:
        base = (render_case(ctx) + f"\n\nREPRODUCTION TEST {REPRO_TEST_PATH} (fails now, must pass after your fix):\n"
                f"```python\n{repro.test_code}\n```\nIts output on the current code:\n{before.output[-2500:]}")
        feedback, status, last = "", "patch_rejected", ""
        for n in (1, 2):
            try:
                proposal = self.ask(PATCH_SYSTEM, base + feedback, PatchProposal)
                diffs = parse_unified_diff(proposal.diff)
                problems = validate_scope(diffs, ctx.allowed_files, self.settings.fix_max_patch_lines)
                if problems:
                    raise PatchError("; ".join(problems))
                patched = {d.path: apply_file_diff(ctx.originals[d.path], d) for d in diffs}
                for path, text in patched.items():
                    try:
                        ast.parse(text)
                    except SyntaxError as exc:
                        raise PatchError(f"{path} does not parse after the patch: {exc.msg} (line {exc.lineno})")
            except (ValueError, PatchError) as exc:
                status, last = "patch_rejected", str(exc)
                self.gate("patch_checks", False, f"attempt {n}: {exc}")
                feedback = f"\n\nYOUR PREVIOUS PATCH WAS REJECTED: {exc}"
                continue
            changed = sum(d.changed_lines() for d in diffs)
            self.gate("patch_checks", True,
                      f"{', '.join(patched)}: {changed} changed line(s), applies cleanly, parses")
            self.rec.step("patch", "Patch written", "success", f"provider call {self.attempt.llm_calls}")
            self.save()
            files = {**patched, REPRO_TEST_PATH: repro.test_code}
            after = self.sandbox("repro_after", ctx, files, [REPRO_TEST_PATH])
            regress = self.sandbox("baseline_after", ctx, files, ["tests"], ignore=[REPRO_TEST_PATH])
            self.gate("repro_passes_after", after.all_passed, f"with the patch: {after.summary()}")
            self.gate("baseline_passes_after", regress.all_passed, f"existing tests with the patch: {regress.summary()}")
            if after.all_passed and regress.all_passed:
                return proposal, patched
            status = "verify_failed"
            last = f"reproduction test {after.summary()}; existing tests {regress.summary()}"
            feedback = (f"\n\nYOUR PREVIOUS PATCH DID NOT VERIFY: {last}.\nPrevious diff:\n{proposal.diff}\n"
                        f"Reproduction test output:\n{after.output[-1500:]}\nExisting tests output:\n"
                        f"{regress.output[-1500:]}")
        raise _Stop(status, f"no verified patch: {last}")


def _event(session: Session, incident: Incident, kind: str, message: str) -> None:
    incident.updated_at = datetime.now(timezone.utc)
    session.add(IncidentEvent(organization_id=incident.organization_id, incident_id=incident.id, kind=kind,
                              message=message[:1000]))


def _pr_body(run: _FixRun, ctx: FixContext, proposal: PatchProposal, folder: str) -> str:
    data = ctx.report.report or {}
    gates = "\n".join(f"- {'✅' if g['ok'] else '❌'} {g['gate']}: {g['detail']}" for g in run.gates)
    return (f"## Nexora fix for {run.incident.key}\n\n{run.incident.title}\n\n"
            f"**Root cause** ({ctx.report.category}, confidence {round((ctx.report.confidence or 0) * 100)}%): "
            f"{data.get('summary', '')}\n\n**Change:** {proposal.explanation}\n\n"
            f"**Reproduction test:** `{REPRO_TEST_PATH}` fails on the current code and passes with this change.\n\n"
            f"### Checks\n{gates}\n\nGenerated by Nexora (artifacts `{folder}`). Draft: a person reviews and merges.\n")


def run_fix(session: Session, settings: Settings, attempt_id: str, run_id: str | None = None,
            http: httpx.Client | None = None) -> dict:
    attempt = session.get(FixAttempt, uuid.UUID(attempt_id))
    if attempt is None:
        raise LookupError(f"fix attempt {attempt_id} not found")
    if attempt.status not in PENDING:
        return {"status": attempt.status, "skipped": True}  # idempotent: already finished
    incident = session.get(Incident, attempt.incident_id)
    report = session.get(RcaReport, attempt.rca_report_id) if attempt.rca_report_id else None
    rec = PipelineRecorder.resume(session, uuid.UUID(run_id)) if run_id else None
    if rec is None:
        rec = PipelineRecorder.start(session, attempt.organization_id, "fix_proposal", f"Fix for {incident.key}",
                                     trigger_id=str(incident.id))
        attempt.run_id = rec.run.id
    rec.run.incident_id = incident.id
    attempt.status = "running"
    session.commit()

    owns = http is None
    client = http or httpx.Client()
    run = _FixRun(session, settings, attempt, incident, rec, client)
    stage = "context"
    try:
        fset = fix_settings(settings)
        attempt.provider = fset.rca_provider
        if fset.rca_provider == "heuristic":
            raise _Stop("skipped", NEEDS_LLM)
        service: Service | None = find_service(session, incident.organization_id, incident.service,
                                               incident.environment)
        reason = eligibility(report, service)
        if reason:
            raise _Stop("skipped", reason)
        try:
            ctx = build_fix_context(session, incident, report, service)
        except LookupError as exc:
            raise _Stop("skipped", str(exc)) from exc
        attempt.target_file = ctx.target_file
        run.client = make_client(fset, client)
        attempt.model = run.client.model
        rec.step("context", "Fix context", "success",
                 f"target {ctx.target_file}; may change {', '.join(sorted(ctx.allowed_files))}; "
                 f"{len(ctx.context_files)} source file(s), {len(ctx.test_files)} existing test file(s); "
                 f"{fset.rca_provider}/{run.client.model}")
        run.save()

        stage = "baseline"
        if not [p for p in ctx.test_files if p != REPRO_TEST_PATH]:
            run.gate("baseline_passes_before", False, "no existing tests")
            raise _Stop("verify_failed", "the service has no existing tests, so regressions cannot be checked")
        base = run.sandbox("baseline_before", ctx, {}, ["tests"], ignore=[REPRO_TEST_PATH])
        run.gate("baseline_passes_before", base.all_passed, f"existing tests on the current code: {base.summary()}")
        if not base.all_passed:
            raise _Stop("failed", f"the existing tests do not pass on the current code ({base.summary()}), "
                                  "so a fix could not be verified")
        rec.step("baseline", "Existing tests pass", "success", base.summary())
        run.save()

        stage = "repro"
        repro, before = run.reproduce(ctx)
        rec.step("repro_fails", "Gate 1: test fails now", "success", before.summary())
        run.save()

        stage = "patch"
        proposal, patched = run.fix(ctx, repro, before)
        rec.step("patch_checks", "Patch checks", "success", f"{', '.join(patched)}")
        rec.step("verify", "Gate 2: test passes with the fix", "success",
                 f"reproduction {run.runs['repro_after']['summary']}; existing {run.runs['baseline_after']['summary']}")

        stage = "artifacts"
        diff = "".join(canonical_diff(p, ctx.originals[p], t) for p, t in patched.items())
        diff += canonical_diff(REPRO_TEST_PATH, "", repro.test_code)
        attempt.patch_diff = diff
        attempt.patch_files = sorted(patched) + [REPRO_TEST_PATH]
        attempt.explanation = proposal.explanation
        folder = f"{incident.key}-{str(attempt.id)[:8]}"
        cfg = ctx.service.config or {}
        repo_path = cfg.get("repository_path") or ""
        body = _pr_body(run, ctx, proposal, folder)
        apply_hint = f"git apply --directory={repo_path} fix.patch" if repo_path else "git apply fix.patch"
        log = "\n\n".join(f"== {name}: {r['summary']}\n{r['output']}" for name, r in run.runs.items())
        out = write_artifacts(settings, folder, {
            "fix.patch": diff, "test_nexora_repro.py": repro.test_code, "sandbox.log": log,
            "PR.md": body + f"\nApply locally from the repository root: `{apply_hint}`\n",
            "summary.json": json.dumps({"incident": incident.key, "status": "verified", "gates": run.gates,
                                        "files": attempt.patch_files}, indent=2),
        })
        attempt.artifacts_dir = str(out)
        attempt.status = "verified"
        rec.step("artifacts", "Patch files written", "success", str(out))
        run.save()

        stage = "pr"
        if settings.github_token:
            try:
                attempt.pr_url = GitHubDraftPr(settings, client).open(PrRequest(
                    repository_url=ctx.service.repository_url or "", repository_path=repo_path,
                    base_branch=cfg.get("repository_branch") or "main",
                    branch=f"nexora/fix-{incident.key.lower()}-{str(attempt.id)[:8]}",
                    title=f"Fix {incident.key}: {ctx.target_file}", body=body,
                    originals={p: ctx.originals[p] for p in patched},
                    changes={**patched, REPRO_TEST_PATH: repro.test_code}))
                attempt.status = "pr_opened"
                run.gate("draft_pr", True, attempt.pr_url)
                rec.step("pr", "Draft PR opened", "success", attempt.pr_url)
            except PrError as exc:
                run.gate("draft_pr", False, str(exc))
                rec.step("pr", "Draft PR not opened", "warning", str(exc))
        else:
            rec.step("pr", "Draft PR", "skipped", "GITHUB_TOKEN is not set; patch files only")

        where = f"draft PR {attempt.pr_url}" if attempt.pr_url else f"patch files in {out}"
        _event(session, incident, "fix_verified",
               f"Verified fix for {', '.join(sorted(patched))}: reproduction test fails before and passes after, "
               f"existing tests pass. See {where}.")
        attempt.reason = ""
    except _Stop as stop:
        attempt.status, attempt.reason = stop.status, stop.reason
        level = "warning" if stop.status == "skipped" else "failed"
        rec.step(stage, {"context": "Fix context", "baseline": "Existing tests", "repro": "Gate 1: reproduce",
                         "patch": "Patch / Gate 2", "artifacts": "Patch files", "pr": "Draft PR"}.get(stage, stage),
                 level, stop.reason)
        _event(session, incident, "fix_skipped" if stop.status == "skipped" else "fix_failed",
               f"Fix {stop.status.replace('_', ' ')}: {stop.reason}")
    finally:
        if owns:
            client.close()
    attempt.finished_at = datetime.now(timezone.utc)
    rec.finish()
    session.commit()
    return {"status": attempt.status, "reason": attempt.reason, "pr_url": attempt.pr_url}


def record_fix_failure(session: Session, payload: dict, error: str, final: bool) -> None:
    attempt = session.get(FixAttempt, uuid.UUID(payload["attempt_id"])) if payload.get("attempt_id") else None
    if attempt is not None and attempt.status in PENDING:
        attempt.status, attempt.reason = "failed", f"unexpected error: {error[:300]}"
        attempt.finished_at = datetime.now(timezone.utc)
    if payload.get("run_id"):
        rec = PipelineRecorder.resume(session, uuid.UUID(payload["run_id"]))
        if rec:
            rec.step("processing", "Fix failed", "failed", error[:300])
            rec.finish("failed")
    session.commit()


def maybe_enqueue_after_rca(session: Session, settings: Settings, incident: Incident) -> Job | None:
    """Called when an investigation succeeds; quietly does nothing when the RCA is not eligible."""
    attempt, _ = maybe_enqueue_fix(session, settings, incident)
    if attempt is None:
        return None
    return session.scalars(select(Job).where(Job.type == JOB_TYPE).order_by(Job.created_at.desc()).limit(1)).first()
