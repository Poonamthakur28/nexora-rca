# Step 5: Mitigation and recovery, as Cursor prompts

Use Cursor **Agent** mode. Run one prompt at a time, in order. After each, check that
`cd api && pytest` and `cd ui && npm test` pass, and look over the diff before going on.
The project rules in `.cursor/rules/nexora.mdc` load automatically.

Goal of Step 5: after the RCA report, Nexora proposes **one safe action** from a fixed catalog,
checks it with deterministic gates, asks a human to approve it, runs it, watches recovery, and undoes it
automatically if the service does not recover.

---

## 5.1 Catalog actions and the action adapters

```
Read README.md, deploy/config/service-catalog.yaml, api/app/modules/catalog/service.py and
shop-demo/app/main.py first.

Add an `actions` list to the shop-demo entry in service-catalog.yaml and seed it into
services.config["actions"] (catalog/service.py seed_from_file). Each action has:
  type, title, risk (low|medium|high), approval (auto|single), params, preconditions (list of gate names).
Actions for shop-demo:
  - rollback_release: roll back to the previous healthy release. Risk low, approval single.
    Preconditions: previous_release_exists, previous_release_healthy, no_irreversible_migration,
    no_execution_running.
  - disable_feature: turn off one planted failure flag (params.flag). Risk low, approval single.
    Preconditions: flag_exists, no_execution_running.
  - restart_service: reset shop-demo's connection pool. Risk medium, approval single.
    Preconditions: no_execution_running, restart_rate_limit (max 2 per hour).

Create a new module api/app/modules/mitigation/ with adapters.py:
  class ActionAdapter (Protocol-like base) with action_type, preflight(plan) -> PreflightResult,
  execute(plan) -> ExecutionResult, undo(execution) -> ExecutionResult.
  Typed results (Pydantic). Every HTTP call uses httpx with a timeout and returns a typed error.
Adapters (they talk to shop-demo's /admin endpoints, which is how the test bench simulates deploys):
  - RollbackReleaseAdapter: disables the planted bug that the current bench release introduced
    (find it from the latest service_deployments row: source="test_bench", description mentions
    "planted bug <name>"), then records a deployment "rollback to <previous version>" via
    catalog.service.record_deployment. undo = re-enable the bug and record a roll-forward.
  - DisableFeatureAdapter: POST /admin/bugs/{flag}/disable; undo = enable.
  - RestartServiceAdapter: add POST /admin/restart to shop-demo (resets the pool, like toggle_bug
    does for db_pool_leak); undo = no-op (report "not reversible").
Capture before_state in each execute (e.g. which bugs were enabled) so undo is exact.

Tests in api/tests/test_mitigation_adapters.py with httpx.MockTransport: each adapter's execute,
undo, and a timeout returning a typed error. Do not wire anything into jobs yet.
```

## 5.2 Policy engine: choose one safe plan

```
In api/app/modules/mitigation/ add policy.py and a new table mitigation_plans
(migration 0006): id, organization_id, incident_id, rca_report_id, action_type, params (json),
title, risk, approval_mode, gate_results (json list of {gate, ok, detail}), score (float),
status (proposed|blocked|approved|rejected|executing|succeeded|failed|undone|expired),
rationale, undo_plan (json), decided_by, decided_at, created_at.

policy.plan_for(session, settings, incident) -> MitigationPlan | None:
  1. Read the latest succeeded RcaReport for the incident and its mitigation_options.
  2. Map each option to a catalog action of this service (option.action rollback -> rollback_release,
     disable_feature -> disable_feature, restart -> restart_service). Options with no catalog
     action are dropped and listed in the rationale ("failover: not in the catalog").
  3. Run every precondition gate as plain code (gates.py, one function per gate name, each returns
     {gate, ok, detail}). no_execution_running: no plan in status executing for the same service +
     environment. previous_release_healthy: there was no incident on the previous version.
  4. Score = rca confidence * risk weight (low 1.0, medium 0.6, high 0.3); if the RCA category is
     dependency, rollback/disable of our code scores 0 (rolling back us does not fix them).
  5. Pick the highest-scoring plan whose gates all pass; save every evaluated plan (blocked ones
     too, with their failed gates) so the UI can show why they were not chosen.
  6. If nothing passes: incident event "no safe action; escalate to on-call", no plan approved.

Job: mitigation.plan, enqueued at the end of rca.engine.investigate when it succeeds
(only if settings.mitigation_enabled, default true). Record pipeline stages:
queued, options, gates, choose, stored. Add the stages to ui/src/pipelineApi.ts.

Tests: null-tax incident → rollback_release chosen; slow-gateway incident → no plan (dependency);
a running execution blocks a new plan; gates are reported for blocked plans.
```

## 5.3 Approval, execution lock and execution

```
Add table mitigation_executions (migration 0007): id, organization_id, plan_id, service, environment,
status (running|succeeded|failed|undone), before_state (json), result (json), error, started_at,
finished_at. Add a partial unique index so only one execution per (organization_id, service,
environment) can be status='running' (PostgreSQL WHERE clause; on SQLite enforce it in code too).

API (module routes, registered in main.py):
  GET  /api/v1/incidents/{id}/mitigation-plans
  POST /api/v1/mitigation-plans/{id}/approve   body {comment}
  POST /api/v1/mitigation-plans/{id}/reject    body {comment}
  GET  /api/v1/mitigation-plans/{id}/executions
Approve: only a proposed plan; re-run all gates at approval time (state may have changed);
if any fails → 409 with the failed gates and the plan goes to blocked. Otherwise status approved and
enqueue mitigation.execute. decided_by comes from a header X-Nexora-Actor (default "local-user").

Job mitigation.execute (max_attempts 1, never retried automatically): acquire the lock (insert
execution row), preflight, execute through the adapter, store result, incident event
"Executed <action> (approved by <actor>)". On failure: status failed, incident event, no retry.
Then enqueue recovery.verify. Stages: lock, preflight, execute, stored.

Tests: approve → executes once; double approve → second is 409; a second plan for the same service
while one is running is blocked by the lock; execute failure does not retry.
```

## 5.4 Recovery verification and automatic undo

```
Add table recovery_checks (migration 0008): id, organization_id, execution_id, window_seconds,
signals (json: per metric {metric, threshold, before, after, ok}), verdict (healthy|unhealthy|
inconclusive), checked_at.

Job recovery.verify, scheduled after an execution succeeds. Use settings.recovery_window_seconds
(default 120) and settings.recovery_poll_seconds (15). The job must not sleep inside one long job:
enqueue itself with a run_at delay (add a run_after column to jobs if the queue has none, migration
in the same file) until the window ends.
At the end, query the service's catalog metrics (reuse evidence collectors' metric_specs and
Prometheus query helper) over the window. Healthy = every metric that was breached in the incident's
evidence is now under its threshold AND the incident's alerts have cleared (active_alerts == 0).
  healthy → plan succeeded, incident status "mitigated", event "Recovered after <action>".
  unhealthy → call the adapter's undo, execution status undone, plan failed, incident back to
             "investigating", event "Not recovered; <action> undone", and enqueue rca.investigate
             again so the next plan can be different.
  inconclusive (metrics unreadable) → do not undo; escalate with an event.
Stages: waiting, signals, verdict, undo (only when undone).

Tests with mocked Prometheus: healthy path, unhealthy path triggers undo, inconclusive does not undo.
```

## 5.5 UI: approval card and results

```
In ui/src add MitigationPanel.tsx shown on the incident detail between the RCA panel and the
evidence (IncidentsPage.tsx). It shows:
  - The chosen plan as an approval card: action title, target, risk pill, score, why (rationale),
    the gate checklist (✓/✗ with detail), the undo plan, and Approve / Reject buttons
    (Approve disabled unless status is proposed). Reject asks for a comment.
  - Plans that were evaluated but blocked, collapsed, each with its failed gates.
  - After approval: execution status, then the recovery verdict with each signal before → after.
  - "No safe action" state with the reason, when there is no plan.
Poll every 5 s while a plan is approved/executing or recovery is pending.
Split it like RcaPanel.tsx: a pure MitigationView({data}) for tests + a wrapper that fetches.
Add tests in MitigationPanel.test.tsx using renderToStaticMarkup.
Add the new pipeline stages (mitigation_plan, mitigation_execution, recovery_verification run kinds)
to pipelineApi.ts stagesFor, with tests.
```

## 5.6 Bench and docs

```
Extend bench/scenarios.yaml with a `mitigation` expectation per scenario:
  null_tax and inventory_keyerror: action rollback_release, recovered true
  db_pool_leak: action rollback_release (or restart_service), recovered true
  slow_gateway: no plan (dependency), escalated true
Extend bench/run_bench.py: after the RCA checks, if a plan is proposed, approve it through the API
(that is the human step), wait for the recovery verdict, and check it. Do NOT disable the bug in the
runner when a mitigation is expected; the mitigation must be what fixes it. Keep the existing
finally-block cleanup so a failed run never leaves a bug on.
Update README.md: a "How Nexora mitigates (Step 5)" section with the flow, the gates, the action
catalog, the lock, and the recovery/undo rule; update the Status table and test counts.
```
