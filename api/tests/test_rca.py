import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Evidence, IncidentEvent, Job, PipelineRun, RcaReport
from app.modules.evidence.collect import collect_evidence
from app.modules.rca.context import build_context, render_for_prompt
from app.modules.rca.engine import extract_json, ground, investigate
from app.modules.rca.schema import RcaReportModel
from tests.test_evidence import full_stack_handler, make_bench, trace_body, urls


def collected(session, settings, tmp_path, handler=full_stack_handler):
    inc = make_bench(session, settings, tmp_path)
    urls(settings)
    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    return inc


def llm_report(file="app/pricing.py", line=None, refs=("E1",), version="v1.1.0", category="code", conf=0.9):
    return {
        "summary": "find_tax_rule returns None for unknown regions",
        "root_cause": {"category": category, "component": "pricing", "file": file, "line": line,
                       "function": "calculate_total", "explanation": "rule is None for TN"},
        "trigger": {"kind": "release", "release_version": version, "explanation": "tax refactor"},
        "confidence": conf, "evidence_refs": list(refs),
        "hypotheses": [{"title": "None tax rule", "likelihood": 0.9, "status": "accepted", "supporting": list(refs)},
                       {"title": "Traffic spike", "likelihood": 0.05, "status": "rejected"}],
        "mitigation_options": [{"action": "rollback", "target": "shop-demo v1.1.0", "reason": "introduced it",
                                "risk": "low"}],
    }


def code_line(session):
    return session.scalars(select(Evidence).where(Evidence.kind == "code")).first().content["line"]


def test_collection_queues_an_investigation(session, settings, tmp_path):
    collected(session, settings, tmp_path)
    jobs = session.scalars(select(Job).where(Job.type == "rca.investigate")).all()
    assert len(jobs) == 1
    run = session.scalars(select(PipelineRun).where(PipelineRun.trigger_kind == "rca_investigation")).one()
    assert run.steps[0].stage == "queued"


def test_heuristic_baseline_names_the_faulty_line(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    run_id = session.scalars(select(Job).where(Job.type == "rca.investigate")).one().payload["run_id"]
    out = investigate(session, settings, str(inc.id), run_id)
    assert out["status"] == "succeeded"
    row = session.scalars(select(RcaReport)).one()
    rc = row.report["root_cause"]
    assert (row.category, rc["file"], rc["line"]) == ("code", "app/pricing.py", code_line(session))
    assert row.report["trigger"]["release_version"] == "v1.1.0"
    assert row.grounded and row.confidence >= 0.8
    assert all(c["ok"] for c in row.checks)
    assert row.report["evidence_index"]["E1"]["kind"] == "code"
    stages = [s.stage for s in session.get(PipelineRun, row.run_id).steps]
    assert stages == ["queued", "context", "model", "validate", "grounding", "stored"]
    assert any(e.kind == "rca_completed" for e in session.scalars(select(IncidentEvent)))


def test_prompt_contains_evidence_ids_and_changed_source(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    ctx = build_context(session, inc)
    text = render_for_prompt(ctx)
    assert "[E1] kind=code" in text and "SOURCE OF app/pricing.py (changed in release v1.1.0)" in text
    assert "letmein123" not in text and "bob@example.com" not in text  # redacted at collection


@pytest.mark.parametrize("provider,reply", [
    ("anthropic", lambda body: {"model": "claude-x", "content": [{"type": "text", "text": body}],
                                "usage": {"input_tokens": 1200, "output_tokens": 300}}),
    ("openai", lambda body: {"model": "gpt-x", "choices": [{"message": {"content": body}}],
                             "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}),
    ("ollama", lambda body: {"model": "llama-x", "message": {"content": body},
                             "prompt_eval_count": 1200, "eval_count": 300}),
])
def test_llm_providers(session, settings, tmp_path, provider, reply):
    inc = collected(session, settings, tmp_path)
    settings.rca_provider = provider
    settings.anthropic_api_key = settings.openai_api_key = "sk-test"
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("x-api-key") or request.headers.get("authorization")
        return httpx.Response(200, json=reply("```json\n" + json.dumps(llm_report(line=code_line(session))) + "\n```"))

    out = investigate(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["status"] == "succeeded", out
    row = session.scalars(select(RcaReport)).one()
    assert row.provider == provider and row.model.endswith("-x") and row.input_tokens == 1200
    assert row.grounded and row.confidence == 0.9
    text = json.dumps(seen["body"])
    assert "[E1] kind=code" in text and "Output ONLY one JSON object" in text
    if provider != "ollama":
        assert "sk-test" in seen["auth"]


def test_invalid_output_is_retried_once_with_the_error(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    settings.rca_provider, settings.anthropic_api_key = "anthropic", "k"
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["messages"][0]["content"])
        text = '{"summary": "x"}' if len(calls) == 1 else json.dumps(llm_report(line=code_line(session)))
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    out = investigate(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["status"] == "succeeded" and len(calls) == 2
    assert "did not match the required JSON shape" in calls[1]
    assert session.scalars(select(RcaReport)).one().report["attempts"] == 2


def test_provider_errors_are_recorded_not_raised(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    settings.rca_provider, settings.anthropic_api_key = "anthropic", ""
    out = investigate(session, settings, str(inc.id))
    assert out["status"] == "failed" and "ANTHROPIC_API_KEY" in out["error"]
    settings.anthropic_api_key = "secret-key-123"
    out = investigate(session, settings, str(inc.id), http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"}))))
    assert out["status"] == "failed" and "HTTP 401" in out["error"] and "secret-key-123" not in out["error"]
    assert {r.status for r in session.scalars(select(RcaReport))} == {"failed"}
    assert any(e.kind == "rca_failed" for e in session.scalars(select(IncidentEvent)))


def test_ungrounded_claims_lower_confidence(session, settings, tmp_path):
    inc = collected(session, settings, tmp_path)
    ctx = build_context(session, inc)
    made_up = RcaReportModel.model_validate(llm_report(file="app/payments_v2.py", refs=("E1", "E99"), version="v9.9.9"))
    checks, conf = ground(made_up, ctx)
    failed = {c["check"] for c in checks if not c["ok"]}
    assert failed == {"cited evidence exists", "blamed file is in the evidence", "blamed release is in the evidence"}
    assert conf == 0.3
    bad_line = RcaReportModel.model_validate(llm_report(line=9999))
    checks, conf = ground(bad_line, ctx)
    assert [c["check"] for c in checks if not c["ok"]] == ["blamed line exists"] and conf == 0.4
    nothing = RcaReportModel.model_validate(llm_report(refs=()))
    checks, conf = ground(nothing, ctx)
    assert conf == 0.3


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_heuristic_blames_dependency_not_code(session, settings, tmp_path):
    from datetime import datetime, timezone

    ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))

    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "tempo" and path == "/api/search":
            q = request.url.params["q"]
            return httpx.Response(200, json={"traces": [{"traceID": "slow1", "durationMs": 2600}] if "duration" in q else []})
        if host == "tempo":
            body = trace_body(ns, gateway_seconds=2.5)
            for rs in body["trace"]["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    for sp in ss["spans"]:
                        sp.pop("events", None)
                        sp.get("status", {}).pop("code", None)
            return httpx.Response(200, json=body)
        if host == "loki":
            return httpx.Response(200, json={"data": {"result": []}})
        if host == "prom":
            t = datetime.now(timezone.utc).timestamp()
            return httpx.Response(200, json={"data": {"result": [{"values": [[t - 60, "0"], [t, "0"]]}]}})
        return httpx.Response(404)

    inc = make_bench(session, settings, tmp_path, minutes_before=600)  # old release: not in the window
    urls(settings)
    collect_evidence(session, settings, str(inc.id), http=httpx.Client(transport=httpx.MockTransport(handler)))
    investigate(session, settings, str(inc.id))
    row = session.scalars(select(RcaReport)).one()
    assert row.category == "dependency" and row.report["root_cause"]["component"] == "payment-gateway"
    assert row.report["root_cause"]["file"] is None and row.report["trigger"]["kind"] == "external"
    assert row.grounded


def test_rca_api(client, session, settings, tmp_path, factory):
    from tests.conftest import drain

    inc = collected(session, settings, tmp_path)
    assert client.get("/api/v1/rca/config").json()["provider"] == "heuristic"
    assert client.get(f"/api/v1/incidents/{inc.id}/rca").json()["latest"] is None
    drain(factory, settings)
    body = client.get(f"/api/v1/incidents/{inc.id}/rca").json()
    assert body["latest"]["category"] == "code" and not body["in_progress"]
    assert client.post(f"/api/v1/incidents/{inc.id}/rca/investigate").json() == {"queued": True}
    assert client.post(f"/api/v1/incidents/{inc.id}/rca/investigate").json() == {"queued": False}  # deduped
    drain(factory, settings)
    assert len(client.get(f"/api/v1/incidents/{inc.id}/rca").json()["history"]) == 2
