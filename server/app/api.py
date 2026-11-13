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
from app.persistence.models import AdministratorDO, AuthSessionDO
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


class LoginDTO(BaseModel):
    """登录请求传输对象，接收用户名、密码和设备名称。"""

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


class UserVO(BaseModel):
    """用户展示对象，作为接口响应中的用户资料。"""

    username: str


class LoginVO(BaseModel):
    """登录结果展示对象，返回会话令牌、有效期和用户资料。"""

    token: str
    expires_at: datetime
    user: UserVO


class SessionVO(BaseModel):
    """会话展示对象，返回当前登录用户和到期时间。"""

    username: str
    expires_at: datetime


class SystemVO(BaseModel):
    """系统状态展示对象，返回环境、实例及服务就绪状态。"""

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
    # 共享写锁覆盖Session退出，维护备份会等待所有已有请求完成。
    with request.app.state.maintenance_gate.writer():
        with request.app.state.database.sessions() as session:
            yield session


SessionDep = Annotated[Session, Depends(database_session)]


@dataclass
class AuthContext:
    """当前请求的认证上下文，关联管理员记录和登录会话。"""

    user: AdministratorDO
    session: AuthSessionDO


def authenticated(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise AppError(401, "authentication_required", "请先登录")
    if not 32 <= len(credentials.credentials) <= 128:
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    stored = session.scalar(
        select(AuthSessionDO).where(
            AuthSessionDO.token_hash == token_digest(credentials.credentials)
        )
    )
    if stored is None or stored.revoked_at is not None or stored.expires_at <= datetime.now(UTC):
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    user = session.get(AdministratorDO, stored.administrator_id)
    if user is None:
        raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
    return AuthContext(user=user, session=stored)


AuthDep = Annotated[AuthContext, Depends(authenticated)]


@dataclass(frozen=True)
class ActorDTO:
    """供业务命令使用的身份快照，不向回调传递口令或登录令牌。"""

    id: int
    username: str


def current_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> ActorDTO:
    """先完成鉴权并关闭只读Session，避免上传期间长期占用数据库连接。"""
    database = request.app.state.database
    status = check_readiness(request.app.state.settings, database)
    if not status.ready:
        raise AppError(503, status.reason or "service_not_ready", "服务尚未就绪")
    with request.app.state.maintenance_gate.writer():
        with database.sessions() as session:
            auth = authenticated(session, credentials)
            return ActorDTO(id=auth.user.id, username=auth.user.username)


ActorDep = Annotated[ActorDTO, Depends(current_actor)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/api/v1/system", response_model=SystemVO)
def system(request: Request) -> SystemVO:
    configured = request.app.state.settings
    status = check_readiness(configured, request.app.state.database)
    maintenance = request.app.state.maintenance_gate.is_maintenance()
    return SystemVO(
        environment=configured.environment,
        instance_id=configured.instance_id or "unconfigured",
        app_version=request.app.version,
        api_version="1",
        ready=status.ready and not maintenance,
        reason="maintenance" if maintenance else status.reason,
    )


@router.post("/api/v1/auth/login", response_model=LoginVO)
def login(payload: LoginDTO, request: Request, session: SessionDep) -> LoginVO:
    peer = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_limiter
    limiter.check(peer)
    user = session.scalar(
        select(AdministratorDO).where(AdministratorDO.username == payload.username)
    )
    matches = verify_password(
        payload.password.get_secret_value(), user.password_hash if user else DUMMY_PASSWORD_HASH
    )
    if user is None or not matches:
        limiter.failed(peer)
        raise AppError(401, "invalid_credentials", "用户名或密码错误")
    token = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=request.app.state.settings.session_hours)
    session.add(
        AuthSessionDO(
            administrator_id=user.id,
            token_hash=token_digest(token),
            device_name=payload.device_name,
            expires_at=expires_at,
        )
    )
    session.commit()
    limiter.succeeded(peer)
    return LoginVO(token=token, expires_at=expires_at, user=UserVO(username=user.username))


@router.get("/api/v1/auth/session", response_model=SessionVO)
def current_session(auth: AuthDep) -> SessionVO:
    return SessionVO(username=auth.user.username, expires_at=auth.session.expires_at)


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
