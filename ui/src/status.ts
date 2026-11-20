import type { CheckStatus } from "./api";

export interface StatusStyle {
  label: string;
  color: string;
  background: string;
}

const STYLES: Record<CheckStatus, StatusStyle> = {
  success: { label: "Success", color: "#14532d", background: "#dcfce7" },
  failed: { label: "Failed", color: "#7f1d1d", background: "#fee2e2" },
  warning: { label: "Warning", color: "#78350f", background: "#fef3c7" },
  skipped: { label: "Skipped", color: "#374151", background: "#e5e7eb" },
};

const EXTRA: Record<string, StatusStyle> = {
  running: { label: "Running", color: "#1e3a8a", background: "#dbeafe" },
  pending: { label: "Pending", color: "#1e3a8a", background: "#dbeafe" },
  not_reached: { label: "Not reached", color: "#6b7280", background: "#f3f4f6" },
  firing: { label: "Firing", color: "#7f1d1d", background: "#fee2e2" },
  resolved: { label: "Resolved", color: "#14532d", background: "#dcfce7" },
  moved: { label: "Moved to new incident", color: "#374151", background: "#e5e7eb" },
  open: { label: "Open", color: "#7f1d1d", background: "#fee2e2" },
  proposed: { label: "Proposed", color: "#1e3a8a", background: "#dbeafe" },
  approved: { label: "Approved", color: "#14532d", background: "#dcfce7" },
  executing: { label: "Executing", color: "#1e3a8a", background: "#dbeafe" },
  succeeded: { label: "Succeeded", color: "#14532d", background: "#dcfce7" },
  blocked: { label: "Blocked", color: "#78350f", background: "#fef3c7" },
  rejected: { label: "Rejected", color: "#374151", background: "#e5e7eb" },
  undone: { label: "Undone", color: "#78350f", background: "#fef3c7" },
  healthy: { label: "Healthy", color: "#14532d", background: "#dcfce7" },
  unhealthy: { label: "Unhealthy", color: "#7f1d1d", background: "#fee2e2" },
  inconclusive: { label: "Inconclusive", color: "#78350f", background: "#fef3c7" },
  mitigated: { label: "Mitigated", color: "#14532d", background: "#dcfce7" },
};

export function statusStyle(status: string): StatusStyle {
  return STYLES[status as CheckStatus] ?? EXTRA[status] ?? STYLES.skipped;
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

export function countByStatus(statuses: string[]): Record<CheckStatus, number> {
  const counts: Record<CheckStatus, number> = { success: 0, failed: 0, warning: 0, skipped: 0 };
  for (const s of statuses) {
    const key = (s in counts ? s : "skipped") as CheckStatus;
    counts[key] += 1;
  }
  return counts;
}
