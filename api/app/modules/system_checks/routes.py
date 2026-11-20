import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.config import Settings, get_settings
from app.db.base import get_session_factory

from app.modules.catalog.service import record_bench_release

from .contracts import CheckResult, SystemChecksReport, overall_status
from .db_checks import run_db_checks
from .service import SystemChecksService

router = APIRouter(prefix="/api/v1/system-checks", tags=["system-checks"])


def get_service(request: Request, settings: Settings = Depends(get_settings)) -> SystemChecksService:
    return SystemChecksService(settings, request.app.state.http_client)


@router.get("", response_model=SystemChecksReport)
async def run_checks(
    service: SystemChecksService = Depends(get_service), settings: Settings = Depends(get_settings)
) -> SystemChecksReport:
    report, (db_components, db_signals), sandbox = await asyncio.gather(
        service.run_all(), asyncio.to_thread(run_db_checks, get_session_factory(), settings),
        asyncio.to_thread(_sandbox_check, settings),
    )
    components = db_components + report.components + ([sandbox] if sandbox else [])
    signals = report.signals + db_signals
    return SystemChecksReport(overall=overall_status(components + signals), components=components, signals=signals)


@router.get("/bugs")
async def list_bugs(service: SystemChecksService = Depends(get_service)) -> dict:
    """The planted failures shop-demo offers (test bench only)."""
    try:
        return await service.list_bugs()
    except Exception as exc:
        raise HTTPException(502, f"shop-demo unavailable: {type(exc).__name__}")


@router.post("/actions/{action}", response_model=CheckResult)
async def run_action(
    action: str,
    requests: int = Query(50, ge=1, le=500),
    bug: str | None = Query(None, max_length=64, pattern=r"^[a-z0-9_]+$"),
    service: SystemChecksService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> CheckResult:
    result = await service.run_action(action, requests=requests, bug=bug)
    if action in ("enable_bug", "disable_bug") and result.status == "success":
        # Code and resource bugs arrive with a release; an external dependency slowing down does not.
        try:
            info = (await service.list_bugs()).get(bug or "code_null_tax", {})
        except Exception:
            info = {}
        files = info.get("release_files") or []
        if files:
            version = await asyncio.to_thread(_bench_release, settings, action == "enable_bug", bug, files)
            if version:
                result.detail += f"; recorded simulated release {version}"
        elif info.get("kind") == "dependency":
            result.detail += "; no release recorded (external dependency)"
    return result


def _sandbox_check(settings: Settings) -> CheckResult | None:
    """Step 6: can the fix track run tests? Amber in subprocess mode (files isolated, network not)."""
    if not settings.fix_enabled:
        return None
    import time

    from app.modules.fix.sandbox import sandbox_status

    start = time.perf_counter()
    status, detail = sandbox_status(settings)
    return CheckResult(id="fix-sandbox", name="Fix sandbox (Step 6)", category="component", status=status,
                       detail=detail, latency_ms=int((time.perf_counter() - start) * 1000))


def _bench_release(settings: Settings, enabled: bool, bug: str | None, files: list[str]) -> str | None:
    try:
        with get_session_factory()() as session:
            return record_bench_release(session, settings, enabled, bug or "code_null_tax", files)
    except Exception:
        return None
