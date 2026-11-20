import { useCallback, useEffect, useState } from "react";
import { type ActionName, type BugInfo, type CheckResult, type SystemChecksReport, fetchBugs, fetchChecks, runAction } from "./api";
import { countByStatus, formatLatency, statusStyle } from "./status";

export function StatusPill({ status }: { status: string }) {
  const s = statusStyle(status);
  return (
    <span className="pill" style={{ color: s.color, background: s.background }}>
      {s.label}
    </span>
  );
}

function ResultTable({ title, hint, results }: { title: string; hint: string; results: CheckResult[] }) {
  const counts = countByStatus(results.map((r) => r.status));
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          <p className="hint">{hint}</p>
        </div>
        <div className="counts">
          <span className="ok">{counts.success} ok</span>
          {counts.warning > 0 && <span className="warn">{counts.warning} warning</span>}
          {counts.failed > 0 && <span className="bad">{counts.failed} failed</span>}
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Check</th>
            <th>Status</th>
            <th>Latency</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.id}>
              <td className="name">{r.name}</td>
              <td>
                <StatusPill status={r.status} />
              </td>
              <td className="muted">{formatLatency(r.latency_ms)}</td>
              <td className="detail">{r.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const ACTIONS: { action: ActionName; label: string; help: string }[] = [
  { action: "enable_bug", label: "Enable bug", help: "Turn on the selected failure in shop-demo" },
  { action: "start_load", label: "Start steady load", help: "3 requests/s for 3 minutes" },
  { action: "generate_traffic", label: "Burst of 50 requests", help: "One quick burst" },
  { action: "stop_load", label: "Stop load", help: "Stop the steady load" },
  { action: "disable_bug", label: "Disable bug", help: "Turn off the selected failure" },
];

export function ChecksPage() {
  const [report, setReport] = useState<SystemChecksReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [actionLog, setActionLog] = useState<CheckResult[]>([]);
  const [busyAction, setBusyAction] = useState<ActionName | null>(null);
  const [bugs, setBugs] = useState<Record<string, BugInfo>>({});
  const [bug, setBug] = useState("code_null_tax");

  const loadBugs = useCallback(async () => {
    try {
      setBugs(await fetchBugs());
    } catch {
      setBugs({});
    }
  }, []);

  useEffect(() => {
    void loadBugs();
  }, [loadBugs]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setReport(await fetchChecks());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => void refresh(), 10_000);
    return () => window.clearInterval(id);
  }, [autoRefresh, refresh]);

  const onAction = async (action: ActionName) => {
    setBusyAction(action);
    try {
      const usesBug = action === "enable_bug" || action === "disable_bug";
      const result = await runAction(action, 50, usesBug ? bug : undefined);
      setActionLog((log) => [result, ...log].slice(0, 8));
    } catch (e) {
      setActionLog((log) =>
        [
          {
            id: `action-${action}`,
            name: action,
            category: "action" as const,
            status: "failed" as const,
            latency_ms: null,
            detail: e instanceof Error ? e.message : String(e),
            checked_at: new Date().toISOString(),
          },
          ...log,
        ].slice(0, 8),
      );
    } finally {
      setBusyAction(null);
      void refresh();
      void loadBugs();
    }
  };

  return (
    <div>
      <header className="top">
        <div>
          <h1>System checks</h1>
          <p className="hint">
            Is every component up, and does a real error in shop-demo reach traces, logs, metrics, alerts and a
            Nexora incident?
          </p>
        </div>
        <div className="top-right">
          {report && (
            <div className="overall">
              Overall <StatusPill status={report.overall} />
            </div>
          )}
          <label className="toggle">
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            Auto-refresh 10 s
          </label>
          <button className="primary" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Running…" : "Run checks"}
          </button>
        </div>
      </header>

      {error && <div className="error">Could not reach the Nexora API: {error}</div>}

      {report && (
        <>
          <ResultTable
            title="Components"
            hint="Is each part of the stack up and ready?"
            results={report.components}
          />
          <ResultTable
            title="Signals"
            hint="Does shop-demo's data arrive? Warnings are normal until you enable the bug and send traffic."
            results={report.signals}
          />
        </>
      )}

      <section className="card">
        <div className="card-head">
          <div>
            <h2>Actions</h2>
            <p className="hint">Create a real incident: pick a failure, enable it, start steady load, wait about 2 minutes.</p>
          </div>
        </div>
        {Object.keys(bugs).length > 0 && (
          <div className="bug-picker">
            <label htmlFor="bug-select">Planted failure</label>
            <select id="bug-select" value={bug} onChange={(e) => setBug(e.target.value)}>
              {Object.entries(bugs).map(([name, info]) => (
                <option key={name} value={name}>
                  {name} ({info.kind}){info.enabled ? " — ON" : ""}
                </option>
              ))}
            </select>
            {bugs[bug] && <span className="muted small-text">{bugs[bug].description}</span>}
          </div>
        )}
        <div className="actions">
          {ACTIONS.map((a) => (
            <button key={a.action} onClick={() => void onAction(a.action)} disabled={busyAction !== null} title={a.help}>
              {busyAction === a.action ? "Running…" : a.label}
            </button>
          ))}
        </div>
        {actionLog.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Action</th>
                <th>Status</th>
                <th>Latency</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {actionLog.map((r, i) => (
                <tr key={`${r.id}-${i}`}>
                  <td className="name">{r.name}</td>
                  <td>
                    <StatusPill status={r.status} />
                  </td>
                  <td className="muted">{formatLatency(r.latency_ms)}</td>
                  <td className="detail">{r.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {report && <footer className="muted">Last checked {new Date(report.checked_at).toLocaleTimeString()}</footer>}
    </div>
  );
}
