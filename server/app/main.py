import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.errors import (
    AppError,
    app_error_handler,
    error_response,
    http_error_handler,
    validation_error_handler,
)
from app.maintenance import MaintenanceGate
from app.persistence.database import Database
from app.security.rate_limit import LoginRateLimiter
from app.services.command_executor import CommandExecutor
from app.settings import Settings

logger = logging.getLogger("procure.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings if settings is not None else Settings()
    database = Database(
        configured.database_url.get_secret_value() if configured.database_url else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        database.close()

    application = FastAPI(title="Procure Reconcile API", version="0.1.0", lifespan=lifespan)
    application.state.settings = configured
    application.state.database = database
    application.state.login_limiter = LoginRateLimiter()
    application.state.maintenance_gate = MaintenanceGate(
        configured.maintenance_dir or configured.files_dir.parent / "maintenance"
    )
    application.state.command_executor = CommandExecutor(
        database,
        lock_timeout_ms=configured.command_lock_timeout_ms,
        gate=application.state.maintenance_gate,
    )
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(HTTPException, http_error_handler)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        logger.warning(
            "request_id=%s error_type=%s",
            getattr(request.state, "request_id", "unavailable"),
            type(error).__name__,
        )
        return error_response(503, "database_unavailable", "数据库暂时不可用")

    @application.exception_handler(Exception)
    async def unexpected_error(request, error):
        request_id = getattr(request.state, "request_id", "unavailable")
        logger.error("request_id=%s error_type=%s", request_id, type(error).__name__)
        response = error_response(500, "internal_error", "服务处理失败，请稍后重试")
        response.headers["X-Request-ID"] = request_id
        return response

    @application.middleware("http")
    async def no_store(request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    from app.api import router

    application.include_router(router)
    from app.documents.api import router as file_router

    application.include_router(file_router)
    return application


app = create_app()
