"""Application settings, read from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"

    database_url: str = "postgresql+psycopg://nexora:nexora@localhost:5433/nexora"
    # Local single-tenant default. Real tenants come from verified integrations.
    default_organization_id: str = "org_local"
    webhook_max_body_bytes: int = 1_000_000
    worker_poll_seconds: float = 1.0
    # Step 3: service catalog and evidence collection
    service_catalog_file: str = ""
    # Optional machine-local file that overrides only source_root / runtime_path_prefix per service
    # (e.g. Nexora and the service running natively on Windows). Unset in Docker.
    service_catalog_overrides_file: str = ""
    # A resolved alert that fires again within this many minutes rejoins its incident (a flap);
    # later it is a recurrence and opens a new incident.
    alert_reopen_window_minutes: int = 15
    evidence_lookback_minutes: int = 15
    evidence_max_items: int = 5
    job_lease_seconds: int = 60

    # Observability stack (defaults match deploy/docker-compose.yml published ports)
    otel_collector_health_url: str = "http://localhost:13133"
    tempo_url: str = "http://localhost:3200"
    loki_url: str = "http://localhost:3100"
    prometheus_url: str = "http://localhost:9090"
    alertmanager_url: str = "http://localhost:9093"
    shop_demo_url: str = "http://localhost:8080"

    # Shared with Alertmanager's webhook config. Never echoed in API responses.
    alertmanager_webhook_token: str = "change-me-local-token"

    # Step 4: AI investigation. Provider: heuristic (no LLM, rule-based baseline) | anthropic | openai | ollama.
    rca_enabled: bool = True
    rca_provider: str = "heuristic"
    rca_model: str = ""  # empty = the provider's default (see app/modules/rca/providers.py)
    rca_timeout_seconds: float = 120.0
    rca_max_output_tokens: int = 2500
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_url: str = "http://host.docker.internal:11434"

    # Step 5: propose one catalog action after a successful investigation.
    mitigation_enabled: bool = True
    recovery_window_seconds: int = 120
    recovery_poll_seconds: int = 15

    check_timeout_seconds: float = 3.0
    signal_window_minutes: int = 15
    monitored_service: str = "shop-demo"

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
