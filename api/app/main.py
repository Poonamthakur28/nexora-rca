"""Nexora RCA engine API."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.db.base import get_session_factory
from app.modules.alert_ingestion.routes import router as alerts_router
from app.modules.alert_ingestion.seed import ensure_local_integration
from app.modules.catalog.routes import router as catalog_router
from app.modules.catalog.service import seed_from_file
from app.modules.evidence.routes import router as evidence_router
from app.modules.rca.routes import router as rca_router
from app.modules.mitigation.routes import router as mitigation_router
from app.modules.incidents.routes import router as incidents_router
from app.modules.pipeline.routes import router as pipeline_router
from app.modules.system_checks.routes import router as system_checks_router

log = logging.getLogger("nexora.api")


def create_app(seed: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient()
        if seed:
            try:
                with get_session_factory()() as session:
                    ensure_local_integration(session, settings)
                    seed_from_file(session, settings)
            except Exception as exc:  # the API still starts; the Checks page shows PostgreSQL as failed
                log.error("could not seed local integration: %s", exc)
        yield
        await app.state.http_client.aclose()

    app = FastAPI(title="Nexora RCA Engine", version="0.3.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "nexora-api"}

    app.include_router(alerts_router)
    app.include_router(incidents_router)
    app.include_router(pipeline_router)
    app.include_router(catalog_router)
    app.include_router(evidence_router)
    app.include_router(rca_router)
    app.include_router(mitigation_router)
    # The test-bench checks and actions are for local development only.
    if settings.is_development:
        app.include_router(system_checks_router)

    # Serve the built UI (ui/dist) when present, so one container runs API + UI.
    ui_dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app


app = create_app()
