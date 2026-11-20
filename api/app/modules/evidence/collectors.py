"""Evidence collectors. Each one reads one source for one service and time window.

A collector returns evidence items, or raises GapError when the source has nothing useful.
Any other exception means the collector itself failed. Either way the incident still gets the
evidence the other collectors found; nothing here is fatal.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.config import Settings
from app.db.models import Incident, Service, ServiceDeployment

from .redaction import redact, redact_obj
from .stacktrace import Frame, deepest_app_frame, exception_line, parse_frames, resolve_source_path

ERROR_RATIO_THRESHOLD = 0.05


class GapError(Exception):
    """The source answered but had nothing for this incident (recorded as a gap, not a failure)."""


@dataclass
class EvidenceItem:
    kind: str
    source: str
    title: str
    summary: str
    content: dict
    relevance: float
    observed_at: datetime | None = None


@dataclass
class CollectionContext:
    settings: Settings
    http: httpx.Client
    incident: Incident
    service: Service
    window_start: datetime
    window_end: datetime
    deployment: ServiceDeployment | None = None
    deployments: list[ServiceDeployment] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)  # filled by trace/log collectors, used by code

    def get_json(self, url: str, **params) -> dict | list:
        resp = self.http.get(url, params=params, timeout=self.settings.check_timeout_seconds + 2)
        resp.raise_for_status()
        return resp.json()


def _ts(ns: int | str) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)


def _attrs(items: list[dict]) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value", {})
        out[a.get("key")] = next(iter(v.values()), None) if isinstance(v, dict) else v
    return out


def _parse(ctx: CollectionContext, stacktrace: str) -> list[Frame]:
    cfg = ctx.service.config or {}
    return parse_frames(stacktrace, ctx.service.runtime_path_prefix, cfg.get("language"), cfg.get("app_namespaces"))


def _remember_frames(ctx: CollectionContext, stacktrace: str, origin: str) -> list[Frame]:
    frames = _parse(ctx, stacktrace)
    deepest = deepest_app_frame(frames)
    if deepest and deepest.relative_path:
        ctx.frames.append({**deepest.to_dict(), "origin": origin,
                           "call_chain": [f.to_dict() for f in frames if f.is_app]})
    return frames


# ---------------------------------------------------------------- traces (Tempo)

MAX_UNREADABLE_TRACES = 2  # extra search results tried when single trace downloads fail


def _read_traces(ctx: CollectionContext, summaries: list[dict], want: int) -> tuple[list[tuple[dict, str, dict]],
                                                                                    list[dict]]:
    """Downloads up to `want` full traces from the search results, in order.

    A trace whose download times out or errors is skipped and the next result is tried, at most
    MAX_UNREADABLE_TRACES times (Tempo can stall for a few seconds; one slow trace must not hide the others).
    Each request is made once with the normal timeout. If no trace can be read, the last error is raised,
    so the source is reported as failed exactly as before."""
    read: list[tuple[dict, str, dict]] = []
    skipped: list[dict] = []
    last_error: Exception | None = None
    for summary in summaries[: want + MAX_UNREADABLE_TRACES]:
        if len(read) >= want:
            break
        trace_id = summary["traceID"].rjust(32, "0")
        try:
            data = ctx.get_json(f"{ctx.settings.tempo_url}/api/v2/traces/{trace_id}")
        except httpx.HTTPError as exc:
            skipped.append({"trace_id": trace_id, "error": type(exc).__name__})
            last_error = exc
            continue
        read.append((summary, trace_id, data.get("trace", data)))
    if not read and last_error is not None:
        raise last_error
    return read, skipped


def _unreadable_note(read: int, skipped: list[dict]) -> str:
    if not skipped:
        return ""
    errors = ", ".join(sorted({s["error"] for s in skipped}))
    return f" {len(skipped)} of {read + len(skipped)} trace(s) could not be read ({errors})."


def collect_traces(ctx: CollectionContext) -> list[EvidenceItem]:
    s, svc = ctx.settings, ctx.service.telemetry_service_name
    query = f'{{ resource.service.name = "{svc}" && status = error }}'
    found = ctx.get_json(f"{s.tempo_url}/api/search", q=query, start=int(ctx.window_start.timestamp()),
                         end=int(ctx.window_end.timestamp()), limit=50).get("traces") or []
    if not found:
        raise GapError("no error traces for this service in the window")

    groups: dict[tuple, dict] = {}
    traces, skipped = _read_traces(ctx, found, 5)  # read a few full traces; they are usually the same failure
    for _summary, trace_id, trace in traces:
        for rs in trace.get("resourceSpans") or trace.get("batches") or []:
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    for ev in span.get("events", []):
                        if ev.get("name") != "exception":
                            continue
                        a = _attrs(ev.get("attributes"))
                        key = (a.get("exception.type"), a.get("exception.message"))
                        g = groups.setdefault(key, {"trace_ids": [], "span": span.get("name"),
                                                    "stacktrace": a.get("exception.stacktrace", ""),
                                                    "first_seen": _ts(ev["timeUnixNano"]),
                                                    "span_attributes": _attrs(span.get("attributes"))})
                        if trace_id not in g["trace_ids"]:
                            g["trace_ids"].append(trace_id)
    if not groups:
        raise GapError(f"{len(found)} error trace(s) found but none carried an exception event"
                       + _unreadable_note(len(traces), skipped))

    items = []
    for (etype, msg), g in list(groups.items())[: ctx.settings.evidence_max_items]:
        stack = redact(g["stacktrace"])
        # A Python stack that does not end with the exception line was cut off by the trace backend.
        language = (ctx.service.config or {}).get("language") or "python"
        truncated = bool(stack) and language == "python" and not exception_line(stack, "python").startswith(f"{etype}")
        frames = _parse(ctx, stack) if truncated else _remember_frames(ctx, stack, "trace")
        deepest = None if truncated else deepest_app_frame(frames)
        where = f" at {deepest.relative_path}:{deepest.line} in {deepest.function}" if deepest else \
            (" (stack trace truncated by the trace backend; see logs for the full trace)" if truncated else "")
        items.append(EvidenceItem(
            kind="trace", source="tempo",
            title=f"{etype} in {g['span']}",
            summary=f"{redact(msg)}{where}. Seen in {len(found)} error trace(s) in the window."
                    + _unreadable_note(len(traces), skipped),
            content=redact_obj({
                "exception_type": etype, "exception_message": msg, "span": g["span"],
                "trace_ids": g["trace_ids"], "error_trace_count": len(found),
                "span_attributes": g["span_attributes"], "stacktrace": stack,
                "app_frames": [f.to_dict() for f in frames if f.is_app], "stacktrace_truncated": truncated,
                **({"unreadable_traces": skipped} if skipped else {}),
            }),
            relevance=0.9, observed_at=g["first_seen"],
        ))
    return items


# ---------------------------------------------------------------- logs (Loki)

def collect_logs(ctx: CollectionContext) -> list[EvidenceItem]:
    s, svc = ctx.settings, ctx.service.telemetry_service_name
    data = ctx.get_json(
        f"{s.loki_url}/loki/api/v1/query_range", query=f'{{service_name="{svc}"}} | severity_text="ERROR"',
        start=int(ctx.window_start.timestamp() * 1e9), end=int(ctx.window_end.timestamp() * 1e9), limit=200,
    )
    streams = data.get("data", {}).get("result") or []
    entries = []
    for st in streams:
        labels = st.get("stream", {})
        for ts, line, *_ in st.get("values") or []:
            entries.append((int(ts), line, labels))
    if not entries:
        raise GapError("no ERROR logs for this service in the window")

    by_msg: Counter = Counter()
    sample: dict[str, tuple] = {}
    for ts, line, labels in entries:
        key = labels.get("exception_type") or line[:80]
        by_msg[key] += 1
        if key not in sample or ts < sample[key][0]:
            sample[key] = (ts, line, labels)

    items = []
    for key, n in by_msg.most_common(ctx.settings.evidence_max_items):
        ts, line, labels = sample[key]
        stack = redact(labels.get("exception_stacktrace", ""))
        if stack:
            _remember_frames(ctx, stack, "log")
        last = max(t for t, ln, lb in entries if (lb.get("exception_type") or ln[:80]) == key)
        items.append(EvidenceItem(
            kind="log", source="loki",
            title=f"{n} ERROR log line(s): {labels.get('exception_type') or 'error'}",
            summary=redact(line)[:300],
            content=redact_obj({
                "count": n, "first_at": _ts(ts).isoformat(), "last_at": _ts(last).isoformat(),
                "sample_line": line, "exception_type": labels.get("exception_type"),
                "exception_message": labels.get("exception_message"), "stacktrace": stack,
                "code_location": {k: labels.get(k) for k in ("code_file_path", "code_function_name",
                                                             "code_line_number") if labels.get(k)},
            }),
            relevance=0.8, observed_at=_ts(ts),
        ))
    return items


# ---------------------------------------------------------------- metrics (Prometheus)

# OpenTelemetry HTTP semantic-convention defaults; a service can override any of them in the catalog.
DEFAULT_METRICS = {
    "error_ratio": {
        "title": "5xx error ratio",
        "query": ('sum(rate(http_server_request_duration_seconds_count{service_name="{service}",'
                  'http_response_status_code=~"5.."}[1m])) / '
                  'sum(rate(http_server_request_duration_seconds_count{service_name="{service}"}[1m]))'),
        "unit": "ratio", "threshold": ERROR_RATIO_THRESHOLD,
    },
    "latency_p95": {
        "title": "p95 latency",
        "query": ('histogram_quantile(0.95, sum by (le) (rate('
                  'http_server_request_duration_seconds_bucket{service_name="{service}"}[1m])))'),
        "unit": "seconds", "threshold": 1.0,
    },
}


def prometheus_range(http: httpx.Client, settings: Settings, query: str, start: datetime, end: datetime,
                     step: str = "15s") -> list[tuple[float, float]] | None:
    """Points for a PromQL range, or None when Prometheus is unreadable."""
    try:
        resp = http.get(
            f"{settings.prometheus_url}/api/v1/query_range",
            params={"query": query, "start": start.timestamp(), "end": max(end.timestamp(), start.timestamp()),
                    "step": step},
            timeout=settings.check_timeout_seconds + 2,
        )
        resp.raise_for_status()
        result = (resp.json().get("data") or {}).get("result") or []
        return [(float(t), float(v)) for r in result[:1] for t, v in r["values"]
                if v not in ("NaN", "+Inf", "-Inf")]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def metric_specs(service: Service) -> list[dict]:
    """Error ratio, latency and saturation queries for a service, catalog values over defaults."""
    cfg = (service.config or {}).get("metrics", {}) or {}
    specs = []
    for name in ("error_ratio", "latency_p95"):
        spec = {**DEFAULT_METRICS[name], **(cfg.get(name) or {}), "name": name}
        specs.append(spec)
    for sat in cfg.get("saturation", []) or []:
        specs.append({"unit": "ratio", "threshold": 0.9, **sat})
    for spec in specs:
        spec["query"] = spec["query"].replace("{service}", service.telemetry_service_name)
    return specs


def _fmt(value: float, unit: str) -> str:
    if unit == "ratio":
        return f"{value:.0%}" if value >= 0.01 or value == 0 else f"{value:.1%}"
    if unit == "seconds":
        return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"
    return f"{value:.3g}"


def _metric_item(ctx: CollectionContext, spec: dict, points: list[tuple[float, float]]) -> EvidenceItem:
    detected = ctx.incident.detected_at.timestamp()
    before = [v for t, v in points if t < detected - 60]
    baseline = sum(before) / len(before) if before else 0.0
    peak_t, peak = max(points, key=lambda p: p[1])
    threshold, unit = float(spec["threshold"]), spec.get("unit", "ratio")
    onset = next((t for t, v in points if v > threshold), None)
    breached = peak > threshold or (unit == "ratio" and spec["name"] != "error_ratio" and peak >= threshold)
    step = max(1, len(points) // 60)
    series = [{"t": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(), "v": round(v, 4)} for t, v in points[::step]]
    at = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc)  # noqa: E731
    if breached:
        title = f"{spec['title']} peaked at {_fmt(peak, unit)} (threshold {_fmt(threshold, unit)})"
        summary = (f"Baseline {_fmt(baseline, unit)}, peak {_fmt(peak, unit)}"
                   + (f", above threshold from {at(onset).strftime('%H:%M:%S')} UTC" if onset else "") + ".")
    else:
        title = f"{spec['title']} stayed normal (peak {_fmt(peak, unit)})"
        summary = f"Never crossed {_fmt(threshold, unit)} in the window; baseline {_fmt(baseline, unit)}."
    return EvidenceItem(
        kind="metric", source="prometheus", title=title, summary=summary,
        content={"metric": spec["name"], "title": spec["title"], "query": spec["query"], "unit": unit,
                 "threshold": threshold, "breached": breached, "baseline": round(baseline, 4),
                 "peak": round(peak, 4), "peak_at": at(peak_t).isoformat(),
                 "onset_at": at(onset).isoformat() if onset else None, "series": series},
        # Normal metrics are kept on purpose: "latency was fine" helps rule causes out.
        relevance=(0.8 if spec["name"] != "latency_p95" else 0.75) if breached else 0.3,
        observed_at=at(onset) if onset else None,
    )


def collect_metrics(ctx: CollectionContext) -> list[EvidenceItem]:
    s = ctx.settings
    items, missing = [], []
    for spec in metric_specs(ctx.service):
        data = ctx.get_json(f"{s.prometheus_url}/api/v1/query_range", query=spec["query"],
                            start=ctx.window_start.timestamp(), end=ctx.window_end.timestamp(), step="15s")
        result = data.get("data", {}).get("result") or []
        points = [(float(t), float(v)) for r in result[:1] for t, v in r["values"]
                  if v not in ("NaN", "+Inf", "-Inf")]
        if points:
            items.append(_metric_item(ctx, spec, points))
        else:
            missing.append(spec["title"])
    if not items:
        raise GapError("no metric data for this service in the window: " + ", ".join(missing))
    # Breached metrics first.
    return sorted(items, key=lambda i: -i.relevance)


# ---------------------------------------------------------------- dependencies (Tempo client spans)

SLOW_CALL_SECONDS = 0.5


def _span_seconds(span: dict) -> float:
    return (int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0))) / 1e9


def collect_dependencies(ctx: CollectionContext) -> list[EvidenceItem]:
    """Outbound calls from the service, from slow or failing traces: who was slow or failing, and how much
    of each request the call took."""
    s, svc = ctx.settings, ctx.service.telemetry_service_name
    query = f'{{ resource.service.name = "{svc}" && (status = error || duration > 500ms) }}'
    found = ctx.get_json(f"{s.tempo_url}/api/search", q=query, start=int(ctx.window_start.timestamp()),
                         end=int(ctx.window_end.timestamp()), limit=30).get("traces") or []
    if not found:
        raise GapError("no slow or failing requests to inspect for dependency calls")

    # Slowest first: a slow dependency hides among fast failing requests that never reached it.
    found.sort(key=lambda t: t.get("durationMs") or 0, reverse=True)
    peers: dict[str, dict] = {}
    traces, skipped = _read_traces(ctx, found, 10)
    for summary, _trace_id, trace in traces:
        spans: dict[str, tuple[dict, str]] = {}
        for rs in trace.get("resourceSpans") or trace.get("batches") or []:
            name = _attrs(rs.get("resource", {}).get("attributes")).get("service.name", "")
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    spans[span.get("spanId", "")] = (span, name)
        roots = [sp for sp, n in spans.values() if n == svc and sp.get("kind") == "SPAN_KIND_SERVER"]
        root_seconds = max((_span_seconds(r) for r in roots), default=0.0)
        for span, name in spans.values():
            if name != svc or span.get("kind") != "SPAN_KIND_CLIENT":
                continue
            a = _attrs(span.get("attributes"))
            peer = a.get("peer.service") or a.get("server.address") or span.get("name", "unknown")
            p = peers.setdefault(peer, {"calls": 0, "errors": 0, "durations": [], "shares": [],
                                        "span_name": span.get("name"), "trace_ids": []})
            secs = _span_seconds(span)
            p["calls"] += 1
            p["durations"].append(secs)
            if root_seconds:
                p["shares"].append(min(1.0, secs / root_seconds))
            if (span.get("status") or {}).get("code") == "STATUS_CODE_ERROR":
                p["errors"] += 1
            if len(p["trace_ids"]) < 5:
                p["trace_ids"].append(summary["traceID"].rjust(32, "0"))
    if not peers:
        raise GapError(f"{len(found)} slow or failing request(s), but none made traced outbound calls"
                       + _unreadable_note(len(traces), skipped))

    items = []
    for peer, p in peers.items():
        avg = sum(p["durations"]) / len(p["durations"])
        share = sum(p["shares"]) / len(p["shares"]) if p["shares"] else 0.0
        error_rate = p["errors"] / p["calls"]
        slow, failing = avg >= SLOW_CALL_SECONDS, error_rate > 0
        if slow or failing:
            parts = []
            if slow:
                parts.append(f"avg {_fmt(avg, 'seconds')}, {share:.0%} of request time")
            if failing:
                parts.append(f"{error_rate:.0%} of calls failed")
            title = f"Calls to {peer} {'slow' if slow else ''}{' and ' if slow and failing else ''}{'failing' if failing else ''}"
            summary = f"{p['calls']} call(s) in {min(len(found), 6)} slow/failing request(s): " + "; ".join(parts) + "."
            relevance = 0.85
        else:
            title = f"Calls to {peer} were healthy"
            summary = f"{p['calls']} call(s), avg {_fmt(avg, 'seconds')}, no errors: {peer} is not the cause."
            relevance = 0.3
        items.append(EvidenceItem(
            kind="dependency", source="tempo", title=title, summary=summary + _unreadable_note(len(traces), skipped),
            content={"peer": peer, "span_name": p["span_name"], "calls": p["calls"], "errors": p["errors"],
                     "avg_seconds": round(avg, 3), "max_seconds": round(max(p["durations"]), 3),
                     "share_of_request": round(share, 3), "slow": slow, "failing": failing,
                     "trace_ids": p["trace_ids"], **({"unreadable_traces": skipped} if skipped else {})},
            relevance=relevance,
        ))
    return sorted(items, key=lambda i: -i.relevance)


# ---------------------------------------------------------------- deployments (catalog)

def collect_deployments(ctx: CollectionContext) -> list[EvidenceItem]:
    dep = ctx.deployment
    if dep is None:
        raise GapError("no deployment recorded for this service before the incident")
    detected = ctx.incident.detected_at
    deployed = dep.deployed_at if dep.deployed_at.tzinfo else dep.deployed_at.replace(tzinfo=timezone.utc)
    minutes = max(0.0, (detected - deployed).total_seconds() / 60)
    previous = next((d for d in ctx.deployments if d.id != dep.id and d.deployed_at < dep.deployed_at), None)
    recent = minutes <= 60
    files = ", ".join(dep.changed_files) or "no file list recorded"
    return [EvidenceItem(
        kind="deployment", source="catalog",
        title=f"{dep.version} deployed {minutes:.0f} min before the incident",
        summary=f"{dep.description} Changed: {files}.",
        content={"version": dep.version, "commit_sha": dep.commit_sha, "description": dep.description,
                 "changed_files": dep.changed_files, "deployed_at": deployed.isoformat(),
                 "minutes_before_incident": round(minutes, 1), "source": dep.source,
                 "previous_version": previous.version if previous else None,
                 "has_irreversible_migration": dep.has_irreversible_migration},
        relevance=0.85 if recent else 0.4, observed_at=deployed,
    )]


# ---------------------------------------------------------------- code (source tree)

def collect_code(ctx: CollectionContext, context_lines: int = 10) -> list[EvidenceItem]:
    root = ctx.service.source_root
    if not root:
        raise GapError("service catalog has no source_root for this service")
    if not ctx.frames:
        raise GapError("no application stack frames found in traces or logs")
    root_path = Path(root)
    if not root_path.is_dir():
        raise GapError(f"source root {root} is not readable from Nexora")

    changed = set(ctx.deployment.changed_files) if ctx.deployment else set()
    # A frame that only appears as a caller in another frame's chain (e.g. middleware that caught
    # the error) adds noise: keep the deepest frames only.
    callers = {(c["relative_path"], c["line"]) for fr in ctx.frames for c in fr.get("call_chain", [])[:-1]}
    frames = [fr for fr in ctx.frames if (fr["relative_path"], fr["line"]) not in callers] or ctx.frames
    seen, items = set(), []
    for fr in frames:
        key = (fr["relative_path"], fr["line"])
        if key in seen:
            continue
        seen.add(key)
        file_path = resolve_source_path(root_path, fr["relative_path"])  # never outside the source root
        if file_path is None:
            continue
        repo_path = file_path.relative_to(root_path.resolve()).as_posix()
        lines = file_path.read_text(errors="replace").splitlines()
        lo, hi = max(1, fr["line"] - context_lines), min(len(lines), fr["line"] + context_lines)
        snippet = [{"n": n, "text": redact(lines[n - 1]), "highlight": n == fr["line"]} for n in range(lo, hi + 1)]
        if not fr["source_line"] and 0 < fr["line"] <= len(lines):
            fr = {**fr, "source_line": lines[fr["line"] - 1].strip()}
        in_change = repo_path in changed or fr["relative_path"] in changed
        version = ctx.deployment.version if ctx.deployment else "the deployed version"
        items.append(EvidenceItem(
            kind="code", source="source",
            title=f"{repo_path}:{fr['line']} in {fr['function']}"
                  + (f" (changed in {ctx.deployment.version})" if in_change and ctx.deployment else ""),
            summary=f"Deepest application frame: `{redact(fr['source_line'])}`"
                    + (f". This file was changed by {version}." if in_change else "."),
            content={"file": repo_path, "line": fr["line"], "function": fr["function"],
                     "failing_line": redact(fr["source_line"]), "snippet": snippet,
                     "changed_in_deployment": in_change,
                     "deployment_version": ctx.deployment.version if ctx.deployment else None,
                     "call_chain": [{"file": c["relative_path"], "line": c["line"], "function": c["function"]}
                                    for c in fr.get("call_chain", [])],
                     "found_via": fr.get("origin")},
            relevance=0.95 if in_change else 0.75,
        ))
        if len(items) >= 3:
            break
    if not items:
        raise GapError("stack frames point to files that are not in the source tree")
    return items
