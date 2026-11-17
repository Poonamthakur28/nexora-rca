from fastapi import APIRouter, Depends, Query, Request

from app.core.config import Settings, get_settings

from .contracts import CheckResult, SystemChecksReport
from .service import SystemChecksService

router = APIRouter(prefix="/api/v1/system-checks", tags=["system-checks"])


def get_service(request: Request, settings: Settings = Depends(get_settings)) -> SystemChecksService:
    return SystemChecksService(settings, request.app.state.http_client)


@router.get("", response_model=SystemChecksReport)
async def run_checks(service: SystemChecksService = Depends(get_service)) -> SystemChecksReport:
    return await service.run_all()


@router.post("/actions/{action}", response_model=CheckResult)
async def run_action(
    action: str,
    requests: int = Query(50, ge=1, le=500),
    service: SystemChecksService = Depends(get_service),
) -> CheckResult:
    return await service.run_action(action, requests=requests)
