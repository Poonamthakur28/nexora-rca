"""Runs component, signal and action checks against the local test bench.

Every check returns a CheckResult; no check ever raises. Checks run concurrently, so one
slow or failing component never blocks or breaks the others.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import Settings

from .contracts import CheckResult, CheckStatus, SystemChecksReport, overall_status

ACTIONS = {"enable_bug", "disable_bug", "generate_traffic", "start_load", "stop_load"}
PLANTED_BUG = "code_null_tax"


class SystemChecksService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self._s = settings
        self._client = client

    # ---------- helpers ----------

    def _safe(self, text: str) -> str:
        """Never let a secret reach the API response."""
        token = self._s.alertmanager_webhook_token
        if token:
            text = text.replace(token, "***")
        return text[:300]

    async def _timed(
        self, check_id: str, name: str, category: str, fn: Callable[[], Awaitable[tuple[CheckStatus, str]]]
    ) -> CheckResult:
        start = time.perf_counter()
        try:
            status, detail = await asyncio.wait_for(fn(), timeout=self._s.check_timeout_seconds + 1)
        except httpx.TimeoutException:
            status, detail = "failed", "timed out"
        except asyncio.TimeoutError:
            status, detail = "failed", "timed out"
        except httpx.ConnectError:
            status, detail = "failed", "connection refused or host unreachable"
        except httpx.HTTPStatusError as exc:
            status, detail = "failed", f"HTTP {exc.response.status_code}"
        except Exception as exc:  # any other problem becomes a failed check
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        latency = int((time.perf_counter() - start) * 1000)
        return CheckResult(
            id=check_id, name=name, category=category, status=status, latency_ms=latency, detail=self._safe(detail)
        )

    async def _get(self, url: str, **params) -> httpx.Response:
        resp = await self._client.get(url, params=params or None, timeout=self._s.check_timeout_seconds)
        resp.raise_for_status()
        return resp

    def _window(self) -> tuple[datetime, datetime]:
        end = datetime.now(timezone.utc)
        return end - timedelta(minutes=self._s.signal_window_minutes), end

    # ---------- component checks ----------

    def _component_targets(self) -> list[tuple[str, str, str]]:
        s = self._s
        return [
            ("otel-collector", "OpenTelemetry Collector", f"{s.otel_collector_health_url}/"),
            ("tempo", "Tempo (traces)", f"{s.tempo_url}/ready"),
            ("loki", "Loki (logs)", f"{s.loki_url}/ready"),
            ("prometheus", "Prometheus (metrics)", f"{s.prometheus_url}/-/ready"),
            ("alertmanager", "Alertmanager (alerts)", f"{s.alertmanager_url}/-/ready"),
            ("shop-demo", "shop-demo service", f"{s.shop_demo_url}/health"),
        ]

    async def _component(self, check_id: str, name: str, url: str) -> CheckResult:
        async def run():
            resp = await self._client.get(url, timeout=self._s.check_timeout_seconds)
            if resp.status_code == 503:
                # Tempo and Loki answer 503 for ~30 s after starting while their ring warms up.
                return "warning", "not ready yet (HTTP 503); normal for about 30 s after a start"
            resp.raise_for_status()
            return "success", f"ready (HTTP {resp.status_code})"

        return await self._timed(check_id, name, "component", run)

    # ---------- signal checks ----------

    async def _traces(self) -> tuple[CheckStatus, str]:
        start, end = self._window()
        query = f'{{ resource.service.name = "{self._s.monitored_service}" && status = error }}'
        resp = await self._get(
            f"{self._s.tempo_url}/api/search",
            q=query,
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            limit=20,
        )
        traces = resp.json().get("traces") or []
        if traces:
            n = f"{len(traces)}+" if len(traces) >= 20 else str(len(traces))
            return "success", f"{n} error trace(s) in the last {self._s.signal_window_minutes} min"
        return "warning", "no error traces yet (enable the bug and generate traffic)"

    async def _logs(self) -> tuple[CheckStatus, str]:
        start, end = self._window()
        query = f'{{service_name="{self._s.monitored_service}"}} | severity_text="ERROR"'
        resp = await self._get(
            f"{self._s.loki_url}/loki/api/v1/query_range",
            query=query,
            start=int(start.timestamp() * 1e9),
            end=int(end.timestamp() * 1e9),
            limit=50,
        )
        streams = resp.json().get("data", {}).get("result") or []
        count = sum(len(s.get("values") or []) for s in streams)
        if count:
            n = f"{count}+" if count >= 50 else str(count)
            return "success", f"{n} ERROR log line(s) in the last {self._s.signal_window_minutes} min"
        return "warning", "no ERROR logs yet"

    async def _metrics(self) -> tuple[CheckStatus, str]:
        svc = self._s.monitored_service
        total_q = f'sum(rate(shop_http_requests_total{{service_name="{svc}"}}[1m]))'
        err_q = f'sum(rate(shop_http_requests_total{{service_name="{svc}",status_code=~"5.."}}[1m]))'
        total = (await self._get(f"{self._s.prometheus_url}/api/v1/query", query=total_q)).json()
        result = total.get("data", {}).get("result") or []
        if not result:
            return "warning", "no request metrics yet (generate traffic)"
        total_rate = float(result[0]["value"][1])
        err = (await self._get(f"{self._s.prometheus_url}/api/v1/query", query=err_q)).json()
        err_result = err.get("data", {}).get("result") or []
        err_rate = float(err_result[0]["value"][1]) if err_result else 0.0
        ratio = (err_rate / total_rate * 100) if total_rate else 0.0
        return "success", f"request metrics present; 5xx ratio {ratio:.1f}% over 1 min"

    async def _alert(self) -> tuple[CheckStatus, str]:
        resp = await self._get(
            f"{self._s.alertmanager_url}/api/v2/alerts", filter='alertname="ShopDemoHighErrorRate"'
        )
        alerts = resp.json() or []
        active = [a for a in alerts if (a.get("status") or {}).get("state") == "active"]
        if active:
            return "success", "ShopDemoHighErrorRate is firing in Alertmanager"
        return "warning", "ShopDemoHighErrorRate not firing"

    # ---------- public API ----------

    async def run_all(self) -> SystemChecksReport:
        components = [self._component(*t) for t in self._component_targets()]
        signals = [
            self._timed("signal-traces", "Error traces reach Tempo", "signal", self._traces),
            self._timed("signal-logs", "Error logs reach Loki", "signal", self._logs),
            self._timed("signal-metrics", "Request metrics reach Prometheus", "signal", self._metrics),
            self._timed("signal-alert", "Alert fires in Alertmanager", "signal", self._alert),
        ]
        results = await asyncio.gather(*components, *signals)
        comp, sig = list(results[: len(components)]), list(results[len(components) :])
        return SystemChecksReport(overall=overall_status(comp + sig), components=comp, signals=sig)

    async def list_bugs(self) -> dict:
        r = await self._client.get(f"{self._s.shop_demo_url}/admin/bugs", timeout=5)
        r.raise_for_status()
        return r.json()

    async def run_action(self, action: str, requests: int = 50, bug: str | None = None) -> CheckResult:
        base = self._s.shop_demo_url
        bug = bug or PLANTED_BUG

        async def enable():
            r = await self._client.post(f"{base}/admin/bugs/{bug}/enable", timeout=5)
            r.raise_for_status()
            return "success", f"planted failure '{bug}' enabled"

        async def disable():
            r = await self._client.post(f"{base}/admin/bugs/{bug}/disable", timeout=5)
            r.raise_for_status()
            return "success", f"planted failure '{bug}' disabled"

        async def traffic():
            r = await self._client.post(f"{base}/admin/traffic", params={"requests": requests}, timeout=60)
            r.raise_for_status()
            counts = r.json().get("status_counts", {})
            errors = sum(v for k, v in counts.items() if k.startswith("5"))
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
            status: CheckStatus = "warning" if errors else "success"
            return status, f"sent {requests} checkout requests ({summary})"

        async def start_load():
            r = await self._client.post(f"{base}/admin/load/start", params={"rps": 3, "seconds": 180}, timeout=5)
            r.raise_for_status()
            return "success", "steady load started: 3 requests/s for 3 minutes"

        async def stop_load():
            r = await self._client.post(f"{base}/admin/load/stop", timeout=5)
            r.raise_for_status()
            return "success", "steady load stopped"

        fns = {
            "enable_bug": enable,
            "disable_bug": disable,
            "generate_traffic": traffic,
            "start_load": start_load,
            "stop_load": stop_load,
        }
        names = {
            "enable_bug": "Enable planted bug",
            "disable_bug": "Disable planted bug",
            "generate_traffic": "Generate checkout traffic",
            "start_load": "Start steady load",
            "stop_load": "Stop steady load",
        }
        if action not in fns:
            return CheckResult(id=f"action-{action}", name=action, category="action", status="failed",
                               detail="unknown action")
        # Traffic can take longer than a normal check.
        if action == "generate_traffic":
            start = time.perf_counter()
            try:
                status, detail = await traffic()
            except Exception as exc:
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
            return CheckResult(id=f"action-{action}", name=names[action], category="action", status=status,
                               latency_ms=int((time.perf_counter() - start) * 1000), detail=self._safe(detail))
        return await self._timed(f"action-{action}", names[action], "action", fns[action])
