export type CheckStatus = "success" | "failed" | "warning" | "skipped";

export interface CheckResult {
  id: string;
  name: string;
  category: "component" | "signal" | "action";
  status: CheckStatus;
  latency_ms: number | null;
  detail: string;
  checked_at: string;
}

export interface SystemChecksReport {
  overall: CheckStatus;
  components: CheckResult[];
  signals: CheckResult[];
  checked_at: string;
}

export type ActionName = "enable_bug" | "disable_bug" | "generate_traffic" | "start_load" | "stop_load";

export async function fetchChecks(): Promise<SystemChecksReport> {
  const res = await fetch("/api/v1/system-checks");
  if (!res.ok) throw new Error(`Checks request failed: HTTP ${res.status}`);
  return res.json();
}

export async function runAction(action: ActionName, requests = 50): Promise<CheckResult> {
  const res = await fetch(`/api/v1/system-checks/actions/${action}?requests=${requests}`, { method: "POST" });
  if (!res.ok) throw new Error(`Action failed: HTTP ${res.status}`);
  return res.json();
}
