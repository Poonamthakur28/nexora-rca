import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import { type PipelineRun, fetchRuns, stageCells } from "./pipelineApi";
import { formatLatency, statusStyle } from "./status";

function StageChip({ label, status, detail }: { label: string; status: string; detail: string }) {
  const s = statusStyle(status);
  return (
    <div className="stage" style={{ borderColor: s.color, background: s.background }} title={detail}>
      <span className="stage-dot" style={{ background: s.color }} />
      <span style={{ color: s.color }}>{label}</span>
    </div>
  );
}

function RunCard({ run, onOpenIncident }: { run: PipelineRun; onOpenIncident: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const cells = stageCells(run);
  return (
    <section className="card run">
      <div className="run-head">
        <div>
          <div className="run-title">{run.title}</div>
          <div className="muted">
            {new Date(run.started_at).toLocaleTimeString()} · {run.trigger_kind.replace("_", " ")}
            {run.incident_key && run.incident_id && (
              <>
                {" · "}
                <button className="link" onClick={() => onOpenIncident(run.incident_id!)}>
                  {run.incident_key}
                </button>
              </>
            )}
          </div>
        </div>
        <div className="run-actions">
          <StatusPill status={run.status} />
          <button className="small" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide steps" : "Show steps"}
          </button>
        </div>
      </div>
      <div className="stages">
        {cells.map((c, i) => (
          <div key={c.stage} className="stage-wrap">
            <StageChip label={c.label} status={c.status} detail={c.detail} />
            {i < cells.length - 1 && <span className="stage-arrow">›</span>}
          </div>
        ))}
      </div>
      {open && (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Step</th>
              <th>Status</th>
              <th>Took</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {run.steps.map((s) => (
              <tr key={s.seq}>
                <td className="muted">{s.seq}</td>
                <td className="name">{s.name}</td>
                <td>
                  <StatusPill status={s.status} />
                </td>
                <td className="muted">{formatLatency(s.duration_ms)}</td>
                <td className="detail">{s.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function PipelinePage({ onOpenIncident }: { onOpenIncident: (id: string) => void }) {
  const [runs, setRuns] = useState<PipelineRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRuns(await fetchRuns());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(id);
  }, [refresh]);

  return (
    <div>
      <header className="top">
        <div>
          <h1>Pipeline</h1>
          <p className="hint">
            Every alert delivery from Alertmanager, stage by stage. Green passed, red failed, blue is waiting for the
            worker. Refreshes every 5 s.
          </p>
        </div>
        <button className="primary" onClick={() => void refresh()}>
          Refresh
        </button>
      </header>
      {error && <div className="error">{error}</div>}
      {runs && runs.length === 0 && (
        <section className="card">
          <p className="hint">
            No deliveries yet. On the Checks tab: Enable bug, Start steady load, and wait about 2 minutes for
            Alertmanager to send the alert.
          </p>
        </section>
      )}
      {runs?.map((r) => <RunCard key={r.id} run={r} onOpenIncident={onOpenIncident} />)}
    </div>
  );
}
