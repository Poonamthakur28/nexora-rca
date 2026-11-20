# Nexora RCA Engine — Team 4

Standalone build of the Incident RCA Engine, delivered step by step. Each step adds
engine capability **and** a check on the **Checks** page, so every action is visibly
green (success), amber (warning) or red (failed).

Monitoring is fully free and self-hosted: **Prometheus + Alertmanager** for alerts and
**OpenTelemetry → Tempo / Loki / Prometheus** for traces, logs and metrics. No Sentry.

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Test bench (monitoring stack, `shop-demo` with planted bug) + System Checks page | Done |
| 2 | Alertmanager webhook → alerts → incidents, PostgreSQL, worker, Pipeline + Incidents tabs | Done |
| 3 | Service catalog + evidence collection (traces, logs, metrics, deployments, code) | Done |
| 3.5 | Generalised evidence: catalog metrics, latency/saturation, dependencies, Java/Node/Go stacks, 4-scenario bench | Done |
| 4 | AI investigation: pluggable LLM (Anthropic / OpenAI / Ollama) + rule-based baseline, grounding checks | Done |
| 5 | Triage, mitigation, approval, rollback, recovery check | Done |
| 6 | Fix track: reproduction test, sandbox, PR | Planned |
| 7 | Remaining scenarios + Team 5 adapters | Planned |

Since Step 5 the engine also runs natively on Windows next to another monitoring stack (see
[Run natively](#run-natively-next-to-another-monitoring-stack)), maps Windows stack frames, keeps
collecting when single Tempo trace downloads fail, and refuses unsafe rollback targets (Step 5 gates).

## Run it (Windows, macOS or Linux)

Prerequisite: Docker Desktop running.

First provision the Alertmanager webhook token once. It is a local secret: both files below are
git-ignored, and the two values must be identical (templates: `deploy/.env.example`,
`deploy/config/alertmanager_webhook_token.example`). Compose refuses to start without it.

```powershell
cd deploy
$bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$token = [Convert]::ToBase64String($bytes)
[IO.File]::WriteAllText("$PWD\config\alertmanager_webhook_token", $token)   # one line, no newline
Copy-Item .env.example .env
(Get-Content .env) -replace '^ALERTMANAGER_WEBHOOK_TOKEN=.*', "ALERTMANAGER_WEBHOOK_TOKEN=$token" | Set-Content .env
```

For a native (non-Docker) API, put the same `ALERTMANAGER_WEBHOOK_TOKEN=...` line in `api/.env`.
To rotate it, change both files (and `api/.env`), then restart Alertmanager and the API: Alertmanager
reads the file on every delivery, but the API stores the token's hash only when it starts.

```powershell
docker compose up -d --build
```

This starts the whole stack. It needs these host ports free: 4317, 4318, 13133, 3100, 3200, 9090,
9093, 8080, 8000 and 5433. All except 8000 are published on `127.0.0.1` only (development use);
8000 stays on all interfaces because Alertmanager delivers webhooks to `host.docker.internal:8000`.
If another monitoring stack already uses some of these ports, do not run a plain `docker compose up`;
see [Run natively](#run-natively-next-to-another-monitoring-stack).

Open **http://localhost:8000**. Tempo and Loki show "not ready yet" for about 30 seconds
after starting; that is normal. The UI has three tabs:

- **Checks** — every component and signal, green / amber / red.
- **Pipeline** — every alert delivery, stage by stage (received → authenticated → validated →
  stored → queued → normalized → alerts stored → incident).
- **Incidents** — incidents opened from real alerts, with their alerts, RCA, **mitigation** approval, evidence and timeline.

Create a real incident from the **Actions** panel:

1. **Enable bug** — turns on `code_null_tax` in shop-demo.
2. **Start steady load** — 3 requests/s for 3 minutes.
3. Wait about 2 minutes. All four signals turn green, including
   *Alert fires in Alertmanager* (`ShopDemoHighErrorRate`).
4. Open **Pipeline**: the `ShopDemoHighErrorRate (firing)` delivery shows all 8 stages green
   and links to **INC-1001** on the **Incidents** tab.
5. **Disable bug** — errors stop; within about 2 minutes a `(resolved)` delivery appears and the
   incident shows 0 active alerts. It stays *open*: Nexora only closes incidents after recovery
   is verified (Step 5).

Stop everything with `docker compose down` (add `-v` to also delete stored traces and logs).

## What runs

| Container | Purpose | Host port |
|---|---|---|
| `nexora` | Nexora API + Checks UI | 8000 (all interfaces) |
| `nexora-worker` | Runs every job: alerts, evidence, RCA, mitigation, recovery | — |
| `postgres` | Nexora's database | 127.0.0.1:5433 |
| `shop-demo` | Sample checkout service with planted bugs | 127.0.0.1:8080 |
| `payment-gateway` | Stand-in external payment provider | — |
| `otel-collector` | Receives OTLP from shop-demo | 127.0.0.1:4317, 4318, 13133 |
| `tempo` | Traces | 127.0.0.1:3200 |
| `loki` | Logs | 127.0.0.1:3100 |
| `prometheus` | Metrics + alert rules | 127.0.0.1:9090 (19090 with `docker-compose.local.yml`) |
| `alertmanager` | Routes alerts to Nexora's webhook | 127.0.0.1:9093 |

Both `nexora` containers run `api/entrypoint.sh`: it applies the Alembic migrations (retrying while
PostgreSQL starts) and stops with an error if they never succeed (`MIGRATION_ATTEMPTS`, default 30).

## How an alert becomes an incident (Step 2)

1. Alertmanager POSTs to `/api/v1/integrations/alerts/alertmanager` with a Bearer token
   (`deploy/config/alertmanager_webhook_token`, same value as `ALERTMANAGER_WEBHOOK_TOKEN`).
2. The gateway finds the integration by the token's SHA-256 (the organization comes from the
   integration, never from the payload), validates the body, and checks whether the identical
   body was already received (duplicate → `200`, nothing created).
3. It stores the delivery and queues an `alert.process` job **in one transaction**, then returns
   `202`. Accepted means stored and queued, not that an incident exists.
4. The worker claims the job (`FOR UPDATE SKIP LOCKED`), normalizes each alert to a
   `CanonicalAlert`, upserts it by `(organization, integration, Alertmanager fingerprint)`, then
   links it to an open incident for the same service and environment or opens a new one.
5. Resolved alerts update the incident's active-alert count and timeline; the incident stays open
   until recovery is verified.

Every stage writes a step to `pipeline_steps`, which is what the Pipeline tab shows.

## How Nexora collects evidence (Step 3)

When an incident opens, the worker runs an `evidence.collect` job (also available as
**Collect again** on the incident page). It looks the service up in the **service catalog**
(`deploy/config/service-catalog.yaml`) and runs six collectors for the incident's time window:

| Collector | Source | What it records |
|---|---|---|
| Traces | Tempo | Exception type, message and full stack trace from error spans |
| Logs | Loki | ERROR log lines grouped by exception, with stack traces |
| Metrics | Prometheus | Catalog-defined signals (5xx ratio, p95 latency, saturation): baseline, peak, onset, breach |
| Dependencies | Tempo | Outgoing calls per downstream service: calls, avg/max time, share of the request, errors |
| Deployments | Catalog | The release running at detection, how long before, which files it changed |
| Code | Source tree | The deepest application stack frame, with the surrounding lines of code |

The stack frame `/srv/app/pricing.py:32` (path inside the container) is mapped to
`app/pricing.py` in the source tree via `runtime_path_prefix`, and flagged when that file was
changed by the latest release. A source that fails or has nothing becomes a **gap**, never a crash.
All text is redacted (tokens, passwords, keys, emails, card numbers) before it is stored.

Where the code lives differs between Docker (`source_root: /repos/shop-demo`, frames under `/srv/`)
and a native run. A machine-local file named by `SERVICE_CATALOG_OVERRIDES_FILE` can override **only**
`source_root` and `runtime_path_prefix` per service (Windows paths work with `\` or `/`, any drive-letter
case); owners, metrics and actions always come from the shared catalog. Docker never sets it.
If one Tempo trace download times out or fails, the collector skips it and reads the next search
result (at most 2 extra), and reports how many it could not read.

On the test bench, **Enable bug** also records a simulated release (`v1.1.0`, changed
`app/pricing.py`) so the deploy-to-error link is visible. Real services record deploys with
`POST /api/v1/services/{name}/deployments` from CI.

## Beyond one bug (Step 3.5)

Nothing in evidence collection is specific to one error:

- **Metrics come from the catalog.** Each service lists its signals (`error_ratio`,
  `latency_p95`, `saturation`…) as PromQL with a threshold; services that list none get the
  OpenTelemetry defaults. A normal metric is kept too — "latency was fine" rules causes out.
- **Dependencies.** Client spans in Tempo are grouped by downstream service, so a slow or
  failing dependency shows up even when our own code throws nothing.
- **A clean evidence window.** Evidence covers 15 minutes before detection, but never reaches
  back past the moment an earlier incident on the same service cleared, so a previous failure's
  errors cannot leak into this one.
- **Flap or recurrence.** An alert that fires again within `ALERT_REOPEN_WINDOW_MINUTES`
  (15 by default, 5 on the test bench) of clearing rejoins its incident; later, it opens a new
  incident with fresh evidence.
- **Stack traces in four languages.** Python, Java (root `Caused by:`, app packages from the
  catalog), Node.js (skips `node_modules`) and Go (the panicking goroutine). Frames are mapped
  to the repo by path suffix and never outside the source root.

## Planted failures and the evaluation bench

Pick a failure on the **Checks** page (or `?bug=` on the action API):

| Failure | Kind | What breaks | Release recorded |
|---|---|---|---|
| `code_null_tax` | code | `find_tax_rule` returns `None` → `AttributeError` in `app/pricing.py` | yes, `app/pricing.py` |
| `code_inventory_keyerror` | code | unknown SKU → `KeyError` in `app/inventory.py` | yes, `app/inventory.py` |
| `slow_payment_gateway` | dependency | `payment-gateway` answers in ~2.5 s, no errors | no (external) |
| `db_pool_leak` | resource | `save_order` never returns its connection → pool exhausted, `PoolTimeoutError` | yes, `app/orders.py` |

`bench/run_bench.py` runs all four live against the local stack (enable → load → incident →
evidence → RCA → approve a proposed plan → recovery verdict → finally disable so a failed run
never leaves a bug on) and writes `bench/results/<time>.md`. When a scenario expects mitigation,
the runner does **not** disable the bug to clear the alert; the catalog action has to be what
fixes it. Expectations live in `bench/scenarios.yaml`.

```bash
docker compose -f deploy/docker-compose.yml up -d
pip install pyyaml && python bench/run_bench.py          # about an hour for all four
python bench/run_bench.py --only slow_gateway            # just one
```

Note for `db_pool_leak`: the stack trace points at the pool's `acquire` (where it times out),
not at `save_order` (where the connection is leaked). Evidence shows both the saturation metric
and the release that changed `orders.py`; connecting them is the job of the Step 4 AI.

## How Nexora investigates (Step 4)

When evidence is stored, the worker runs an `rca.investigate` job. The **Incidents** tab shows the
result above the evidence; the **Pipeline** tab shows each stage.

| Stage | What happens |
|---|---|
| Context | Latest evidence, numbered `E1…En`, plus the source of files changed by a release in the window (redacted) |
| Model | The configured provider writes a report in a fixed JSON shape |
| Validate | Strict schema check; one retry with the error if the output does not fit |
| Grounding | Cited ids must exist; the blamed file, line, release and dependency must appear in the evidence. Each failure caps confidence (not hidden) |
| Stored | Report saved; one line on the incident timeline |

The report has: summary, root cause (category, component, file:line, function), trigger (release /
traffic / external), symptom vs cause, confidence, hypotheses with supporting and contradicting
evidence, missing evidence, next checks, and mitigation **options** (suggestions only; the policy engine decides what is safe to run).

### Choosing the investigator

Set these before `docker compose up` (shell env or `deploy/.env`). Keys are never returned by the API.

| `RCA_PROVIDER` | Needs | Default model (`RCA_MODEL` overrides) |
|---|---|---|
| `heuristic` (default) | nothing | `rules-v1`: explicit rules, offline, free |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| `openai` | `OPENAI_API_KEY` (`OPENAI_BASE_URL` for compatible gateways) | `gpt-4.1-mini` |
| `ollama` | Ollama running on the host (`OLLAMA_URL`) | `llama3.1:8b` |

```bash
RCA_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... docker compose -f deploy/docker-compose.yml up -d
```

The rule-based baseline is the bar an LLM has to beat. It names the right category and file in all
four bench scenarios, but it cannot read code: for the pool leak it stops at "the release changed
`orders.py`". The bench checks that an LLM goes further and finds the leak in `save_order()`.

## How Nexora mitigates (Step 5)

The LLM only proposes. Plain code decides which catalog action is safe, a human approves it, an
adapter runs one fixed `/admin` call, and recovery either keeps the change or undoes it.

**Catalog.** `shop-demo` exposes three actions: `rollback_release`, `disable_feature`, and
`restart_service`. Each declares risk, approval mode, parameters and precondition gates. Adapters
never take LLM-generated URLs, SQL or shell; they return typed errors on timeout. Rollback records a
roll-forward, feature disable restores the previous flag value, restart is not reversible.

**Plan.** After a successful RCA, `mitigation.plan` maps options onto the catalog (`rollback` →
`rollback_release`, `restart` → `restart_service`). Unknown options are dropped (`failover: not in
the catalog`). Gates run in code: previous release exists and was healthy, no irreversible
migration, no execution already running, the feature flag exists, restart rate limit (max 2/hour).
"Healthy" is strict: the rollback target must not be the release the RCA blames, the blamed release
must be the current one (else rolling back does not remove it), an RCA that blames an unnamed or
unrecorded release blocks the rollback, the target must have been live at least 5 minutes, and no
incident (including the one being mitigated) may have been detected while it was live.
Score is RCA confidence × risk weight (low 1.0, medium 0.6, high 0.3). Rolling back our code scores
0 when the cause is a dependency. The highest-scoring plan whose gates pass is `proposed`; blocked
plans are stored so the UI can show why. If nothing passes: "no safe action; escalate to on-call".

**Approve and lock.** `POST /api/v1/mitigation-plans/{id}/approve` (actor from `X-Nexora-Actor`)
re-checks gates (409 if they now fail). `mitigation.execute` runs once (`max_attempts` 1) under an
exclusive lock: at most one `running` execution per service and environment. Success records
`Executed <action> (approved by <actor>)` and queues recovery. Failure is stored and never retried.

**Recovery and undo.** `recovery.verify` does not sleep in one long job: it re-queues itself with
`run_after` until `RECOVERY_WINDOW_SECONDS` (120) have passed, then reads catalog metrics from
Prometheus. Healthy = every metric that was breached is under its threshold **and** alerts have
cleared (`active_alerts == 0`) → incident `mitigated`, event `Recovered after <action>`. Unhealthy →
adapter undo, execution
`undone`, plan failed, incident back to `investigating`, RCA queued again. Inconclusive (metrics
unreadable) → no undo; escalate.

**UI.** The incident **Mitigation** panel (between RCA and evidence) is the approval card: title,
target, risk, score, rationale, gate checklist, undo plan, Approve / Reject (Approve only while
`proposed`; Reject needs a comment). Blocked plans are collapsed. After approval it shows execution
status and each recovery signal before → after.

The live bench (`bench/scenarios.yaml`) expects `rollback_release` to recover `null_tax` and
`inventory_keyerror`, `rollback_release` or `restart_service` for `db_pool_leak`, and no plan
(escalated) for `slow_gateway`. The runner approves a proposed plan; it does not disable the bug to
fake recovery.

## Run natively next to another monitoring stack

Use this when ports 4317/4318/13133/3100/3200/9090/8080 already belong to something else (for
example a shared OpenTelemetry stack). Nexora's API, worker, shop-demo and payment-gateway run as
plain Python processes; only Nexora's PostgreSQL, Alertmanager and a rule-evaluating Prometheus
run from this compose file. Adjust the ports below to your machine.

1. Provision the webhook token (see [Run it](#run-it-windows-macos-or-linux)), then:

   ```powershell
   cd deploy
   docker compose up -d postgres alertmanager
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --no-deps prometheus
   ```

   `docker-compose.local.yml` changes only `prometheus`: it publishes it on `127.0.0.1:19090` and
   uses `config/prometheus.local.yml`, which federates shop-demo's series from the Prometheus on
   `host.docker.internal:9090`, scrapes shop-demo at `host.docker.internal:18080`, and loads the
   same alert rules as the Docker stack.

2. Create `api/.env` (git-ignored; the API and worker read it from the `api/` folder):

   ```ini
   ALERTMANAGER_WEBHOOK_TOKEN=<same value as deploy/.env>
   DATABASE_URL=postgresql+psycopg://nexora:nexora@127.0.0.1:5433/nexora
   ALERTMANAGER_URL=http://127.0.0.1:9093
   SHOP_DEMO_URL=http://127.0.0.1:18080
   TEMPO_URL=http://127.0.0.1:3200
   LOKI_URL=http://127.0.0.1:3100
   PROMETHEUS_URL=http://127.0.0.1:9090
   SERVICE_CATALOG_OVERRIDES_FILE=<absolute path>/deploy/config/service-catalog.local.yaml
   ```

   Use `127.0.0.1`, not `localhost`: on Windows a port bound to IPv4 only makes every new
   `localhost` connection wait ~2 s for `::1` to be refused.

3. Create `deploy/config/service-catalog.local.yaml` (git-ignored) so stack frames map to the
   source tree:

   ```yaml
   services:
     - name: shop-demo
       environment: local
       source_root: <absolute path>/shop-demo
       runtime_path_prefix: <absolute path>/shop-demo/
   ```

4. Start each process in its own terminal:

   ```powershell
   # payment-gateway and shop-demo (OTLP to the existing collector, e.g. 127.0.0.1:14317)
   cd shop-demo
   $env:OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:14317"; $env:OTEL_SERVICE_NAME="payment-gateway"
   python -m uvicorn app.gateway:app --host 0.0.0.0 --port 8090
   $env:OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:14317"; $env:OTEL_SERVICE_NAME="shop-demo"
   $env:SHOP_DEMO_SELF_URL="http://127.0.0.1:18080"; $env:PAYMENT_GATEWAY_URL="http://127.0.0.1:8090"
   python -m uvicorn app.main:app --host 0.0.0.0 --port 18080

   # Nexora: migrate once, then API and worker (both from api/ so api/.env is found)
   cd api
   alembic upgrade head
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   python -m app.worker
   ```

   The API listens on all interfaces because Alertmanager (in Docker) reaches it at
   `host.docker.internal:8000`; restrict it with the host firewall if the machine is on a shared
   network. The worker is required: without it alerts are accepted but no incident is created.

## Develop without Docker for the API/UI

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r api/requirements-dev.txt
cd api && pytest                                   # 137 tests (SQLite, no Docker needed)
alembic upgrade head                               # against DATABASE_URL (PostgreSQL)
uvicorn app.main:app --port 8000                   # API; reads api/.env when started from api/
python -m app.worker                               # worker (separate terminal), required for incidents

cd ../ui && npm install && npm test && npm run dev # 20 UI tests; UI on http://localhost:5173
```

## Layout

```text
api/        FastAPI app + worker (system_checks, alert_ingestion, incidents, pipeline, catalog, evidence, rca, mitigation), Alembic migrations
ui/         React + Vite UI: Checks, Pipeline, Incidents (+ RCA, mitigation approval, evidence)
shop-demo/  Sample service (+ payment-gateway) with planted failures and OpenTelemetry
bench/      Evaluation bench: scenarios.yaml, run_bench.py, results/
deploy/     docker-compose.yml and configs (OTel, Tempo, Loki, Prometheus, Alertmanager)
docs/       Screenshots of each step's acceptance
```
