import { useCallback, useEffect, useState } from "react";
import { StatusPill } from "./ChecksPage";
import { type FixBundle, type FixConfig, fetchFix, fetchFixConfig, requestFix } from "./pipelineApi";

const GATE_LABEL: Record<string, string> = {
  baseline_passes_before: "Existing tests pass on the current code",
  repro_test_safe: "Reproduction test is safe to run",
  repro_fails_before: "Gate 1: reproduction test fails on the current code",
  patch_checks: "Patch stays in scope, applies and parses",
  repro_passes_after: "Gate 2: reproduction test passes with the patch",
  baseline_passes_after: "Existing tests still pass with the patch",
  draft_pr: "Draft pull request",
};

const RUN_LABEL: Record<string, string> = {
  baseline_before: "Existing tests, before",
  repro_before: "Reproduction test, before",
  repro_after: "Reproduction test, after",
  baseline_after: "Existing tests, after",
};

export function FixPanel({ incidentId }: { incidentId: string }) {
  const [data, setData] = useState<FixBundle | null>(null);
  const [config, setConfig] = useState<FixConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await fetchFix(incidentId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [incidentId]);

  useEffect(() => {
    void load();
    void fetchFixConfig().then(setConfig).catch(() => setConfig(null));
  }, [load]);

  useEffect(() => {
    if (!data?.pending) return;
    const t = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(t);
  }, [data?.pending, load]);

  const ask = async () => {
    setBusy(true);
    try {
      setNotice(await requestFix(incidentId));
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return <FixView data={data} config={config} error={error} notice={notice} busy={busy} onRequest={() => void ask()} />;
}

export function FixView({ data, config = null, error = null, notice = null, busy = false, onRequest }: {
  data: FixBundle | null; config?: FixConfig | null; error?: string | null; notice?: string | null;
  busy?: boolean; onRequest?: () => void;
}) {
  const a = data?.latest ?? null;
  const running = data?.pending ?? false;
  return (
    <section className="fix" aria-labelledby="fix-heading">
      <div className="ev-bar">
        <h3 id="fix-heading">Fix</h3>
        {a && <StatusPill status={a.status} />}
        {onRequest && (
          <button type="button" className="small" disabled={busy || running} onClick={onRequest}>
            {a ? "Try again" : "Propose a fix"}
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="muted" role="status">{notice}</p>}
      {config && !config.configured && (
        <p className="muted">
          The fix track needs an LLM provider (FIX_PROVIDER or RCA_PROVIDER = anthropic, openai or ollama); the
          rule-based baseline cannot write tests or patches.
        </p>
      )}
      {!data && !error && <p className="muted">Loading fix attempts…</p>}
      {data && !a && <p className="muted">No fix attempted. Fixes are tried for grounded code and resource defects.</p>}
      {a && (
        <div className={`rca-card${["failed", "patch_rejected", "verify_failed"].includes(a.status) ? " failed" : ""}`}>
          {running && <p className="muted">Working: writing and checking a reproduction test and a patch…</p>}
          {a.reason && <p><b>Stopped:</b> {a.reason}</p>}
          <dl className="rca-grid">
            {a.target_file && (<><dt>Target</dt><dd><code>{a.target_file}</code></dd></>)}
            {a.provider && (<><dt>Written by</dt><dd>{a.provider}{a.model ? ` / ${a.model}` : ""} · {a.llm_calls} call(s)</dd></>)}
            {a.explanation && (<><dt>Change</dt><dd>{a.explanation}</dd></>)}
            {a.pr_url && (<><dt>Draft PR</dt><dd><a href={a.pr_url} target="_blank" rel="noreferrer">{a.pr_url}</a></dd></>)}
            {a.artifacts_dir && (<><dt>Patch files</dt><dd><code>{a.artifacts_dir}</code></dd></>)}
          </dl>
          {a.gates.length > 0 && (
            <>
              <h4>Checks</h4>
              <ul className="checks">
                {a.gates.map((g, i) => (
                  <li key={`${g.gate}-${i}`} className={g.ok ? "ok" : "bad"}>
                    {g.ok ? "✓" : "✗"} {GATE_LABEL[g.gate] ?? g.gate} <span className="muted">· {g.detail}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {Object.keys(a.runs).length > 0 && (
            <>
              <h4>Sandbox runs</h4>
              <ul className="checks">
                {Object.values(a.runs).map((r) => (
                  <li key={r.name}>
                    {RUN_LABEL[r.name] ?? r.name}: <b>{r.summary}</b>
                  </li>
                ))}
              </ul>
            </>
          )}
          {a.repro_test_code && (
            <details className="mit-blocked">
              <summary>Reproduction test ({a.repro_test_path})</summary>
              <pre className="stack">{a.repro_test_code}</pre>
            </details>
          )}
          {a.patch_diff && (
            <details className="mit-blocked" open>
              <summary>Patch ({a.patch_files.join(", ")})</summary>
              <pre className="stack diff">
                {a.patch_diff.split("\n").map((line, i) => (
                  <div key={i} className={line.startsWith("+") && !line.startsWith("+++") ? "add"
                    : line.startsWith("-") && !line.startsWith("---") ? "del" : undefined}>{line || " "}</div>
                ))}
              </pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
