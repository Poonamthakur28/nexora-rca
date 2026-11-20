"""Nexora RCA evaluation bench.

Runs each scenario in scenarios.yaml against the live local stack:
  enable failure -> steady load -> wait for a new incident + evidence + RCA
  -> if a plan is proposed, approve it (the human step) and wait for recovery
  -> finally disable the bug (cleanup) so a failed run never leaves it on.
Writes bench/results/<timestamp>.json and .md.

When a scenario expects mitigation, the runner does not disable the bug to make
the alert clear: the catalog action has to be what fixes it.

Usage:  python bench/run_bench.py [--only name,name] [--timeout 480]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import yaml

NEXORA = "http://localhost:8000"
SHOP = "http://localhost:8080"
PROM = "http://localhost:19090"
HERE = pathlib.Path(__file__).resolve().parent
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    req = urllib.request.Request(url, method=method, data=data, headers=hdrs)
    try:
        with NO_PROXY.open(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def check(expect: dict, items: list[dict], started: dt.datetime) -> list[dict]:
    """Return one result row per expectation: {check, ok, detail}."""
    by = lambda k: [i for i in items if i["kind"] == k]  # noqa: E731
    out = []

    def add(name, ok, detail):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    if "exception" in expect:
        types = {(i["content"] or {}).get("exception_type") for i in by("trace") + by("log")} - {None}
        add(f"exception {expect['exception']}", expect["exception"] in types, f"seen: {sorted(types) or 'none'}")
    if "code_file" in expect:
        code = by("code")
        top = code[0]["content"] if code else None
        add(f"top code frame in {expect['code_file']}", top and top["file"] == expect["code_file"],
            f"{top['file']}:{top['line']} {top['function']}()" if top else "no code evidence")
        if expect.get("code_changed_in_release"):
            add("code changed in the latest release", top and top.get("changed_in_deployment"),
                f"release {top.get('deployment_version')}" if top else "no code evidence")
    if expect.get("no_code_evidence"):
        add("no code evidence (nothing in our code failed)", not by("code"),
            ", ".join(f"{c['content']['file']}:{c['content']['line']}" for c in by("code")) or "none")
    metrics = {(i["content"] or {}).get("metric"): i["content"] for i in by("metric")}
    for m in expect.get("metrics_breached", []):
        c = metrics.get(m)
        add(f"metric {m} breached", c and c["breached"],
            f"peak {c['peak']} vs threshold {c['threshold']}" if c else "metric missing")
    for m in expect.get("metrics_normal", []):
        c = metrics.get(m)
        add(f"metric {m} normal", c and not c["breached"],
            f"peak {c['peak']} vs threshold {c['threshold']}" if c else "metric missing")
    if "dependency_slow" in expect:
        peer = expect["dependency_slow"]
        d = next((i["content"] for i in by("dependency") if i["content"]["peer"] == peer), None)
        add(f"dependency {peer} flagged slow", d and d["slow"],
            f"avg {d['avg_seconds']}s, {round(d['share_of_request'] * 100)}% of request, {d['calls']} calls"
            if d else "no dependency evidence")
    if "release_blamed" in expect:
        recent = [i["content"] for i in by("deployment")
                  if dt.datetime.fromisoformat(i["content"]["deployed_at"]) >= started - dt.timedelta(seconds=5)]
        want = expect["release_blamed"]
        add("a release in the window" if want else "no release in the window", bool(recent) == want,
            ", ".join(f"{r['version']} ({', '.join(r['changed_files'])})" for r in recent) or "none")
    return out


def check_rca(expect: dict, rca: dict | None, provider: str) -> list[dict]:
    out = []

    def add(name, ok, detail):
        out.append({"check": f"RCA: {name}", "ok": bool(ok), "detail": detail})

    if not rca or rca["status"] != "succeeded":
        add("report produced", False, (rca or {}).get("error") or "no report yet")
        return out
    rep, rc = rca["report"], rca["report"]["root_cause"]
    where = (rc["file"] + (f":{rc['line']}" if rc["line"] else "") + (f" {rc['function']}" if rc["function"] else "")) if rc["file"] else rc["component"]
    add(f"category {expect['category']}", rc["category"] == expect["category"], f"{rc['category']} ({where})")
    if "file" in expect:
        add(f"blames {expect['file']}", rc["file"] == expect["file"], rc["file"] or "no file")
    if "component" in expect:
        add(f"blames {expect['component']}", expect["component"] in (rc["component"] or ""), rc["component"])
    if "release" in expect:
        blamed = rep["trigger"]["kind"] == "release"
        add("release blamed" if expect["release"] else "no release blamed", blamed == expect["release"],
            f"{rep['trigger']['kind']} {rep['trigger'].get('release_version') or ''}".strip())
    if "llm_function" in expect and provider != "heuristic":
        add(f"finds the defect in {expect['llm_function']}()",
            expect["llm_function"] in json.dumps(rep), rc.get("function") or "not mentioned")
    add("grounded in evidence", rca["grounded"],
        "; ".join(c["detail"] for c in rca["checks"] if not c["ok"]) or f"confidence {rca['confidence']}")
    return out


def allowed_actions(expect: dict) -> list[str]:
    action = expect.get("action")
    if action is None:
        return []
    return list(action) if isinstance(action, list) else [action]


def check_mitigation(expect: dict, bundle: dict | None) -> list[dict]:
    out = []

    def add(name, ok, detail):
        out.append({"check": f"mitigation: {name}", "ok": bool(ok), "detail": detail})

    if not bundle:
        add("plan", False, "no mitigation payload")
        return out
    chosen = bundle.get("chosen")
    if expect.get("escalated"):
        add("no plan proposed", chosen is None, (chosen or {}).get("action_type") or "none")
        msg = bundle.get("no_safe_action") or ""
        add("escalated to on-call", "no safe action" in msg.lower() or "escalate" in msg.lower(),
            msg or "no escalation event")
        return out
    actions = allowed_actions(expect)
    add("action " + " or ".join(actions),
        chosen is not None and chosen.get("action_type") in actions,
        (chosen or {}).get("action_type") or "no plan")
    if expect.get("recovered"):
        rec = bundle.get("recovery") or {}
        add("recovered", rec.get("verdict") == "healthy", rec.get("verdict") or "no recovery check")
    return out


def mitigation_settled(expect: dict, bundle: dict | None) -> bool:
    if not bundle:
        return False
    if expect.get("escalated"):
        return not bundle.get("pending")
    if bundle.get("recovery"):
        return True
    status = (bundle.get("chosen") or {}).get("status")
    return status in {"failed", "undone", "rejected"}


def run_mitigation(expect: dict, incident_id: str, deadline: float) -> tuple[list[dict], dict | None]:
    """Approve a proposed plan (the human step) and wait for the recovery verdict.

    Does not disable the planted bug: the catalog action has to be what fixes it.
    """
    approved = False
    last = None
    while time.time() < deadline:
        try:
            last = call("GET", f"{NEXORA}/api/v1/incidents/{incident_id}/mitigation")
        except RuntimeError as e:
            log(f"mitigation not ready: {e}")
            time.sleep(10)
            continue
        chosen = last.get("chosen")
        should_approve = bool(allowed_actions(expect) or expect.get("recovered"))
        if should_approve and not approved and chosen and chosen.get("status") == "proposed":
            log(f"approving {chosen['action_type']} (human step)")
            try:
                call("POST", f"{NEXORA}/api/v1/mitigation-plans/{chosen['id']}/approve",
                     body={"comment": "bench"}, headers={"X-Nexora-Actor": "bench"})
                approved = True
            except RuntimeError as e:
                checks = check_mitigation(expect, last)
                checks.append({"check": "mitigation: approved", "ok": False, "detail": str(e)})
                return checks, last
        if mitigation_settled(expect, last):
            return check_mitigation(expect, last), last
        status = (chosen or {}).get("status") or ("escalated" if last.get("no_safe_action") else "pending")
        log(f"waiting for mitigation ({status})")
        time.sleep(10)
    return check_mitigation(expect, last), last


def wait_quiet(timeout: int = 600) -> bool:
    """Wait until Prometheus has no pending or firing shop-demo alerts. Returns True if it had to wait."""
    waited = False
    end = time.time() + timeout
    while time.time() < end:
        alerts = call("GET", f"{PROM}/api/v1/alerts")["data"]["alerts"]
        if not alerts:
            return waited
        if not waited:
            log("waiting for earlier alerts to clear: " + ", ".join(a["labels"]["alertname"] for a in alerts))
        waited = True
        time.sleep(15)
    return waited


def wait_resolved(incident_id: str, timeout: int) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        inc = call("GET", f"{NEXORA}/api/v1/incidents/{incident_id}")
        if inc.get("active_alerts", 1) == 0:
            return True
        time.sleep(10)
    return False


def run_scenario(sc: dict, timeout: int) -> dict:
    log(f"=== {sc['name']}: {sc['story']}")
    before = {i["id"] for i in call("GET", f"{NEXORA}/api/v1/incidents")}
    started = now()
    res = call("POST", f"{NEXORA}/api/v1/system-checks/actions/enable_bug?bug={sc['bug']}")
    log(f"enable: {res['status']} - {res['detail']}")
    call("POST", f"{SHOP}/admin/load/start?rps=3&seconds={timeout + 60}")
    result = {"name": sc["name"], "bug": sc["bug"], "story": sc["story"], "started_at": started.isoformat(),
              "incident": None, "checks": [], "evidence": [], "seconds_to_incident": None,
              "seconds_to_pass": None, "resolved": None, "rca": None, "mitigation": None}
    deadline = time.time() + timeout
    inc = None
    try:
        while time.time() < deadline:
            time.sleep(10)
            if inc is None:
                new = [i for i in call("GET", f"{NEXORA}/api/v1/incidents") if i["id"] not in before]
                if not new:
                    continue
                inc = new[-1]
                result["incident"] = {"key": inc["key"], "title": inc["title"]}
                result["seconds_to_incident"] = round((now() - started).total_seconds())
                log(f"incident {inc['key']} opened after {result['seconds_to_incident']}s: {inc['title']}")
            ev = call("GET", f"{NEXORA}/api/v1/incidents/{inc['id']}/evidence")
            if not ev["collection"] or ev["collection"]["status"] in ("running", "queued"):
                continue
            result["checks"] = check(sc["expect"], ev["items"], started)
            result["evidence"] = [{"kind": i["kind"], "title": i["title"], "relevance": i["relevance"]}
                                  for i in ev["items"]]
            if "rca" in sc:
                rca = call("GET", f"{NEXORA}/api/v1/incidents/{inc['id']}/rca")
                latest = rca["latest"]
                if latest and latest.get("collection_id") != ev["collection"]["id"]:
                    latest = None  # still investigating the newest evidence
                provider = call("GET", f"{NEXORA}/api/v1/rca/config")["provider"]
                result["rca"] = latest and {k: latest[k] for k in ("provider", "model", "category", "confidence",
                                                                   "grounded", "latency_ms")} | {
                    "summary": latest["report"].get("summary")}
                result["checks"] += check_rca(sc["rca"], latest, provider)
            failing = [c["check"] for c in result["checks"] if not c["ok"]]
            if failing:
                log(f"waiting; not yet: {', '.join(failing)}")
                continue
            result["seconds_to_pass"] = round((now() - started).total_seconds())
            log(f"all {len(result['checks'])} evidence/RCA checks pass after {result['seconds_to_pass']}s")
            if "mitigation" in sc:
                # Recovery watches ~120s after execute; don't cut that short if RCA used most of --timeout.
                mit_deadline = max(deadline, time.time() + 240)
                mit_checks, bundle = run_mitigation(sc["mitigation"], inc["id"], mit_deadline)
                result["checks"] += mit_checks
                chosen = (bundle or {}).get("chosen") or {}
                rec = (bundle or {}).get("recovery") or {}
                result["mitigation"] = {
                    "action": chosen.get("action_type"), "status": chosen.get("status"),
                    "verdict": rec.get("verdict"), "escalated": (bundle or {}).get("no_safe_action"),
                }
                failing = [c["check"] for c in mit_checks if not c["ok"]]
                if failing:
                    log(f"mitigation failed: {', '.join(failing)}")
                else:
                    result["seconds_to_pass"] = round((now() - started).total_seconds())
                    log(f"mitigation checks pass after {result['seconds_to_pass']}s")
            break
        else:
            log("timed out" + ("" if inc else " - no incident opened"))
    finally:
        # Cleanup only: when mitigation is expected, the action (not this call) must have fixed it.
        res = call("POST", f"{NEXORA}/api/v1/system-checks/actions/disable_bug?bug={sc['bug']}")
        log(f"disable: {res['status']} - {res['detail']}")
        call("POST", f"{SHOP}/admin/load/start?rps=3&seconds=150")  # healthy traffic so the alert can clear
        if inc:
            result["resolved"] = wait_resolved(inc["id"], 360)
            log(f"alerts resolved: {result['resolved']}")
        call("POST", f"{SHOP}/admin/load/stop")
    for c in result["checks"]:
        log(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}  ({c['detail']})")
    return result


def report(results: list[dict]) -> str:
    lines = ["# Nexora RCA evaluation bench", "", f"Run at {now():%Y-%m-%d %H:%M UTC}", "",
             "| Scenario | Incident | Time to incident | Time to full evidence | Checks | Resolved | Mitigation |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        ok = sum(c["ok"] for c in r["checks"])
        mit = r.get("mitigation") or {}
        mit_cell = mit.get("action") or ("escalated" if mit.get("escalated") else "—")
        if mit.get("verdict"):
            mit_cell += f" / {mit['verdict']}"
        lines.append(f"| {r['name']} | {r['incident']['key'] if r['incident'] else '—'} | "
                     f"{r['seconds_to_incident'] or '—'} s | {r['seconds_to_pass'] or '—'} s | "
                     f"{ok}/{len(r['checks'])} | {'yes' if r['resolved'] else 'no'} | {mit_cell} |")
    for r in results:
        lines += ["", f"## {r['name']}", "", r["story"], ""]
        if r["incident"]:
            lines.append(f"Incident {r['incident']['key']}: {r['incident']['title']}")
            lines.append("")
        if r.get("rca"):
            a = r["rca"]
            lines.append(f"RCA ({a['provider']}/{a['model']}, {round((a['confidence'] or 0) * 100)}% confidence, "
                         f"{a['latency_ms']} ms): {a['summary']}")
            lines.append("")
        for c in r["checks"]:
            lines.append(f"- {'✅' if c['ok'] else '❌'} {c['check']} — {c['detail']}")
        if r["evidence"]:
            lines += ["", "Evidence collected (most relevant first):", ""]
            lines += [f"- `{e['kind']}` {e['title']}" for e in r["evidence"][:10]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=480)
    ap.add_argument("--gap", type=int, default=330,
                    help="calm seconds between scenarios; longer than ALERT_REOPEN_WINDOW_MINUTES, "
                         "so a re-firing alert counts as a new incident")
    args = ap.parse_args()
    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text())["scenarios"]
    if args.only:
        keep = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["name"] in keep]
    results = []
    for i, sc in enumerate(scenarios):
        # Always pause: an alert that cleared moments ago and fires again would count as a flap,
        # and the evidence window would still contain the previous failure.
        wait_quiet()
        log(f"calm gap {args.gap}s so the next evidence window starts clean")
        time.sleep(args.gap)
        results.append(run_scenario(sc, args.timeout))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = f"{now():%Y%m%d-%H%M}"
    (out / f"{stamp}.json").write_text(json.dumps(results, indent=2, default=str))
    (out / f"{stamp}.md").write_text(report(results))
    log(f"report: bench/results/{stamp}.md")
    return 0 if all(c["ok"] for r in results for c in r["checks"]) and all(r["checks"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
