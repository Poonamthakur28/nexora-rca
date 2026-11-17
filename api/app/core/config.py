"""Application settings, read from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"

    # Observability stack (defaults match deploy/docker-compose.yml published ports)
    otel_collector_health_url: str = "http://localhost:13133"
    tempo_url: str = "http://localhost:3200"
    loki_url: str = "http://localhost:3100"
    prometheus_url: str = "http://localhost:9090"
    alertmanager_url: str = "http://localhost:9093"
    shop_demo_url: str = "http://localhost:8080"

    # Shared with Alertmanager's webhook config. Never echoed in API responses.
    alertmanager_webhook_token: str = "change-me-local-token"

    check_timeout_seconds: float = 3.0
    signal_window_minutes: int = 15
    monitored_service: str = "shop-demo"

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
