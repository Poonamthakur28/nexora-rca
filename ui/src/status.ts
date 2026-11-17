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

export function statusStyle(status: string): StatusStyle {
  return STYLES[status as CheckStatus] ?? STYLES.skipped;
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
