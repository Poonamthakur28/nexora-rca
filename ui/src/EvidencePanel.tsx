import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import { type EvidenceBundle, type EvidenceItem, fetchEvidence, recollectEvidence } from "./pipelineApi";

const KIND_LABEL: Record<string, string> = {
  code: "Code",
  trace: "Trace",
  log: "Logs",
  metric: "Metric",
  dependency: "Dependency",
  deployment: "Deployment",
  gap: "Gap",
};

function Sparkline({ points }: { points: { t: string; v: number }[] }) {
  if (!points?.length) return null;
  const w = 260;
  const h = 48;
  const max = Math.max(0.05, ...points.map((p) => p.v));
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${((i / Math.max(1, points.length - 1)) * w).toFixed(1)},${(h - (p.v / max) * h).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={w} height={h} className="spark" role="img" aria-label="5xx ratio over time">
      <path d={d} fill="none" stroke="#b91c1c" strokeWidth="1.5" />
    </svg>
  );
}

function CodeBlock({ item }: { item: EvidenceItem }) {
  const c = item.content;
  return (
    <>
      <pre className="code">
        {(c.snippet as { n: number; text: string; highlight: boolean }[]).map((l) => (
          <div key={l.n} className={l.highlight ? "hl" : undefined}>
            <span className="ln">{l.n}</span>
            {l.text || " "}
          </div>
        ))}
      </pre>
      {c.call_chain?.length > 0 && (
        <div className="muted small-text">
          Call chain: {(c.call_chain as { file: string; line: number; function: string }[])
            .map((f) => `${f.function} (${f.file}:${f.line})`)
            .join(" → ")}
        </div>
      )}
    </>
  );
}

function Details({ item }: { item: EvidenceItem }) {
  const c = item.content;
  if (item.kind === "code") return <CodeBlock item={item} />;
  if (item.kind === "trace" || item.kind === "log")
    return c.stacktrace ? <pre className="stack">{String(c.stacktrace).trim()}</pre> : null;
  if (item.kind === "metric")
    return (
      <>
        <Sparkline points={c.series} />
        {c.threshold != null && (
          <div className="muted small-text">
            Threshold {c.threshold} · {c.breached ? "breached" : "within threshold"}
          </div>
        )}
      </>
    );
  if (item.kind === "dependency")
    return (
      <div className="dep-grid small-text">
        <span>Peer</span><b>{c.peer}</b>
        <span>Calls</span><b>{c.calls}</b>
        <span>Avg / max</span><b>{c.avg_seconds}s / {c.max_seconds}s</b>
        <span>Share of request</span><b>{Math.round((c.share_of_request ?? 0) * 100)}%</b>
        <span>Errors</span><b>{c.errors}</b>
        <span>Verdict</span><b>{c.failing ? "failing" : c.slow ? "slow" : "healthy"}</b>
      </div>
    );
  if (item.kind === "deployment")
    return (
      <div className="muted small-text">
        Previous version: {c.previous_version ?? "—"} · commit {c.commit_sha ?? "—"} · source {c.source}
      </div>
    );
  return null;
}

function EvidenceCard({ item }: { item: EvidenceItem }) {
  const [open, setOpen] = useState(item.kind === "code");
  const expandable = item.kind !== "gap";
  return (
    <div className={`ev ev-${item.kind}`}>
      <div className="ev-head">
        <span className="ev-kind">{KIND_LABEL[item.kind] ?? item.kind}</span>
        <span className="ev-title">{item.title}</span>
        <span className="ev-src muted">{item.source}</span>
        {expandable && (
          <button className="small" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide" : "Show"}
          </button>
        )}
      </div>
      <div className="ev-summary">{item.summary}</div>
      {open && expandable && <Details item={item} />}
    </div>
  );
}

export function EvidencePanel({ incidentId }: { incidentId: string }) {
  const [bundle, setBundle] = useState<EvidenceBundle | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setBundle(await fetchEvidence(incidentId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [incidentId]);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const onRecollect = async () => {
    setBusy(true);
    try {
      await recollectEvidence(incidentId);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const items = bundle?.items ?? [];
  const found = items.filter((i) => i.kind !== "gap");
  const gaps = items.filter((i) => i.kind === "gap");

  return (
    <div>
      <div className="ev-bar">
        <h3>Evidence</h3>
        {bundle?.collection && <StatusPill status={bundle.collection.status} />}
        <span className="muted small-text">
          {found.length} item(s), {gaps.length} gap(s)
        </span>
        <button className="small" onClick={() => void onRecollect()} disabled={busy}>
          {busy ? "Queuing…" : "Collect again"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {!bundle?.collection && <p className="hint">No evidence collected yet.</p>}
      {found.map((i) => (
        <EvidenceCard key={i.id} item={i} />
      ))}
      {gaps.map((i) => (
        <EvidenceCard key={i.id} item={i} />
      ))}
    </div>
  );
}
