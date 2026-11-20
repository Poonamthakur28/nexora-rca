import { type PipelineRun, stageCells } from "./pipelineApi";

const step = (seq: number, stage: string, status = "success") => ({
  seq, stage, name: stage, status, detail: "", duration_ms: 1, finished_at: "",
});

const run = (status: string, steps: ReturnType<typeof step>[]): PipelineRun => ({
  id: "r", title: "t", trigger_kind: "alert_delivery", status, started_at: "", finished_at: null,
  incident_id: null, incident_key: null, steps,
});

describe("stageCells", () => {
  it("shows all stages green for a complete run", () => {
    const stages = ["received", "authenticated", "validated", "stored", "queued", "normalized", "alerts_stored", "incident_linked"];
    const cells = stageCells(run("success", stages.map((s, i) => step(i + 1, s))));
    expect(cells.every((c) => c.status === "success")).toBe(true);
  });

  it("marks later stages pending while the worker has not run yet", () => {
    const cells = stageCells(run("running", ["received", "authenticated", "validated", "stored", "queued"].map((s, i) => step(i + 1, s))));
    expect(cells[4].status).toBe("success");
    expect(cells[5].status).toBe("pending");
  });

  it("marks stages after a failure as not reached", () => {
    const cells = stageCells(run("failed", [step(1, "received"), step(2, "authenticated", "failed")]));
    expect(cells[1].status).toBe("failed");
    expect(cells[2].status).toBe("not_reached");
    expect(cells[7].status).toBe("not_reached");
  });
});

describe("evidence runs", () => {
  it("use the evidence stages", () => {
    const r = { ...run("success", [step(1, "queued"), step(2, "catalog"), step(3, "traces", "warning")]), trigger_kind: "evidence_collection" };
    const cells = stageCells(r);
    expect(cells.map((c) => c.label)).toEqual(["Queued", "Catalog", "Window", "Traces", "Logs", "Metrics", "Dependencies", "Deployments", "Code", "Stored"]);
    expect(cells[3].status).toBe("warning");
  });
});

describe("rca runs", () => {
  it("use the investigation stages", () => {
    const r = { ...run("warning", [step(1, "queued"), step(2, "context"), step(3, "model"), step(4, "validate"), step(5, "grounding", "warning")]), trigger_kind: "rca_investigation" };
    const cells = stageCells(r);
    expect(cells.map((c) => c.label)).toEqual(["Queued", "Context", "Model", "Validate", "Grounding", "Stored"]);
    expect(cells[4].status).toBe("warning");
  });
});

describe("mitigation plan runs", () => {
  it("use the planning stages", () => {
    const r = {
      ...run("success", [step(1, "queued"), step(2, "options"), step(3, "gates"), step(4, "choose"), step(5, "stored")]),
      trigger_kind: "mitigation_plan",
    };
    const cells = stageCells(r);
    expect(cells.map((c) => c.label)).toEqual(["Queued", "Options", "Gates", "Choose", "Stored"]);
    expect(cells.every((c) => c.status === "success")).toBe(true);
  });
});

describe("mitigation execution runs", () => {
  it("use the execution stages", () => {
    const r = {
      ...run("failed", [step(1, "queued"), step(2, "lock"), step(3, "preflight"), step(4, "execute", "failed")]),
      trigger_kind: "mitigation_execution",
    };
    const cells = stageCells(r);
    expect(cells.map((c) => c.label)).toEqual(["Queued", "Lock", "Preflight", "Execute", "Stored"]);
    expect(cells[3].status).toBe("failed");
    expect(cells[4].status).toBe("not_reached");
  });
});

describe("recovery runs", () => {
  it("use the recovery stages and skip undo when healthy", () => {
    const r = {
      ...run("success", [step(1, "queued"), step(2, "waiting"), step(3, "signals"), step(4, "verdict")]),
      trigger_kind: "recovery_verification",
    };
    const cells = stageCells(r);
    expect(cells.map((c) => c.label)).toEqual(["Queued", "Waiting", "Signals", "Verdict", "Undo"]);
    expect(cells[3].status).toBe("success");
    expect(cells[4].status).toBe("skipped");
  });
});
