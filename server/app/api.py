from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.persistence.models import Administrator, AuthSession
from app.readiness import check_readiness
from app.security.passwords import (
    hasher,
    new_session_token,
    token_digest,
    verify_password,
)

router = APIRouter()
bearer = HTTPBearer(auto_error=False)
# A missing username still performs a password verification. This is not an account password.
DUMMY_PASSWORD_HASH = hasher.hash(new_session_token())


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=128)

    @field_validator("username", "device_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Value cannot be empty or contain control characters")
        return value


class UserOutput(BaseModel):
    username: str


class LoginOutput(BaseModel):
    token: str
    expires_at: datetime
    user: UserOutput


class SessionOutput(BaseModel):
    username: str
    expires_at: datetime


class SystemOutput(BaseModel):
    environment: str
    instance_id: str
    app_version: str
    api_version: str
    ready: bool
    reason: str | None


def database_session(request: Request) -> Iterator[Session]:
    status = check_readiness(request.app.state.settings, request.app.state.database)
    if not status.ready:
        raise AppError(503, status.reason or "service_not_ready", "服务尚未就绪，请检查初始化状态")
    with request.app.state.database.sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(database_session)]


@dataclass
class AuthContext:
    user: Administrator
    session: AuthSession


def authenticated(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise AppError(401, "authentication_required", "请先登录")
    if not 32 <= len(credentials.credentials) <= 128:
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    stored = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_digest(credentials.credentials))
    )
    if stored is None or stored.revoked_at is not None or stored.expires_at <= datetime.now(UTC):
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    user = session.get(Administrator, stored.administrator_id)
    if user is None:
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    return AuthContext(user=user, session=stored)


AuthDep = Annotated[AuthContext, Depends(authenticated)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/api/v1/system", response_model=SystemOutput)
def system(request: Request) -> SystemOutput:
    configured = request.app.state.settings
    status = check_readiness(configured, request.app.state.database)
    return SystemOutput(
        environment=configured.environment,
        instance_id=configured.instance_id or "unconfigured",
        app_version=request.app.version,
        api_version="1",
        ready=status.ready,
        reason=status.reason,
    )


@router.post("/api/v1/auth/login", response_model=LoginOutput)
def login(payload: LoginInput, request: Request, session: SessionDep) -> LoginOutput:
    peer = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_limiter
    limiter.check(peer)
    user = session.scalar(select(Administrator).where(Administrator.username == payload.username))
    matches = verify_password(
        payload.password.get_secret_value(), user.password_hash if user else DUMMY_PASSWORD_HASH
    )
    if user is None or not matches:
        limiter.failed(peer)
        raise AppError(401, "invalid_credentials", "用户名或密码错误")
    token = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=request.app.state.settings.session_hours)
    session.add(
        AuthSession(
            administrator_id=user.id,
            token_hash=token_digest(token),
            device_name=payload.device_name,
            expires_at=expires_at,
        )
    )
    session.commit()
    limiter.succeeded(peer)
    return LoginOutput(token=token, expires_at=expires_at, user=UserOutput(username=user.username))


@router.get("/api/v1/auth/session", response_model=SessionOutput)
def current_session(auth: AuthDep) -> SessionOutput:
    return SessionOutput(username=auth.user.username, expires_at=auth.session.expires_at)


@router.post("/api/v1/auth/logout", status_code=204)
def logout(auth: AuthDep, session: SessionDep) -> Response:
    auth.session.revoked_at = datetime.now(UTC)
    session.commit()
    return Response(status_code=204)


@router.get("/api/v1/workspace")
def workspace(request: Request, auth: AuthDep) -> dict[str, str]:
    configured = request.app.state.settings
    return {
        "message": "连接与登录已就绪",
        "environment": configured.environment,
        "instance_id": configured.instance_id,
        "username": auth.user.username,
        "database": "connected",
    }
