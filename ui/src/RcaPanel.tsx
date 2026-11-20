import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import { type RcaBundle, fetchRca, investigateAgain } from "./pipelineApi";

const CATEGORY_LABEL: Record<string, string> = {
  code: "Code defect",
  dependency: "Dependency",
  resource: "Resource exhaustion",
  configuration: "Configuration",
  infrastructure: "Infrastructure",
  capacity: "Capacity",
  unknown: "Unknown",
};

const pct = (v: number | null | undefined) => `${Math.round((v ?? 0) * 100)}%`;

function Refs({ refs, index }: { refs: string[]; index: Record<string, { kind: string; title: string }> }) {
  if (!refs?.length) return null;
  return (
    <span className="refs">
      {refs.map((r) => (
        <span key={r} className="ref" title={index[r] ? `${index[r].kind}: ${index[r].title}` : "unknown evidence id"}>
          {r}
        </span>
      ))}
    </span>
  );
}

export function RcaPanel({ incidentId }: { incidentId: string }) {
  const [data, setData] = useState<RcaBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await fetchRca(incidentId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [incidentId]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  const again = async () => {
    setBusy(true);
    try {
      await investigateAgain(incidentId);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return <RcaView data={data} error={error} busy={busy} onAgain={() => void again()} />;
}

export function RcaView({ data, error = null, busy = false, onAgain }: {
  data: RcaBundle | null; error?: string | null; busy?: boolean; onAgain?: () => void;
}) {
  const r = data?.latest;
  const rep = r?.report;
  return (
    <section className="rca">
      <div className="ev-bar">
        <h3>Root cause analysis</h3>
        {data?.in_progress && <StatusPill status="running" />}
        {r && <span className="muted small-text">via {r.provider}{r.model ? ` / ${r.model}` : ""}{r.latency_ms != null ? ` · ${(r.latency_ms / 1000).toFixed(1)} s` : ""}</span>}
        <button className="small" onClick={onAgain} disabled={busy || data?.in_progress}>
          {busy ? "Queuing…" : "Investigate again"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {!r && !data?.in_progress && <p className="muted">No investigation yet. It starts after evidence is collected.</p>}
      {r?.status === "failed" && (
        <div className="rca-card failed">
          <b>Investigation failed.</b> <span>{r.error}</span>
        </div>
      )}
      {r?.status === "succeeded" && rep && (
        <div className="rca-card">
          <div className="rca-top">
            <span className={`cat cat-${rep.root_cause.category}`}>{CATEGORY_LABEL[rep.root_cause.category] ?? rep.root_cause.category}</span>
            <div className="conf" title={`model said ${pct(rep.model_confidence)}; after grounding ${pct(rep.confidence)}`}>
              <div className="conf-bar"><div style={{ width: pct(rep.confidence) }} /></div>
              <span>{pct(rep.confidence)} confidence</span>
            </div>
            {!r.grounded && <StatusPill status="warning" />}
          </div>
          <p className="rca-summary">{rep.summary}</p>
          <dl className="rca-grid">
            <dt>Cause</dt>
            <dd>
              {rep.root_cause.component}
              {rep.root_cause.file && (
                <code>
                  {" "}
                  {rep.root_cause.file}
                  {rep.root_cause.line ? `:${rep.root_cause.line}` : ""}
                  {rep.root_cause.function ? ` ${rep.root_cause.function}()` : ""}
                </code>
              )}
              <div className="muted">{rep.root_cause.explanation}</div>
            </dd>
            <dt>Trigger</dt>
            <dd>
              {rep.trigger.kind}
              {rep.trigger.release_version ? ` · ${rep.trigger.release_version}` : ""}
              {rep.trigger.explanation && <div className="muted">{rep.trigger.explanation}</div>}
            </dd>
            {rep.symptom_vs_cause && (
              <>
                <dt>Symptom vs cause</dt>
                <dd className="muted">{rep.symptom_vs_cause}</dd>
              </>
            )}
            <dt>Evidence</dt>
            <dd>
              <Refs refs={rep.evidence_refs} index={rep.evidence_index} />
            </dd>
          </dl>

          {rep.hypotheses.length > 0 && (
            <>
              <h4>Hypotheses</h4>
              <ul className="hyps">
                {rep.hypotheses.map((h, i) => (
                  <li key={i} className={`hyp hyp-${h.status}`}>
                    <span className="hyp-status">{h.status}</span> <b>{h.title}</b> <span className="muted">{pct(h.likelihood)}</span>
                    <Refs refs={h.supporting} index={rep.evidence_index} />
                    {h.contradicting.length > 0 && (
                      <>
                        <span className="muted"> against </span>
                        <Refs refs={h.contradicting} index={rep.evidence_index} />
                      </>
                    )}
                    {h.reasoning && <div className="muted small-text">{h.reasoning}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}

          {rep.mitigation_options.length > 0 && (
            <>
              <h4>Mitigation options <span className="muted small-text">(suggestions; not executed)</span></h4>
              <ul className="mitig">
                {rep.mitigation_options.map((m, i) => (
                  <li key={i}>
                    <span className={`risk risk-${m.risk}`}>{m.risk}</span> <b>{m.action.replace("_", " ")}</b> {m.target}
                    <span className="muted"> · {m.reason}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          {rep.next_checks.length > 0 && (
            <>
              <h4>Next checks</h4>
              <ul>{rep.next_checks.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          )}

          <h4>Grounding</h4>
          <ul className="checks">
            {r.checks.map((c, i) => (
              <li key={i}>
                {c.ok ? "✓" : "✗"} {c.check} <span className="muted">· {c.detail}</span>
              </li>
            ))}
          </ul>
          {rep.missing_evidence.length > 0 && (
            <p className="muted small-text">Missing evidence: {rep.missing_evidence.join("; ")}</p>
          )}
        </div>
      )}
    </section>
  );
}
