"""Rule-based investigator: the no-LLM baseline. It reads the same evidence the LLM gets and applies a
few explicit rules. It is predictable, free and offline, and it sets the bar an LLM must beat: it finds
where a failure surfaces, but it cannot read code to find a defect that lives somewhere else."""

from __future__ import annotations

from .context import InvestigationContext


def investigate(ctx: InvestigationContext) -> dict:
    items = ctx.items
    of = lambda kind: [i for i in items if i["kind"] == kind]  # noqa: E731
    code, traces, logs = of("code"), of("trace"), of("log")
    metrics = {i["content"].get("metric"): i for i in of("metric")}
    breached = [i for i in of("metric") if i["content"].get("breached")]
    deps = [i for i in of("dependency") if i["content"].get("slow") or i["content"].get("failing")]
    releases = [i for i in of("deployment") if (i["relevance"] or 0) >= 0.5]
    release = releases[0] if releases else None
    rel_ver = release["content"].get("version") if release else None
    gaps = [i["title"] for i in of("gap")]
    error = (traces + logs)[0] if traces + logs else None
    etype = error["content"].get("exception_type") if error else None
    refs = lambda *xs: [x["ref"] for x in xs if x]  # noqa: E731

    hypotheses: list[dict] = []
    saturation = next((i for i in breached
                       if any(w in (i["content"].get("metric") or "") for w in ("pool", "saturation", "utilization"))),
                      None)

    if deps and not code:
        d = deps[0]
        c = d["content"]
        lat = next((i for i in breached if "latency" in (i["content"].get("metric") or "")), None)
        err = metrics.get("error_ratio")
        verdict = "failing" if c.get("failing") else "slow"
        conf = 0.8 if lat and (c.get("share_of_request") or 0) >= 0.5 else 0.6
        report = _report(
            summary=f"{c['peer']} is {verdict}; {ctx.incident.service} spends "
                    f"{round((c.get('share_of_request') or 0) * 100)}% of each request waiting on it.",
            category="dependency", component=c["peer"], file=None, line=None, function=None,
            explanation=f"Calls to {c['peer']} averaged {c.get('avg_seconds')} s ({c.get('calls')} calls). "
                        f"No exception was raised in {ctx.incident.service}'s own code, so the delay is downstream.",
            trigger=("external", None, f"{c['peer']} degraded; no release of {ctx.incident.service} in the window."),
            symptom=f"Latency surfaces in {ctx.incident.service}; the delay is inside {c['peer']}.",
            confidence=conf, evidence=refs(d, lat, err),
            mitigations=[("failover", c["peer"], "Route to a healthy instance or region of the dependency if one exists.", "medium"),
                         ("config_change", f"{ctx.incident.service} → {c['peer']} timeout",
                          "Lower the client timeout and fail fast so requests stop piling up.", "low"),
                         ("wait", c["peer"], "Escalate to the owner of the dependency and monitor.", "low")],
            next_checks=[f"Check {c['peer']}'s own health, latency and recent changes.",
                         "Confirm no retry storm from our client is adding load."],
        )
        hypotheses.append(_h(f"{c['peer']} is {verdict}", conf, "accepted", refs(d, lat), [], ""))
        if release is None:
            hypotheses.append(_h(f"A release of {ctx.incident.service} caused it", 0.05, "rejected", [], [],
                                 "No release in the evidence window."))
    elif saturation is not None:
        files = release["content"].get("changed_files") if release else []
        conf = 0.6 if release else 0.4
        top = code[0]["content"] if code else None
        report = _report(
            summary=f"{saturation['title']}; requests time out waiting for a resource"
                    + (f" after release {rel_ver} changed {', '.join(files)}." if release else "."),
            category="resource", component=saturation["content"].get("metric") or "resource pool",
            file=files[0] if files else (top["file"] if top else None), line=None, function=None,
            explanation="The resource pool is exhausted, so new requests wait and time out. Exhaustion that "
                        "builds up under steady load points at a leak (acquired and never returned) "
                        + (f"introduced by {rel_ver}." if release else "rather than a traffic spike."),
            trigger=("release", rel_ver, f"{rel_ver} changed {', '.join(files)}") if release
            else ("unknown", None, "No release in the window."),
            symptom=(f"Errors surface at {top['file']}:{top['line']} {top['function']}() where the pool times out; "
                     "that is the symptom, not the defect." if top else ""),
            confidence=conf, evidence=refs(saturation, release, code[0] if code else None, error),
            mitigations=([("rollback", f"{ctx.incident.service} {rel_ver}", "The release coincides with the leak.", "low")]
                         if release else [])
            + [("restart", ctx.incident.service, "Frees leaked resources; buys time but does not fix the leak.", "medium")],
            next_checks=["Find code paths that acquire the resource and do not release it on every path."]
            + ([f"Review the diff of {rel_ver} in {', '.join(files)}."] if release else []),
        )
        hypotheses.append(_h("Resource leak", conf, "accepted", refs(saturation, release), [], ""))
        if top:
            hypotheses.append(_h(f"Bug at {top['file']}:{top['line']} where the error is raised", 0.2, "rejected",
                                 refs(code[0]), refs(saturation), "That is where the pool times out, not why."))
    elif code:
        top = code[0]["content"]
        in_release = bool(top.get("changed_in_deployment"))
        err = next((i for i in breached if i["content"].get("metric") == "error_ratio"), None)
        conf = 0.85 if in_release and err else 0.7 if in_release or err else 0.5
        report = _report(
            summary=f"{etype or 'An exception'} at {top['file']}:{top['line']} in {top['function']}()"
                    + (f", changed in release {top.get('deployment_version')}." if in_release else "."),
            category="code", component=top["file"], file=top["file"], line=top["line"], function=top["function"],
            explanation=f"The failing line is `{top.get('failing_line', '').strip()}`. "
                        f"{(error['summary'] if error else '')}".strip(),
            trigger=("release", top.get("deployment_version"), f"{top['file']} changed in {top.get('deployment_version')}")
            if in_release else ("unknown", None, "The failing file was not changed by a recent release."),
            symptom="",
            confidence=conf, evidence=refs(code[0], error, err, release),
            mitigations=([("rollback", f"{ctx.incident.service} {top.get('deployment_version')}",
                           "The failing code arrived in this release.", "low")] if in_release else [])
            + [("other", f"{top['file']}:{top['line']}", "Fix the defect and redeploy.", "medium")],
            next_checks=[f"Read {top['file']} around line {top['line']} and the release diff.",
                         "Check which inputs reach this line (see the trace's span attributes)."],
        )
        hypotheses.append(_h(f"Defect at {top['file']}:{top['line']}", conf, "accepted", refs(code[0], error), [], ""))
    else:
        report = _report(
            summary="Not enough evidence to name a root cause.", category="unknown",
            component=ctx.incident.service, file=None, line=None, function=None,
            explanation="No exception, slow dependency or saturated resource was found in the window.",
            trigger=("unknown", None, ""), symptom="", confidence=0.15,
            evidence=refs(*breached[:2]), mitigations=[("wait", ctx.incident.service, "Gather more evidence.", "low")],
            next_checks=["Check the gaps listed under missing evidence."],
        )
    report["hypotheses"] = hypotheses
    report["missing_evidence"] = gaps[:10]
    return report


def _h(title, likelihood, status, supporting, contradicting, reasoning) -> dict:
    return {"title": title, "likelihood": likelihood, "status": status, "supporting": supporting,
            "contradicting": contradicting, "reasoning": reasoning}


def _report(*, summary, category, component, file, line, function, explanation, trigger, symptom, confidence,
            evidence, mitigations, next_checks) -> dict:
    kind, version, why = trigger
    return {
        "summary": summary[:400],
        "root_cause": {"category": category, "component": component, "file": file, "line": line,
                       "function": function, "explanation": explanation[:2000]},
        "trigger": {"kind": kind, "release_version": version, "explanation": why},
        "symptom_vs_cause": symptom,
        "confidence": confidence,
        "evidence_refs": evidence,
        "next_checks": next_checks,
        "mitigation_options": [{"action": a, "target": t, "reason": r, "risk": k} for a, t, r, k in mitigations],
    }
