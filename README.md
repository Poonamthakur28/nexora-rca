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
| 2 | Alertmanager webhook → alerts → incidents, Pipeline view | Next |
| 3 | Service catalog + evidence collectors | Planned |
| 4 | AI investigation | Planned |
| 5 | Triage, mitigation, approval, rollback, recovery check | Planned |
| 6 | Fix track: reproduction test, sandbox, PR | Planned |
| 7 | Remaining scenarios + Team 5 adapters | Planned |

## Run it (Windows, macOS or Linux)

Prerequisite: Docker Desktop running.

```powershell
cd deploy
docker compose up -d --build
```

Open **http://localhost:8000**. Tempo and Loki show "not ready yet" for about 30 seconds
after starting; that is normal.

Create a real incident from the **Actions** panel:

1. **Enable bug** — turns on `code_null_tax` in shop-demo.
2. **Start steady load** — 3 requests/s for 3 minutes.
3. Wait about 2 minutes. All four signals turn green, including
   *Alert fires in Alertmanager* (`ShopDemoHighErrorRate`).
4. **Disable bug** — errors stop and the alert resolves within about 2 minutes.

Stop everything with `docker compose down` (add `-v` to also delete stored traces and logs).

## What runs

| Container | Purpose | Port |
|---|---|---|
| `nexora` | Nexora API + Checks UI | 8000 |
| `shop-demo` | Sample checkout service with planted bugs | 8080 |
| `otel-collector` | Receives OTLP from shop-demo | 4317, 4318, 13133 |
| `tempo` | Traces | 3200 |
| `loki` | Logs | 3100 |
| `prometheus` | Metrics + alert rules | 9090 |
| `alertmanager` | Routes alerts to Nexora's webhook | 9093 |

Alertmanager already points at `http://host.docker.internal:8000/api/v1/integrations/alerts/alertmanager`
with a Bearer token (`deploy/config/alertmanager_webhook_token`). That endpoint arrives in
Step 2; until then Alertmanager logs failed deliveries, which is expected.

## The planted bug

`shop-demo/app/pricing.py` — with `code_null_tax` enabled, regions without an explicit tax
rule (TN, GJ) get `None` from `find_tax_rule`, and `calculate_total` raises
`AttributeError: 'NoneType' object has no attribute 'rate'` → HTTP 500. The exception, with
its full stack trace, is recorded on the trace span (Tempo) and in the error log (Loki).
This is the evidence the RCA engine will use in later steps to find the faulty line.

## Develop without Docker for the API/UI

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r api/requirements-dev.txt
cd api && pytest                                   # 11 tests
uvicorn app.main:app --port 8000                   # uses localhost ports above

cd ../ui && npm install && npm test && npm run dev # UI on http://localhost:5173
```

## Layout

```text
api/        FastAPI app (modules/system_checks = Step 1)
ui/         React + Vite Checks page
shop-demo/  Sample service with planted bugs and OpenTelemetry
deploy/     docker-compose.yml and configs (OTel, Tempo, Loki, Prometheus, Alertmanager)
docs/       Screenshots of each step's acceptance
```
