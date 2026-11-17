"""Nexora RCA engine API."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.modules.system_checks.routes import router as system_checks_router


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient()
        yield
        await app.state.http_client.aclose()

    app = FastAPI(title="Nexora RCA Engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "nexora-api"}

    # The test-bench checks and actions are for local development only.
    if settings.is_development:
        app.include_router(system_checks_router)

    # Serve the built UI (ui/dist) when present, so one container runs API + UI.
    ui_dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app


app = create_app()
