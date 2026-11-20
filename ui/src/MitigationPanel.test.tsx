import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { MitigationBundle, MitigationPlan } from "./pipelineApi";
import { MitigationView } from "./MitigationPanel";

const plan = (over: Partial<MitigationPlan> = {}): MitigationPlan => ({
  id: "p1", incident_id: "i1", action_type: "rollback_release", params: {},
  title: "Roll back to the previous healthy release", risk: "low", approval_mode: "single",
  gate_results: [
    { gate: "previous_release_exists", ok: true, detail: "v1.0.0" },
    { gate: "no_execution_running", ok: true, detail: "ok" },
  ],
  score: 0.8, status: "proposed",
  rationale: "The release coincides with the leak. Score 0.80.",
  undo_plan: { action: "roll_forward", description: "Re-enable the planted bug and record a roll-forward." },
  target: "shop-demo · local", decided_by: null, decided_at: null, created_at: "",
  ...over,
});

const bundle = (over: Partial<MitigationBundle> = {}): MitigationBundle => ({
  chosen: plan(),
  blocked: [],
  executions: [],
  recovery: null,
  no_safe_action: null,
  pending: false,
  ...over,
});

const html = (data: MitigationBundle | null) => renderToStaticMarkup(<MitigationView data={data} />);

describe("MitigationView", () => {
  it("shows the chosen plan as an approval card", () => {
    const out = html(bundle());
    expect(out).toContain("Roll back to the previous healthy release");
    expect(out).toContain("shop-demo · local");
    expect(out).toContain("risk-low");
    expect(out).toContain("score 0.80");
    expect(out).toContain("The release coincides with the leak");
    expect(out).toContain("✓ previous release exists");
    expect(out).toContain("Re-enable the planted bug");
    expect(out).toContain(">Approve<");
    expect(out).toContain(">Reject<");
    expect(out).toContain('for="mitigation-comment">Comment<');
    expect(out).toContain("Required to reject this plan");
    expect(out).not.toMatch(/<button[^>]*class="primary"[^>]*disabled/);
    expect(out).toMatch(/<button[^>]*disabled[^>]*>Reject</);
  });

  it("disables Approve unless the plan is proposed", () => {
    const out = html(bundle({ chosen: plan({ status: "approved", decided_by: "alice" }) }));
    expect(out).toMatch(/<button[^>]*class="primary"[^>]*disabled[^>]*>Approve</);
    expect(out).toContain("Approve is available only while the plan is proposed");
    expect(out).toContain("Decided by alice");
  });

  it("lists blocked plans collapsed with their failed gates", () => {
    const out = html(bundle({
      blocked: [plan({
        id: "p2", title: "Restart the service connection pool", status: "blocked", risk: "medium",
        gate_results: [
          { gate: "restart_rate_limit", ok: false, detail: "2 restarts in the last hour" },
          { gate: "no_execution_running", ok: true, detail: "ok" },
        ],
      })],
    }));
    expect(out).toContain("<details");
    expect(out).toContain("1 plan evaluated but blocked");
    expect(out).toContain("Restart the service connection pool");
    expect(out).toContain("✗ restart rate limit");
    expect(out).toContain("2 restarts in the last hour");
    expect(out).not.toContain("✗ no execution running");
  });

  it("shows execution status and recovery signals after approval", () => {
    const recovery = {
      id: "c1", execution_id: "e1", window_seconds: 120, verdict: "healthy", checked_at: "",
      signals: [{ metric: "error_ratio", threshold: 0.05, before: 0.4, after: 0.01, ok: true }],
    };
    const out = html(bundle({
      chosen: plan({ status: "succeeded", decided_by: "alice" }),
      executions: [{
        id: "e1", plan_id: "p1", service: "shop-demo", environment: "local", status: "succeeded",
        before_state: {}, result: {}, error: null, started_at: "", finished_at: "", recovery,
      }],
      recovery,
    }));
    expect(out).toContain("Execution");
    expect(out).toContain("Succeeded");
    expect(out).toContain("Recovery");
    expect(out).toContain("Healthy");
    expect(out).toContain("error ratio 0.40 → 0.01");
    expect(out).toContain("threshold 0.05");
  });

  it("shows the no safe action reason when there is no plan", () => {
    const out = html(bundle({
      chosen: null,
      no_safe_action: "no safe action; escalate to on-call (failover: not in the catalog)",
    }));
    expect(out).toContain("No safe action.");
    expect(out).toContain("escalate to on-call");
    expect(out).not.toContain(">Approve<");
  });
});
