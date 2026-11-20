import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RcaBundle } from "./pipelineApi";
import { RcaView } from "./RcaPanel";

const bundle = {
  in_progress: false,
  history: [],
  latest: {
    id: "1", status: "succeeded", provider: "heuristic", model: "rules-v1", category: "dependency",
    confidence: 0.8, grounded: true, error: null, latency_ms: 12, input_tokens: null, output_tokens: null,
    created_at: "", checks: [{ check: "cited evidence exists", ok: true, detail: "3 cited" }],
    report: {
      summary: "payment-gateway is slow", confidence: 0.8, model_confidence: 0.8, evidence_refs: ["E1"],
      root_cause: { category: "dependency", component: "payment-gateway", file: null, line: null, function: null, explanation: "2.5 s per call" },
      trigger: { kind: "external", release_version: null, explanation: "" }, symptom_vs_cause: "",
      hypotheses: [{ title: "Gateway slow", likelihood: 0.8, status: "accepted", supporting: ["E1"], contradicting: [], reasoning: "" }],
      missing_evidence: [], next_checks: ["Check the gateway"],
      mitigation_options: [{ action: "failover", target: "payment-gateway", reason: "healthy replica", risk: "medium" }],
      evidence_index: { E1: { id: "x", kind: "dependency", title: "Calls to payment-gateway slow" } },
    },
  },
};

const html = (data: unknown) => renderToStaticMarkup(<RcaView data={data as RcaBundle} />);

describe("RcaView", () => {
  it("shows the root cause, confidence, evidence ids and mitigation options", () => {
    const out = html(bundle);
    expect(out).toContain("payment-gateway is slow");
    expect(out).toContain(">Dependency<");
    expect(out).toContain("80% confidence");
    expect(out).toContain('title="dependency: Calls to payment-gateway slow"');
    expect(out).toContain("failover");
  });

  it("shows a failed investigation's reason", () => {
    const out = html({ ...bundle, latest: { ...bundle.latest, status: "failed", error: "ANTHROPIC_API_KEY is not set" } });
    expect(out).toContain("ANTHROPIC_API_KEY is not set");
    expect(out).not.toContain("confidence</span>");
  });

  it("explains when there is no investigation yet", () => {
    expect(html({ latest: null, in_progress: false, history: [] })).toContain("No investigation yet");
  });
});
