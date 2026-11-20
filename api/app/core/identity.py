"""Who is calling. Local dev uses a header with a default; Team 5 identity replaces this in M7."""

from fastapi import Depends, Header

from app.core.config import Settings, get_settings


def get_organization_id(
    x_nexora_organization_id: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    return (x_nexora_organization_id or settings.default_organization_id).strip()


def get_actor(x_nexora_actor: str | None = Header(default=None)) -> str:
    return (x_nexora_actor or "local-user").strip() or "local-user"
