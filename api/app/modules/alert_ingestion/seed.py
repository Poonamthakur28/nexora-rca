"""Creates the local Alertmanager integration from configuration (idempotent)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AlertIntegration

from .alertmanager import PROVIDER
from .fingerprint import token_hash

LOCAL_INTEGRATION_NAME = "local-alertmanager"


def ensure_local_integration(session: Session, settings: Settings) -> AlertIntegration:
    integration = session.scalars(
        select(AlertIntegration).where(
            AlertIntegration.organization_id == settings.default_organization_id,
            AlertIntegration.provider == PROVIDER,
            AlertIntegration.name == LOCAL_INTEGRATION_NAME,
        )
    ).first()
    hashed = token_hash(settings.alertmanager_webhook_token)
    if integration is None:
        integration = AlertIntegration(
            organization_id=settings.default_organization_id, provider=PROVIDER,
            name=LOCAL_INTEGRATION_NAME, token_sha256=hashed, enabled=True,
        )
        session.add(integration)
    elif integration.token_sha256 != hashed:
        integration.token_sha256 = hashed  # token rotated in configuration
    session.commit()
    return integration
