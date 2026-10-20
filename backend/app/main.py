"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.api.admin import admin_router
from app.api.ingest import ingest_router
from app.clock import Clock, SystemClock
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory, init_db


def _health_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    def health(request: Request) -> dict:
        settings: Settings = request.app.state.settings
        return {
            "status": "ok",
            "ai_provider": settings.ai_provider,
            "ai_provider_mock": settings.ai_provider_is_mock,
        }

    return router


def create_app(
    settings: Settings | None = None,
    *,
    create_tables: bool = True,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the API.

    With ``create_tables`` the schema is created immediately; otherwise it is
    created on application startup (used by the module-level ``app`` so that
    importing this module never touches the database).
    """
    settings = settings or get_settings()

    engine = create_db_engine(settings.database_url)
    if create_tables:
        init_db(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Sentinel AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.clock = clock or SystemClock()

    app.include_router(_health_router())
    app.include_router(admin_router())
    app.include_router(ingest_router())
    return app


app = create_app(create_tables=False)
