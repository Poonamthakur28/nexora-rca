import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FixView } from "./FixPanel";
import type { FixAttempt, FixBundle } from "./pipelineApi";

const attempt: FixAttempt = {
  id: "a1", incident_id: "i1", rca_report_id: "r1", status: "verified", reason: "", provider: "anthropic",
  model: "claude-sonnet-5-5", target_file: "app/pricing.py", repro_test_path: "tests/test_nexora_repro.py",
  repro_test_code: "def test_unknown_region():\n    assert calculate_total(100.0, 'TN')['total'] == 118.0\n",
  patch_diff: "--- a/app/pricing.py\n+++ b/app/pricing.py\n@@ -1,3 +1,3 @@\n-    if rule is None and not flag:\n+    if rule is None:\n",
  patch_files: ["app/pricing.py", "tests/test_nexora_repro.py"], explanation: "fall back to the default rule",
  gates: [
    { gate: "repro_fails_before", ok: true, detail: "fails on the current code: 1 failed (exit 1), raising AttributeError" },
    { gate: "repro_passes_after", ok: true, detail: "with the patch: 1 passed (exit 0)" },
  ],
  runs: {
    repro_before: { name: "repro_before", exit_code: 1, passed: 0, failed: 1, errors: 0, timed_out: false, duration_ms: 900, output: "", summary: "1 failed (exit 1)" },
    repro_after: { name: "repro_after", exit_code: 0, passed: 1, failed: 0, errors: 0, timed_out: false, duration_ms: 800, output: "", summary: "1 passed (exit 0)" },
  },
  artifacts_dir: "api/var/fixes/INC-1002-a1", pr_url: null, requested_by: "auto", llm_calls: 2,
  input_tokens: 2000, output_tokens: 400, created_at: "", finished_at: "",
};

const bundle = (latest: FixAttempt | null, pending = false): FixBundle => ({ latest, pending, history: [] });
const html = (props: Parameters<typeof FixView>[0]) => renderToStaticMarkup(<FixView {...props} />);

describe("FixView", () => {
  it("shows a verified fix with its gates, sandbox runs, test and patch", () => {
    const out = html({ data: bundle(attempt) });
    expect(out).toContain(">Verified<");
    expect(out).toContain("Gate 1: reproduction test fails on the current code");
    expect(out).toContain("raising AttributeError");
    expect(out).toContain("Reproduction test, before: <b>1 failed (exit 1)</b>");
    expect(out).toContain("tests/test_nexora_repro.py");
    expect(out).toContain('<div class="add">+    if rule is None:</div>');
    expect(out).toContain('<div class="del">-    if rule is None and not flag:</div>');
    expect(out).toContain("api/var/fixes/INC-1002-a1");
    expect(out).not.toContain("Stopped:");
  });

  it("links a draft PR", () => {
    const out = html({ data: bundle({ ...attempt, status: "pr_opened", pr_url: "https://github.com/a/b/pull/7" }) });
    expect(out).toContain(">Draft PR opened<");
    expect(out).toContain('href="https://github.com/a/b/pull/7"');
  });

  it("shows why an attempt stopped and failed gates", () => {
    const out = html({
      data: bundle({
        ...attempt, status: "repro_failed", patch_diff: "", artifacts_dir: null,
        reason: "no reproduction test failed the way production did: it passed",
        gates: [{ gate: "repro_fails_before", ok: false, detail: "attempt 2: it passed" }],
      }),
    });
    expect(out).toContain(">Not reproduced<");
    expect(out).toContain("<b>Stopped:</b> no reproduction test failed");
    expect(out).toContain('class="bad"');
    expect(out).not.toContain("Patch (");
  });

  it("explains that an LLM is needed and offers a first attempt", () => {
    const out = html({
      data: bundle(null),
      config: { enabled: true, provider: "heuristic", configured: false, sandbox: "subprocess", draft_pr: false },
      onRequest: () => undefined,
    });
    expect(out).toContain("needs an LLM provider");
    expect(out).toContain(">Propose a fix</button>");
    expect(out).toContain("No fix attempted");
  });

  it("disables the button while an attempt is running", () => {
    const out = html({ data: bundle({ ...attempt, status: "running", gates: [], runs: {}, patch_diff: "" }, true), onRequest: () => undefined });
    expect(out).toContain("Working: writing and checking");
    expect(out).toMatch(/<button[^>]*disabled=""[^>]*>Try again<\/button>/);
  });

  it("shows the server's refusal reason", () => {
    expect(html({ data: bundle(null), notice: "a fix attempt is already queued or running" }))
      .toContain('role="status">a fix attempt is already queued or running');
  });
});
