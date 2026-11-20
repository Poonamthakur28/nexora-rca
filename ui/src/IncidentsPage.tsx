import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import { EvidencePanel } from "./EvidencePanel";
import { MitigationPanel } from "./MitigationPanel";
import { RcaPanel } from "./RcaPanel";
import { type IncidentDetail, type IncidentSummary, fetchIncident, fetchIncidents } from "./pipelineApi";

const SEVERITY_COLORS: Record<string, string> = {
  critical: "#b91c1c",
  high: "#c2410c",
  medium: "#b45309",
  low: "#4b5563",
  info: "#4b5563",
  unknown: "#4b5563",
};

function Severity({ value }: { value: string }) {
  return (
    <span className="sev" style={{ color: SEVERITY_COLORS[value] ?? "#4b5563" }}>
      {value}
    </span>
  );
}

export function IncidentsPage({ selectedId, onSelect }: { selectedId: string | null; onSelect: (id: string) => void }) {
  const [incidents, setIncidents] = useState<IncidentSummary[] | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await fetchIncidents();
      setIncidents(list);
      const id = selectedId ?? list[0]?.id;
      if (id) {
        setDetail(await fetchIncident(id));
        if (!selectedId) onSelect(id);
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selectedId, onSelect]);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  return (
    <div>
      <header className="top">
        <div>
          <h1>Incidents</h1>
          <p className="hint">Incidents opened from real alerts, with their alerts and timeline.</p>
        </div>
      </header>
      {error && <div className="error">{error}</div>}
      {incidents && incidents.length === 0 && (
        <section className="card">
          <p className="hint">No incidents yet.</p>
        </section>
      )}
      {incidents && incidents.length > 0 && (
        <div className="split">
          <section className="card list">
            {incidents.map((i) => (
              <button
                key={i.id}
                className={`incident-row ${i.id === (selectedId ?? detail?.id) ? "active" : ""}`}
                onClick={() => onSelect(i.id)}
              >
                <div className="row-top">
                  <strong>{i.key}</strong> <Severity value={i.severity} />
                </div>
                <div className="row-title">{i.title}</div>
                <div className="muted">
                  {i.service} · {i.status} · {i.active_alerts} active
                </div>
              </button>
            ))}
          </section>
          {detail && (
            <section className="card detail-card">
              <div className="row-top">
                <h2>
                  {detail.key} · {detail.title}
                </h2>
              </div>
              <div className="facts">
                <div>
                  <span className="muted">Status</span>
                  <strong>{detail.status}</strong>
                </div>
                <div>
                  <span className="muted">Severity</span>
                  <Severity value={detail.severity} />
                </div>
                <div>
                  <span className="muted">Service</span>
                  <strong>{detail.service ?? "—"}</strong>
                </div>
                <div>
                  <span className="muted">Environment</span>
                  <strong>{detail.environment ?? "—"}</strong>
                </div>
                <div>
                  <span className="muted">Active alerts</span>
                  <strong>{detail.active_alerts}</strong>
                </div>
                <div>
                  <span className="muted">Detected</span>
                  <strong>{new Date(detail.detected_at).toLocaleTimeString()}</strong>
                </div>
              </div>

              <h3>Alerts</h3>
              <table>
                <thead>
                  <tr>
                    <th>Alert</th>
                    <th>Status</th>
                    <th>Severity</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.alerts.map((a) => (
                    <tr key={a.id}>
                      <td className="name">{a.title}</td>
                      <td>
                        <StatusPill status={a.status} />
                      </td>
                      <td>
                        <Severity value={a.severity} />
                      </td>
                      <td className="muted">{new Date(a.started_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <RcaPanel incidentId={detail.id} />

              <MitigationPanel incidentId={detail.id} />

              <EvidencePanel incidentId={detail.id} />

              <h3>Timeline</h3>
              <ol className="timeline">
                {detail.timeline.map((e, i) => (
                  <li key={i}>
                    <span className="muted">{new Date(e.at).toLocaleTimeString()}</span> {e.message}
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
