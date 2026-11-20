import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import {
  type MitigationBundle,
  type MitigationPlan,
  approveMitigation,
  fetchMitigation,
  rejectMitigation,
} from "./pipelineApi";

const label = (value: string) => value.replace(/_/g, " ");

function fmt(value: number | null | undefined) {
  if (value == null) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export function MitigationPanel({ incidentId }: { incidentId: string }) {
  const [data, setData] = useState<MitigationBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await fetchMitigation(incidentId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [incidentId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!data?.pending) return;
    const t = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(t);
  }, [data?.pending, load]);

  const decide = async (kind: "approve" | "reject", comment: string) => {
    if (!data?.chosen) return;
    setBusy(true);
    try {
      if (kind === "approve") await approveMitigation(data.chosen.id, comment);
      else await rejectMitigation(data.chosen.id, comment);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <MitigationView
      data={data}
      error={error}
      busy={busy}
      onApprove={(comment) => void decide("approve", comment)}
      onReject={(comment) => void decide("reject", comment)}
    />
  );
}

export function MitigationView({ data, error = null, busy = false, onApprove, onReject }: {
  data: MitigationBundle | null; error?: string | null; busy?: boolean;
  onApprove?: (comment: string) => void; onReject?: (comment: string) => void;
}) {
  const [comment, setComment] = useState("");
  const chosen = data?.chosen ?? null;
  const proposed = chosen?.status === "proposed";
  const execution = data?.executions[0] ?? null;
  const recovery = data?.recovery ?? execution?.recovery ?? null;

  return (
    <section className="mitigation" aria-labelledby="mitigation-heading">
      <div className="ev-bar">
        <h3 id="mitigation-heading">Mitigation</h3>
        {data?.pending && <StatusPill status="running" />}
        {chosen && <StatusPill status={chosen.status} />}
      </div>
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="muted">Loading mitigation…</p>}
      {data && !chosen && data.pending && <p className="muted">Planning a mitigation…</p>}
      {data && !chosen && !data.pending && (
        <div className="rca-card">
          <p>
            <b>No safe action.</b> {data.no_safe_action || "No mitigation plan was proposed."}
          </p>
        </div>
      )}
      {chosen && (
        <ApprovalCard
          plan={chosen}
          proposed={proposed}
          comment={comment}
          busy={busy}
          onComment={setComment}
          onApprove={onApprove}
          onReject={onReject}
        />
      )}
      {execution && (
        <div className="rca-card mit-follow">
          <h4>Execution</h4>
          <p>
            <StatusPill status={execution.status} /> {execution.service} · {execution.environment}
            {execution.error ? <span className="muted"> · {execution.error}</span> : null}
          </p>
          {recovery && (
            <>
              <h4>Recovery</h4>
              <p>
                <StatusPill status={recovery.verdict} /> after {recovery.window_seconds}s
              </p>
              <ul className="checks">
                {recovery.signals.map((s, i) => (
                  <li key={i}>
                    {s.ok ? "✓" : "✗"} {label(s.metric)} {fmt(s.before)} → {fmt(s.after)}
                    <span className="muted"> · threshold {fmt(s.threshold)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
      {data && data.blocked.length > 0 && (
        <details className="mit-blocked">
          <summary>
            {data.blocked.length} plan{data.blocked.length === 1 ? "" : "s"} evaluated but blocked
          </summary>
          <ul className="mitig">
            {data.blocked.map((p) => (
              <li key={p.id}>
                <b>{p.title}</b> <span className={`risk risk-${p.risk}`}>{p.risk}</span>
                <ul className="checks">
                  {p.gate_results.filter((g) => !g.ok).map((g, i) => (
                    <li key={i}>
                      ✗ {label(g.gate)} <span className="muted">· {g.detail}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function ApprovalCard({ plan, proposed, comment, busy, onComment, onApprove, onReject }: {
  plan: MitigationPlan; proposed: boolean; comment: string; busy: boolean;
  onComment: (value: string) => void;
  onApprove?: (comment: string) => void; onReject?: (comment: string) => void;
}) {
  return (
    <div className="rca-card mit-card">
      <div className="rca-top">
        <strong className="mit-title">{plan.title}</strong>
        <span className={`risk risk-${plan.risk}`}>{plan.risk}</span>
        <span className="muted">score {plan.score.toFixed(2)}</span>
      </div>
      <dl className="rca-grid">
        <dt>Action</dt>
        <dd>{label(plan.action_type)}</dd>
        <dt>Target</dt>
        <dd>{plan.target || "—"}</dd>
        <dt>Why</dt>
        <dd>{plan.rationale}</dd>
        <dt>Undo</dt>
        <dd>{plan.undo_plan?.description || plan.undo_plan?.action || "—"}</dd>
      </dl>
      <h4>Gates</h4>
      <ul className="checks">
        {plan.gate_results.map((g, i) => (
          <li key={i}>
            {g.ok ? "✓" : "✗"} {label(g.gate)} <span className="muted">· {g.detail}</span>
          </li>
        ))}
      </ul>
      <div className="mit-actions">
        <label htmlFor="mitigation-comment">Comment</label>
        <textarea
          id="mitigation-comment"
          name="comment"
          rows={2}
          value={comment}
          onChange={(e) => onComment(e.target.value)}
          aria-describedby="mitigation-comment-help"
          disabled={!proposed || busy}
        />
        <small id="mitigation-comment-help" className="help-text">
          Required to reject this plan. Optional when approving.
        </small>
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={!proposed || busy}
            onClick={() => onApprove?.(comment)}
          >
            Approve
          </button>
          <button
            type="button"
            disabled={!proposed || busy || !comment.trim()}
            onClick={() => onReject?.(comment)}
          >
            Reject
          </button>
        </div>
        {!proposed && (
          <p className="muted small-text">
            Approve is available only while the plan is proposed.
            {plan.decided_by ? ` Decided by ${plan.decided_by}.` : ""}
          </p>
        )}
      </div>
    </div>
  );
}
