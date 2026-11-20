export interface PipelineStep {
  seq: number;
  stage: string;
  name: string;
  status: string;
  detail: string;
  duration_ms: number;
  finished_at: string;
}

export interface PipelineRun {
  id: string;
  title: string;
  trigger_kind: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  incident_id: string | null;
  incident_key: string | null;
  steps: PipelineStep[];
}

export interface IncidentSummary {
  id: string;
  key: string;
  title: string;
  service: string | null;
  environment: string | null;
  severity: string;
  status: string;
  active_alerts: number;
  detected_at: string;
  updated_at: string;
}

export interface IncidentDetail extends IncidentSummary {
  alerts: { id: string; title: string; status: string; severity: string; started_at: string; resolved_at: string | null }[];
  timeline: { at: string; kind: string; message: string }[];
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} failed: HTTP ${res.status}`);
  return res.json();
}

export const fetchRuns = () => getJson<PipelineRun[]>("/api/v1/pipeline/runs?limit=30");
export const fetchIncidents = () => getJson<IncidentSummary[]>("/api/v1/incidents");
export const fetchIncident = (id: string) => getJson<IncidentDetail>(`/api/v1/incidents/${id}`);

/** Stages each kind of pipeline run is expected to pass, in order. */
export const EVIDENCE_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "catalog", label: "Catalog" },
  { stage: "window", label: "Window" },
  { stage: "traces", label: "Traces" },
  { stage: "logs", label: "Logs" },
  { stage: "metrics", label: "Metrics" },
  { stage: "dependencies", label: "Dependencies" },
  { stage: "deployments", label: "Deployments" },
  { stage: "code", label: "Code" },
  { stage: "stored", label: "Stored" },
];

export const RCA_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "context", label: "Context" },
  { stage: "model", label: "Model" },
  { stage: "validate", label: "Validate" },
  { stage: "grounding", label: "Grounding" },
  { stage: "stored", label: "Stored" },
];

export const MITIGATION_PLAN_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "options", label: "Options" },
  { stage: "gates", label: "Gates" },
  { stage: "choose", label: "Choose" },
  { stage: "stored", label: "Stored" },
];

export const MITIGATION_EXECUTE_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "lock", label: "Lock" },
  { stage: "preflight", label: "Preflight" },
  { stage: "execute", label: "Execute" },
  { stage: "stored", label: "Stored" },
];

export const RECOVERY_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "waiting", label: "Waiting" },
  { stage: "signals", label: "Signals" },
  { stage: "verdict", label: "Verdict" },
  { stage: "undo", label: "Undo" },
];

export const FIX_STAGES: { stage: string; label: string }[] = [
  { stage: "queued", label: "Queued" },
  { stage: "context", label: "Context" },
  { stage: "baseline", label: "Baseline" },
  { stage: "repro", label: "Repro test" },
  { stage: "repro_fails", label: "Fails now" },
  { stage: "patch", label: "Patch" },
  { stage: "patch_checks", label: "Patch checks" },
  { stage: "verify", label: "Passes after" },
  { stage: "artifacts", label: "Patch files" },
  { stage: "pr", label: "Draft PR" },
];

export const EXPECTED_STAGES: { stage: string; label: string }[] = [
  { stage: "received", label: "Received" },
  { stage: "authenticated", label: "Authenticated" },
  { stage: "validated", label: "Validated" },
  { stage: "stored", label: "Stored" },
  { stage: "queued", label: "Queued" },
  { stage: "normalized", label: "Normalized" },
  { stage: "alerts_stored", label: "Alerts stored" },
  { stage: "incident_linked", label: "Incident" },
];

export interface StageCell {
  stage: string;
  label: string;
  status: string; // success | failed | warning | pending | not_reached
  detail: string;
}

/** Lines a run's recorded steps up against the expected stages, so gaps are visible. */
export function stagesFor(run: PipelineRun) {
  if (run.trigger_kind === "evidence_collection") return EVIDENCE_STAGES;
  if (run.trigger_kind === "rca_investigation") return RCA_STAGES;
  if (run.trigger_kind === "mitigation_plan") return MITIGATION_PLAN_STAGES;
  if (run.trigger_kind === "mitigation_execution") return MITIGATION_EXECUTE_STAGES;
  if (run.trigger_kind === "recovery_verification") return RECOVERY_STAGES;
  if (run.trigger_kind === "fix_proposal") return FIX_STAGES;
  return EXPECTED_STAGES;
}

export function stageCells(run: PipelineRun): StageCell[] {
  const byStage = new Map(run.steps.map((s) => [s.stage, s]));
  const failedAt = run.steps.find((s) => s.status === "failed");
  const ended = run.status !== "running";
  let stopped = false;
  return stagesFor(run).map(({ stage, label }) => {
    const step = byStage.get(stage);
    if (step) {
      if (step.status === "failed") stopped = true;
      return { stage, label, status: step.status, detail: step.detail };
    }
    if (stopped || (failedAt && ended) || (ended && run.status !== "success")) {
      return { stage, label, status: "not_reached", detail: "not reached" };
    }
    return { stage, label, status: run.status === "running" ? "pending" : "skipped", detail: "waiting for the worker" };
  });
}

export interface EvidenceItem {
  id: string;
  kind: "code" | "trace" | "log" | "metric" | "dependency" | "deployment" | "gap";
  source: string;
  title: string;
  summary: string;
  content: Record<string, any>;
  relevance: number;
  observed_at: string | null;
}

export interface EvidenceBundle {
  collection: { id: string; status: string; started_at: string; finished_at: string | null } | null;
  items: EvidenceItem[];
}

export const fetchEvidence = (id: string) => getJson<EvidenceBundle>(`/api/v1/incidents/${id}/evidence`);

export async function recollectEvidence(id: string): Promise<void> {
  const res = await fetch(`/api/v1/incidents/${id}/evidence/collect`, { method: "POST" });
  if (!res.ok) throw new Error(`Re-collect failed: HTTP ${res.status}`);
}

export interface RcaHypothesis {
  title: string;
  likelihood: number;
  status: "accepted" | "rejected" | "open";
  supporting: string[];
  contradicting: string[];
  reasoning: string;
}

export interface RcaReportBody {
  summary: string;
  root_cause: { category: string; component: string; file: string | null; line: number | null;
    function: string | null; explanation: string };
  trigger: { kind: string; release_version: string | null; explanation: string };
  symptom_vs_cause: string;
  confidence: number;
  model_confidence: number;
  evidence_refs: string[];
  hypotheses: RcaHypothesis[];
  missing_evidence: string[];
  next_checks: string[];
  mitigation_options: { action: string; target: string; reason: string; risk: string }[];
  evidence_index: Record<string, { id: string; kind: string; title: string }>;
}

export interface RcaResult {
  id: string;
  status: "succeeded" | "failed";
  provider: string;
  model: string;
  category: string | null;
  confidence: number | null;
  grounded: boolean;
  report: RcaReportBody;
  checks: { check: string; ok: boolean; detail: string }[];
  error: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
}

export interface RcaBundle {
  latest: RcaResult | null;
  in_progress: boolean;
  history: { id: string; status: string; provider: string; model: string; category: string | null;
    confidence: number | null; created_at: string }[];
}

export const fetchRca = (id: string) => getJson<RcaBundle>(`/api/v1/incidents/${id}/rca`);

export async function investigateAgain(id: string): Promise<void> {
  const res = await fetch(`/api/v1/incidents/${id}/rca/investigate`, { method: "POST" });
  if (!res.ok) throw new Error(`Investigate failed: HTTP ${res.status}`);
}

export interface MitigationGate {
  gate: string;
  ok: boolean;
  detail: string;
}

export interface MitigationPlan {
  id: string;
  incident_id: string;
  action_type: string;
  params: Record<string, unknown>;
  title: string;
  risk: string;
  approval_mode: string;
  gate_results: MitigationGate[];
  score: number;
  status: string;
  rationale: string;
  undo_plan: { action?: string; description?: string };
  target: string;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface RecoverySignal {
  metric: string;
  threshold: number | null;
  before: number | null;
  after: number | null;
  ok: boolean;
}

export interface RecoveryCheck {
  id: string;
  execution_id: string;
  window_seconds: number;
  signals: RecoverySignal[];
  verdict: string;
  checked_at: string;
}

export interface MitigationExecution {
  id: string;
  plan_id: string;
  service: string;
  environment: string;
  status: string;
  before_state: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  recovery: RecoveryCheck | null;
}

export interface MitigationBundle {
  chosen: MitigationPlan | null;
  blocked: MitigationPlan[];
  executions: MitigationExecution[];
  recovery: RecoveryCheck | null;
  no_safe_action: string | null;
  pending: boolean;
}

export const fetchMitigation = (id: string) =>
  getJson<MitigationBundle>(`/api/v1/incidents/${id}/mitigation`);

async function postDecision(planId: string, action: "approve" | "reject", comment: string): Promise<void> {
  const res = await fetch(`/api/v1/mitigation-plans/${planId}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment }),
  });
  if (!res.ok) throw new Error(`${action} failed: HTTP ${res.status}`);
}

export const approveMitigation = (planId: string, comment = "") => postDecision(planId, "approve", comment);
export const rejectMitigation = (planId: string, comment: string) => postDecision(planId, "reject", comment);

// ---------- Step 6: fix track ----------

export interface FixGate {
  gate: string;
  ok: boolean;
  detail: string;
}

export interface FixRun {
  name: string;
  exit_code: number | null;
  passed: number;
  failed: number;
  errors: number;
  timed_out: boolean;
  duration_ms: number;
  output: string;
  summary: string;
}

export interface FixAttempt {
  id: string;
  incident_id: string;
  rca_report_id: string | null;
  status: string;
  reason: string;
  provider: string;
  model: string;
  target_file: string | null;
  repro_test_path: string | null;
  repro_test_code: string;
  patch_diff: string;
  patch_files: string[];
  explanation: string;
  gates: FixGate[];
  runs: Record<string, FixRun>;
  artifacts_dir: string | null;
  pr_url: string | null;
  requested_by: string;
  llm_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface FixBundle {
  latest: FixAttempt | null;
  pending: boolean;
  history: { id: string; status: string; reason: string; created_at: string }[];
}

export interface FixConfig {
  enabled: boolean;
  provider: string;
  configured: boolean;
  sandbox: string;
  draft_pr: boolean;
}

export const fetchFix = (id: string) => getJson<FixBundle>(`/api/v1/incidents/${id}/fix-attempts`);
export const fetchFixConfig = () => getJson<FixConfig>("/api/v1/fix/config");

/** Ask for a fix attempt; resolves to the server's reason when it refuses (HTTP 409). */
export async function requestFix(id: string): Promise<string | null> {
  const res = await fetch(`/api/v1/incidents/${id}/fix-attempts`, { method: "POST" });
  if (res.status === 409) return ((await res.json()) as { detail: string }).detail;
  if (!res.ok) throw new Error(`Fix request failed: HTTP ${res.status}`);
  return null;
}
