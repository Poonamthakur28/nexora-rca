"""Adapters for the fixed mitigation actions exposed by the service catalog."""

import re
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Service, ServiceDeployment
from app.modules.catalog.service import find_service, record_deployment


class ActionPlan(BaseModel):
    """The service and parameters needed by an adapter.

    The persisted mitigation plan introduced by the policy layer can be converted to this
    small execution contract without allowing model-generated URLs or commands.
    """

    organization_id: str
    service: str
    environment: str = "local"
    params: dict[str, Any] = Field(default_factory=dict)


class AdapterError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class PreflightResult(BaseModel):
    ok: bool
    action_type: str
    detail: str
    error: AdapterError | None = None


class ExecutionResult(BaseModel):
    ok: bool
    action_type: str
    message: str
    before_state: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: AdapterError | None = None


class ActionAdapter(ABC):
    """Base for deterministic adapters; subclasses may only call their fixed endpoints."""

    action_type: str

    def __init__(self, session: Session, settings: Settings, http: httpx.Client | None = None):
        self.session = session
        self.settings = settings
        self.http = http or httpx.Client()

    @abstractmethod
    def preflight(self, plan: ActionPlan) -> PreflightResult:
        raise NotImplementedError

    @abstractmethod
    def execute(self, plan: ActionPlan) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def undo(self, execution: ExecutionResult) -> ExecutionResult:
        raise NotImplementedError

    def _request(self, method: str, path: str) -> tuple[dict[str, Any] | None, AdapterError | None]:
        """Call one fixed shop-demo admin path and turn all transport failures into data."""
        url = f"{self.settings.shop_demo_url.rstrip('/')}{path}"
        try:
            response = self.http.request(method, url, timeout=self.settings.check_timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None, AdapterError(code="invalid_response", message="shop-demo returned a non-object response")
            return payload, None
        except httpx.TimeoutException:
            return None, AdapterError(
                code="timeout",
                message=f"shop-demo did not answer within {self.settings.check_timeout_seconds:g} seconds",
                retryable=True,
            )
        except httpx.HTTPStatusError as exc:
            return None, AdapterError(
                code=f"http_{exc.response.status_code}",
                message=f"shop-demo rejected the action with HTTP {exc.response.status_code}",
            )
        except (httpx.RequestError, ValueError) as exc:
            return None, AdapterError(code="request_failed", message=f"shop-demo request failed: {exc}", retryable=True)

    def _service(self, plan: ActionPlan) -> Service | None:
        return find_service(self.session, plan.organization_id, plan.service, plan.environment)

    def _failure(self, message: str, error: AdapterError,
                 before_state: dict[str, Any] | None = None) -> ExecutionResult:
        return ExecutionResult(
            ok=False,
            action_type=self.action_type,
            message=message,
            before_state=before_state or {},
            error=error,
        )


class DisableFeatureAdapter(ActionAdapter):
    action_type = "disable_feature"

    def _flag(self, plan: ActionPlan) -> str | None:
        flag = plan.params.get("flag")
        return flag if isinstance(flag, str) and flag else None

    def preflight(self, plan: ActionPlan) -> PreflightResult:
        flag = self._flag(plan)
        if flag is None:
            error = AdapterError(code="invalid_params", message="params.flag is required")
            return PreflightResult(ok=False, action_type=self.action_type, detail=error.message, error=error)
        bugs, error = self._request("GET", "/admin/bugs")
        if error:
            return PreflightResult(ok=False, action_type=self.action_type, detail=error.message, error=error)
        if flag not in bugs:
            error = AdapterError(code="flag_not_found", message=f"unknown planted failure flag {flag}")
            return PreflightResult(ok=False, action_type=self.action_type, detail=error.message, error=error)
        return PreflightResult(ok=True, action_type=self.action_type, detail=f"flag {flag} exists")

    def execute(self, plan: ActionPlan) -> ExecutionResult:
        flag = self._flag(plan)
        if flag is None:
            return self._failure(
                "Feature was not disabled",
                AdapterError(code="invalid_params", message="params.flag is required"),
            )
        bugs, error = self._request("GET", "/admin/bugs")
        if error:
            return self._failure("Could not capture feature state", error)
        if flag not in bugs:
            return self._failure(
                "Feature was not disabled",
                AdapterError(code="flag_not_found", message=f"unknown planted failure flag {flag}"),
            )
        before = {"flag": flag, "enabled": bool(bugs[flag].get("enabled"))}
        payload, error = self._request("POST", f"/admin/bugs/{flag}/disable")
        if error:
            return self._failure(f"Failed to disable {flag}", error, before)
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message=f"Disabled {flag}",
            before_state=before,
            result=payload or {},
        )

    def undo(self, execution: ExecutionResult) -> ExecutionResult:
        flag = execution.before_state.get("flag")
        was_enabled = execution.before_state.get("enabled")
        if not isinstance(flag, str) or not isinstance(was_enabled, bool):
            return self._failure(
                "Feature state could not be restored",
                AdapterError(code="invalid_before_state", message="execution has no exact feature state"),
                execution.before_state,
            )
        if not was_enabled:
            return ExecutionResult(
                ok=True,
                action_type=self.action_type,
                message=f"{flag} was already disabled; no change needed",
                before_state=execution.before_state,
                result={"flag": flag, "enabled": False, "changed": False},
            )
        payload, error = self._request("POST", f"/admin/bugs/{flag}/enable")
        if error:
            return self._failure(f"Failed to restore {flag}", error, execution.before_state)
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message=f"Restored {flag} to enabled",
            before_state=execution.before_state,
            result=payload or {},
        )


class RollbackReleaseAdapter(ActionAdapter):
    action_type = "rollback_release"
    _BUG_RE = re.compile(r"planted bug ([A-Za-z0-9_-]+)", re.IGNORECASE)

    def _release_context(
        self, plan: ActionPlan
    ) -> tuple[Service | None, ServiceDeployment | None, ServiceDeployment | None, str | None]:
        service = self._service(plan)
        if service is None:
            return None, None, None, None
        current = self.session.scalars(
            select(ServiceDeployment)
            .where(
                ServiceDeployment.organization_id == plan.organization_id,
                ServiceDeployment.service_id == service.id,
                ServiceDeployment.source == "test_bench",
            )
            .order_by(ServiceDeployment.deployed_at.desc())
            .limit(1)
        ).first()
        if current is None:
            return service, None, None, None
        previous = self.session.scalars(
            select(ServiceDeployment)
            .where(
                ServiceDeployment.organization_id == plan.organization_id,
                ServiceDeployment.service_id == service.id,
                ServiceDeployment.deployed_at < current.deployed_at,
            )
            .order_by(ServiceDeployment.deployed_at.desc())
            .limit(1)
        ).first()
        match = self._BUG_RE.search(current.description)
        return service, current, previous, match.group(1) if match else None

    def preflight(self, plan: ActionPlan) -> PreflightResult:
        service, current, previous, bug = self._release_context(plan)
        if service is None:
            error = AdapterError(code="service_not_found", message="service is not in the catalog")
        elif current is None or bug is None:
            error = AdapterError(code="release_not_found", message="no current planted-bug release was found")
        elif previous is None:
            error = AdapterError(code="previous_release_not_found", message="no previous release was found")
        else:
            return PreflightResult(
                ok=True,
                action_type=self.action_type,
                detail=f"{current.version} can be rolled back to {previous.version} by disabling {bug}",
            )
        return PreflightResult(ok=False, action_type=self.action_type, detail=error.message, error=error)

    def execute(self, plan: ActionPlan) -> ExecutionResult:
        service, current, previous, bug = self._release_context(plan)
        if service is None or current is None or previous is None or bug is None:
            preflight = self.preflight(plan)
            return self._failure("Release was not rolled back", preflight.error or AdapterError(
                code="preflight_failed", message=preflight.detail
            ))
        bugs, error = self._request("GET", "/admin/bugs")
        if error:
            return self._failure("Could not capture release state", error)
        if bug not in bugs:
            return self._failure(
                "Release was not rolled back",
                AdapterError(code="flag_not_found", message=f"release refers to unknown planted failure flag {bug}"),
            )
        before = {
            "bug": bug,
            "bug_enabled": bool(bugs[bug].get("enabled")),
            "organization_id": plan.organization_id,
            "service": plan.service,
            "environment": plan.environment,
            "current_version": current.version,
            "current_changed_files": list(current.changed_files or []),
            "previous_version": previous.version,
        }
        payload, error = self._request("POST", f"/admin/bugs/{bug}/disable")
        if error:
            return self._failure(f"Failed to disable {bug}", error, before)
        deployment = record_deployment(
            self.session,
            service,
            previous.version,
            f"rollback to {previous.version} from {current.version}",
            list(previous.changed_files or []),
            commit_sha=previous.commit_sha,
            source="test_bench",
            has_irreversible_migration=previous.has_irreversible_migration,
        )
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message=f"Rolled back {current.version} to {previous.version}",
            before_state=before,
            result={"bug": payload, "deployment_id": str(deployment.id), "version": previous.version},
        )

    def undo(self, execution: ExecutionResult) -> ExecutionResult:
        state = execution.before_state
        bug = state.get("bug")
        current_version = state.get("current_version")
        organization_id = state.get("organization_id")
        service_name = state.get("service")
        environment = state.get("environment", "local")
        if not all(isinstance(value, str) and value for value in (bug, current_version, organization_id, service_name)):
            return self._failure(
                "Rollback could not be undone",
                AdapterError(code="invalid_before_state", message="execution lacks release or service identity"),
                state,
            )
        if state.get("bug_enabled"):
            payload, error = self._request("POST", f"/admin/bugs/{bug}/enable")
            if error:
                return self._failure(f"Failed to re-enable {bug}", error, state)
        else:
            payload = {"name": bug, "enabled": False, "changed": False}
        service = find_service(self.session, organization_id, service_name, environment)
        if service is None:
            return self._failure(
                "Rollback could not be undone",
                AdapterError(code="service_not_found", message="service is not in the catalog"),
                state,
            )
        deployment = record_deployment(
            self.session,
            service,
            current_version,
            f"roll-forward to {current_version}; restores planted bug {bug} after undoing rollback",
            list(state.get("current_changed_files") or []),
            source="test_bench",
        )
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message=f"Rolled forward to {current_version}",
            before_state=state,
            result={"bug": payload, "deployment_id": str(deployment.id), "version": current_version},
        )


class RestartServiceAdapter(ActionAdapter):
    action_type = "restart_service"

    def preflight(self, plan: ActionPlan) -> PreflightResult:
        if self._service(plan) is None:
            error = AdapterError(code="service_not_found", message="service is not in the catalog")
            return PreflightResult(ok=False, action_type=self.action_type, detail=error.message, error=error)
        return PreflightResult(ok=True, action_type=self.action_type, detail="service is in the catalog")

    def execute(self, plan: ActionPlan) -> ExecutionResult:
        payload, error = self._request("POST", "/admin/restart")
        if error:
            return self._failure("Service was not restarted", error, {"reversible": False})
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message="Restarted the service connection pool",
            before_state={"reversible": False, "remote": payload.get("before", {}) if payload else {}},
            result=payload or {},
        )

    def undo(self, execution: ExecutionResult) -> ExecutionResult:
        return ExecutionResult(
            ok=True,
            action_type=self.action_type,
            message="Restart is not reversible; no undo action was run",
            before_state=execution.before_state,
            result={"reversible": False, "changed": False},
        )


ADAPTERS: dict[str, type[ActionAdapter]] = {
    "rollback_release": RollbackReleaseAdapter,
    "disable_feature": DisableFeatureAdapter,
    "restart_service": RestartServiceAdapter,
}


def adapter_for(action_type: str, session: Session, settings: Settings,
                http: httpx.Client | None = None) -> ActionAdapter | None:
    cls = ADAPTERS.get(action_type)
    if cls is None:
        return None
    return cls(session, settings, http)
