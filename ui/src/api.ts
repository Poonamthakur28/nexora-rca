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

export interface BugInfo {
  enabled: boolean;
  kind: string;
  description: string;
  release_files: string[];
}

export async function fetchBugs(): Promise<Record<string, BugInfo>> {
  const res = await fetch("/api/v1/system-checks/bugs");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function runAction(action: ActionName, requests = 50, bug?: string): Promise<CheckResult> {
  const q = new URLSearchParams({ requests: String(requests) });
  if (bug) q.set("bug", bug);
  const res = await fetch(`/api/v1/system-checks/actions/${action}?${q}`, { method: "POST" });
  if (!res.ok) throw new Error(`Action failed: HTTP ${res.status}`);
  return res.json();
}
