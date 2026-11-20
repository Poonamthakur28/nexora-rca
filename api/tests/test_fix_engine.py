"""Step 6 end to end: the fix job against the real shop-demo source and sandbox, with a mocked provider."""

import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Evidence, FixAttempt, Incident, IncidentEvent, Job, PipelineRun, RcaReport, Service
from app.modules.fix.adapters import GitHubDraftPr, PrError, PrRequest
from app.modules.fix.engine import maybe_enqueue_fix, run_fix
from tests.conftest import drain

SHOP_DEMO = Path(__file__).resolve().parents[2] / "shop-demo"
PRICING = (SHOP_DEMO / "app" / "pricing.py").read_text(encoding="utf-8")

REPRO = """from app import bugs
from app.pricing import calculate_total


def test_unknown_region_gets_default_tax_when_rule_lookup_returns_none():
    bugs.set_enabled("code_null_tax", True)
    try:
        assert calculate_total(100.0, "TN")["total"] == 118.0
    finally:
        bugs.set_enabled("code_null_tax", False)
"""
NOT_A_REPRO = "from app.pricing import calculate_total\n\ndef test_ok():\n    assert calculate_total(100.0, 'DL')['total'] == 112.0\n"
GOOD_FIX = """--- a/app/pricing.py
+++ b/app/pricing.py
@@ -1,3 +1,3 @@
     rule = TAX_RULES.get(region)
-    if rule is None and not is_enabled("code_null_tax"):
+    if rule is None:
         # Correct behaviour: unknown regions fall back to a default rule.
"""
# Hides the exception instead of fixing the lookup: the reproduction test still fails (tax 0, not 18).
HIDING_FIX = """--- a/app/pricing.py
+++ b/app/pricing.py
@@ -1,2 +1,2 @@
     tax = find_tax_rule(region)
-    tax_amount = round(subtotal * tax.rate, 2)  # AttributeError when tax is None
+    tax_amount = round(subtotal * tax.rate, 2) if tax else 0.0  # AttributeError when tax is None
"""
FLAG_FIX = """--- a/app/bugs.py
+++ b/app/bugs.py
@@ -1,2 +1,2 @@
     "code_null_tax": {
-        "enabled": False,
+        "enabled": False,  # never on
"""


class FakeLLM:
    """Answers the provider's /v1/messages calls in order; GitHub calls go to `github` when given."""

    def __init__(self, answers: list[dict], github=None):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.github = github

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            body = json.loads(request.content)
            self.prompts.append(body["system"] + "\n" + body["messages"][0]["content"])
            answer = self.answers.pop(0)
            return httpx.Response(200, json={"model": "claude-test", "content": [{"type": "text", "text": json.dumps(answer)}],
                                             "usage": {"input_tokens": 1000, "output_tokens": 200}})
        if self.github:
            return self.github(request)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def repro(code=REPRO):
    return {"test_code": code, "expected_exception": "AttributeError", "explanation": "unknown region with the flag on"}


def patch(diff=GOOD_FIX):
    return {"diff": diff, "explanation": "fall back to the default rule whenever no rule is found"}


@pytest.fixture
def llm_settings(settings, tmp_path):
    settings.rca_provider = "anthropic"
    settings.anthropic_api_key = "test-key"
    settings.fix_artifacts_dir = str(tmp_path / "fixes")
    return settings


def incident_with_rca(session, category="code", grounded=True, source_root=SHOP_DEMO, language="python",
                      file="app/pricing.py") -> Incident:
    now = datetime.now(timezone.utc)
    svc = Service(organization_id="org_local", name="shop-demo", environment="local", source_root=str(source_root),
                  runtime_path_prefix="/srv/", telemetry_service_name="shop-demo",
                  repository_url="https://github.com/acme/monorepo",
                  config={"language": language, "repository_path": "shop-demo", "repository_branch": "main"})
    session.add(svc)
    inc = Incident(organization_id="org_local", key="INC-3001", title="shop-demo 5xx error rate above 5%",
                   service="shop-demo", environment="local", severity="critical", detected_at=now)
    session.add(inc)
    session.flush()
    collection = uuid.uuid4()
    stack = ('Traceback (most recent call last):\n  File "/srv/app/pricing.py", line 32, in calculate_total\n'
             "    tax_amount = round(subtotal * tax.rate, 2)\nAttributeError: 'NoneType' object has no attribute 'rate'\n")
    for kind, content, relevance in [
        ("code", {"file": file, "line": 32, "function": "calculate_total",
                  "call_chain": [{"file": "app/main.py", "line": 90}, {"file": file, "line": 32}]}, 0.95),
        ("trace", {"exception_type": "AttributeError", "exception_message": "'NoneType' object has no attribute 'rate'",
                   "stacktrace": stack}, 0.9),
        ("deployment", {"version": "v1.1.0", "changed_files": [file]}, 0.85),
    ]:
        session.add(Evidence(organization_id="org_local", incident_id=inc.id, collection_id=collection, kind=kind,
                             source="test", title=kind, content=content, relevance=relevance))
    session.add(RcaReport(
        organization_id="org_local", incident_id=inc.id, collection_id=collection, provider="anthropic",
        model="claude-test", status="succeeded", category=category, confidence=0.9, grounded=grounded,
        report={"summary": "find_tax_rule returns None for unknown regions",
                "root_cause": {"category": category, "file": file if category != "dependency" else None, "line": 32,
                               "function": "calculate_total", "explanation": "no default rule"},
                "trigger": {"kind": "release", "release_version": "v1.1.0", "explanation": "tax refactor"}},
        checks=[{"check": "blamed file is in the evidence", "ok": grounded, "detail": file}],
    ))
    session.commit()
    return inc


def queue_and_run(session, settings, inc, llm: FakeLLM) -> FixAttempt:
    attempt, reason = maybe_enqueue_fix(session, settings, inc, requested_by="alice")
    assert attempt is not None, reason
    session.commit()
    run_fix(session, settings, str(attempt.id), str(attempt.run_id), http=llm.client())
    session.refresh(attempt)
    return attempt


def gates(attempt) -> dict:
    return {g["gate"]: g for g in attempt.gates}


# ---------- the verified path ----------

def test_verified_fix_for_the_null_tax_bug(session, llm_settings):
    inc = incident_with_rca(session)
    llm = FakeLLM([repro(), patch()])
    a = queue_and_run(session, llm_settings, inc, llm)

    assert a.status == "verified", a.reason
    assert a.target_file == "app/pricing.py" and a.patch_files == ["app/pricing.py", "tests/test_nexora_repro.py"]
    g = gates(a)
    assert all(x["ok"] for x in a.gates), a.gates
    assert "raising AttributeError as in production" in g["repro_fails_before"]["detail"]
    assert a.runs["repro_before"]["failed"] == 1 and a.runs["repro_after"]["passed"] == 1
    assert a.runs["baseline_before"]["passed"] == a.runs["baseline_after"]["passed"] == 4
    assert "-    if rule is None and not is_enabled(\"code_null_tax\"):\n+    if rule is None:\n" in a.patch_diff
    assert "+++ b/tests/test_nexora_repro.py" in a.patch_diff and "\r" not in a.patch_diff
    assert a.llm_calls == 2 and a.input_tokens == 2000 and a.provider == "anthropic"

    out = Path(a.artifacts_dir)
    assert {p.name for p in out.iterdir()} == {"fix.patch", "test_nexora_repro.py", "sandbox.log", "PR.md", "summary.json"}
    assert "git apply --directory=shop-demo fix.patch" in (out / "PR.md").read_text()
    assert (SHOP_DEMO / "app" / "pricing.py").read_text(encoding="utf-8") == PRICING  # source never modified
    assert not (SHOP_DEMO / "tests" / "test_nexora_repro.py").exists()

    run = session.get(PipelineRun, a.run_id)
    assert [s.stage for s in run.steps] == ["queued", "context", "baseline", "repro", "repro_fails", "patch",
                                            "patch_checks", "verify", "artifacts", "pr"]
    assert run.steps[-1].status == "skipped" and run.status == "success"
    events = [e.message for e in session.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id))]
    assert any(m.startswith("Verified fix for app/pricing.py") for m in events)
    # the prompt carried the case and read-only context, never the API key
    assert "FILE app/pricing.py (MAY BE CHANGED)" in llm.prompts[0] and "FILE app/bugs.py (READ-ONLY CONTEXT)" in llm.prompts[0]
    assert "EXISTING TEST tests/test_baseline.py (READ-ONLY)" in llm.prompts[0]
    assert all("test-key" not in p for p in llm.prompts)


def test_job_runs_through_the_worker_with_a_long_lease(session, factory, llm_settings, monkeypatch):
    inc = incident_with_rca(session)
    llm = FakeLLM([repro(), patch()])
    monkeypatch.setattr("app.worker.run_fix", lambda s, st, aid, rid=None: run_fix(s, st, aid, rid, http=llm.client()))
    attempt, _ = maybe_enqueue_fix(session, llm_settings, inc)
    session.commit()
    job = session.scalars(select(Job).where(Job.type == "fix.propose")).one()
    assert job.max_attempts == 1  # never retried automatically

    from app.jobs import queue

    seen = {}
    real_claim = queue.claim_next

    def spy(s, lease):
        j = real_claim(s, lease)
        seen["default_lease"] = lease
        return j

    monkeypatch.setattr("app.worker.claim_next", spy)
    drain(factory, llm_settings)
    session.refresh(attempt)
    session.refresh(job)
    assert attempt.status == "verified" and job.status == "succeeded"
    assert seen["default_lease"] == llm_settings.job_lease_seconds


# ---------- gates that stop the attempt ----------

def test_test_that_does_not_reproduce_is_rejected_after_one_retry(session, llm_settings):
    inc = incident_with_rca(session)
    llm = FakeLLM([repro(NOT_A_REPRO), repro(NOT_A_REPRO)])
    a = queue_and_run(session, llm_settings, inc, llm)
    assert a.status == "repro_failed" and a.llm_calls == 2
    assert "it passed, so it does not reproduce the defect" in a.reason
    assert "YOUR PREVIOUS TEST DID NOT REPRODUCE THE FAILURE" in llm.prompts[1]
    assert not a.patch_diff and a.artifacts_dir is None


def test_unsafe_test_is_never_run(session, llm_settings):
    inc = incident_with_rca(session)
    unsafe = "import subprocess\n\ndef test_x():\n    subprocess.run(['whoami'])\n"
    a = queue_and_run(session, llm_settings, inc, FakeLLM([repro(unsafe), repro(unsafe)]))
    assert a.status == "repro_failed"
    assert gates(a)["repro_test_safe"] == {"gate": "repro_test_safe", "ok": False, "detail": "imports subprocess"}
    assert "repro_before" not in a.runs


def test_patch_outside_the_rca_files_is_rejected(session, llm_settings):
    inc = incident_with_rca(session)
    a = queue_and_run(session, llm_settings, inc, FakeLLM([repro(), patch(FLAG_FIX), patch(FLAG_FIX)]))
    assert a.status == "patch_rejected" and a.llm_calls == 3
    assert "app/bugs.py: not one of the files the RCA or the release points at (app/pricing.py)" in a.reason
    assert "repro_after" not in a.runs


def test_patch_that_hides_the_error_fails_verification(session, llm_settings):
    inc = incident_with_rca(session)
    llm = FakeLLM([repro(), patch(HIDING_FIX), patch(HIDING_FIX)])
    a = queue_and_run(session, llm_settings, inc, llm)
    assert a.status == "verify_failed"
    assert gates(a)["repro_passes_after"]["ok"] is False and "1 failed" in a.reason
    assert "YOUR PREVIOUS PATCH DID NOT VERIFY" in llm.prompts[-1]


def test_invalid_answer_gets_one_retry(session, llm_settings):
    inc = incident_with_rca(session)
    a = queue_and_run(session, llm_settings, inc, FakeLLM([{"tests": "nope"}, repro(), patch()]))
    assert a.status == "verified" and a.llm_calls == 3


def test_provider_error_stops_with_a_typed_reason(session, llm_settings):
    inc = incident_with_rca(session)
    attempt, _ = maybe_enqueue_fix(session, llm_settings, inc)
    session.commit()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(529, text="overloaded")))
    run_fix(session, llm_settings, str(attempt.id), str(attempt.run_id), http=client)
    session.refresh(attempt)
    assert attempt.status == "failed" and "anthropic returned HTTP 529" in attempt.reason


def test_failing_baseline_stops_before_any_provider_call(session, llm_settings, tmp_path):
    import shutil

    src = tmp_path / "shop"
    shutil.copytree(SHOP_DEMO, src, ignore=shutil.ignore_patterns("__pycache__"))
    (src / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    inc = incident_with_rca(session, source_root=src)
    llm = FakeLLM([])
    a = queue_and_run(session, llm_settings, inc, llm)
    assert a.status == "failed" and "existing tests do not pass" in a.reason and llm.prompts == []


# ---------- eligibility ----------

@pytest.mark.parametrize("kwargs,reason", [
    ({"category": "dependency"}, "fixes are attempted for code and resource defects only"),
    ({"grounded": False}, "is not grounded in the evidence"),
    ({"language": "java"}, "language java is not supported"),
])
def test_ineligible_rcas_are_not_queued(session, llm_settings, kwargs, reason):
    inc = incident_with_rca(session, **kwargs)
    attempt, why = maybe_enqueue_fix(session, llm_settings, inc)
    assert attempt is None and reason in why


def test_no_llm_means_nothing_is_queued(session, settings):
    inc = incident_with_rca(session)
    attempt, why = maybe_enqueue_fix(session, settings, inc)
    assert attempt is None and "needs an LLM provider" in why
    assert not session.scalars(select(Job).where(Job.type == "fix.propose")).all()


def test_automatic_attempts_run_once_per_rca_report(session, llm_settings):
    inc = incident_with_rca(session)
    first, _ = maybe_enqueue_fix(session, llm_settings, inc)
    assert first is not None
    assert maybe_enqueue_fix(session, llm_settings, inc)[1] == "a fix attempt is already queued or running"
    first.status = "repro_failed"
    session.commit()
    assert maybe_enqueue_fix(session, llm_settings, inc)[1] == "a fix was already attempted for this RCA report"
    assert maybe_enqueue_fix(session, llm_settings, inc, requested_by="alice")[0] is not None  # a person may retry


# ---------- API ----------

def test_api_lists_requests_and_serves_the_patch(client, session, llm_settings):
    inc = incident_with_rca(session)
    assert client.get(f"/api/v1/incidents/{inc.id}/fix-attempts").json() == {"latest": None, "pending": False,
                                                                              "history": []}
    r = client.post(f"/api/v1/incidents/{inc.id}/fix-attempts", headers={"X-Nexora-Actor": "alice"})
    assert r.status_code == 202
    assert client.post(f"/api/v1/incidents/{inc.id}/fix-attempts").status_code == 409  # one at a time
    body = client.get(f"/api/v1/incidents/{inc.id}/fix-attempts").json()
    assert body["pending"] and body["latest"]["requested_by"] == "alice"

    attempt_id = r.json()["attempt_id"]
    attempt = session.get(FixAttempt, uuid.UUID(attempt_id))
    run_fix(session, llm_settings, attempt_id, str(attempt.run_id), http=FakeLLM([repro(), patch()]).client())
    patch_text = client.get(f"/api/v1/fix-attempts/{attempt_id}/patch")
    assert patch_text.status_code == 200 and patch_text.text.startswith("--- a/app/pricing.py")
    cfg = client.get("/api/v1/fix/config").json()
    assert cfg == {"enabled": True, "provider": "anthropic", "configured": True, "sandbox": "subprocess",
                   "draft_pr": False}
    assert client.get(f"/api/v1/incidents/{uuid.uuid4()}/fix-attempts").status_code == 404
    other_org = client.get(f"/api/v1/incidents/{inc.id}/fix-attempts", headers={"X-Nexora-Organization-Id": "org_other"})
    assert other_org.status_code == 404


def test_api_refuses_ineligible_requests_with_the_reason(client, session):
    inc = incident_with_rca(session)
    r = client.post(f"/api/v1/incidents/{inc.id}/fix-attempts")
    assert r.status_code == 409 and "needs an LLM provider" in r.json()["detail"]


# ---------- draft PR ----------

def github_handler(remote_pricing: str, calls: list):
    def handle(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        calls.append((method, path, json.loads(request.content) if request.content else None))
        assert request.headers["Authorization"] == "Bearer ghp_test"
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base123"}})
        if path.endswith("/git/commits/base123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if "/contents/" in path:
            return httpx.Response(200, json={"content": base64.b64encode(remote_pricing.encode()).decode()})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": f"blob{len(calls)}"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "newtree"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "newcommit"})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/acme/monorepo/pull/7"})
        return httpx.Response(404)
    return handle


def test_verified_fix_opens_a_draft_pr_when_configured(session, llm_settings):
    llm_settings.github_token = "ghp_test"
    inc = incident_with_rca(session)
    calls: list = []
    a = queue_and_run(session, llm_settings, inc,
                      FakeLLM([repro(), patch()], github=github_handler(PRICING.replace("\r\n", "\n"), calls)))
    assert a.status == "pr_opened" and a.pr_url == "https://github.com/acme/monorepo/pull/7"
    paths = [p for _, p, _ in calls]
    assert "/repos/acme/monorepo/contents/shop-demo/app/pricing.py" in paths  # base checked inside the service folder
    tree = next(b for m, p, b in calls if p.endswith("/git/trees"))
    assert sorted(t["path"] for t in tree["tree"]) == ["shop-demo/app/pricing.py", "shop-demo/tests/test_nexora_repro.py"]
    pull = next(b for m, p, b in calls if p.endswith("/pulls"))
    assert pull["draft"] is True and pull["base"] == "main" and pull["head"].startswith("nexora/fix-inc-3001-")
    assert not any(p.endswith("/merge") for p in paths)


def test_pr_is_refused_when_the_remote_file_differs(session, llm_settings):
    llm_settings.github_token = "ghp_test"
    inc = incident_with_rca(session)
    calls: list = []
    a = queue_and_run(session, llm_settings, inc, FakeLLM([repro(), patch()], github=github_handler("other code\n", calls)))
    assert a.status == "verified" and a.pr_url is None  # the verified patch files are still there
    assert "base_mismatch" in gates(a)["draft_pr"]["detail"]
    assert not any(p.endswith("/pulls") or p.endswith("/git/blobs") for _, p, _ in calls)


def test_github_timeout_is_a_typed_error(llm_settings):
    llm_settings.github_token = "ghp_test"

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    pr = GitHubDraftPr(llm_settings, httpx.Client(transport=httpx.MockTransport(slow)))
    req = PrRequest("https://github.com/acme/monorepo", "", "main", "b", "t", "body", {}, {})
    with pytest.raises(PrError) as err:
        pr.open(req)
    assert err.value.code == "timeout"
    with pytest.raises(PrError) as err:
        GitHubDraftPr(llm_settings, httpx.Client()).open(PrRequest("https://gitlab.com/a/b", "", "main", "b", "t", "", {}, {}))
    assert err.value.code == "unsupported_repository"


# ---------- automatic trigger after an RCA ----------

def test_successful_code_rca_queues_a_fix_when_an_llm_is_configured(session, settings, tmp_path, monkeypatch):
    from app.modules.rca.engine import investigate
    from tests.test_rca import collected

    inc = collected(session, settings, tmp_path)
    settings.fix_provider = "anthropic"  # RCA stays rule-based; fixes use the LLM
    settings.anthropic_api_key = "test-key"
    svc = session.scalars(select(Service)).one()
    investigate(session, settings, str(inc.id))  # catalog entry has no language yet: not eligible
    assert session.scalars(select(FixAttempt)).all() == []
    assert maybe_enqueue_fix(session, settings, inc)[1] == "language unknown is not supported by the fix sandbox yet"

    svc.config = {"language": "python"}
    session.commit()
    investigate(session, settings, str(inc.id))
    attempt = session.scalars(select(FixAttempt).where(FixAttempt.incident_id == inc.id)).one()
    assert attempt.status == "queued" and attempt.requested_by == "auto"
    job = session.scalars(select(Job).where(Job.type == "fix.propose")).one()
    assert job.payload["attempt_id"] == str(attempt.id)
