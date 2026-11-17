"""HTTP 接口共用的数据库会话与身份认证依赖。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.common.security.auth_context import AuthContext
from app.domain.dto.actor_dto import ActorDTO
from app.repository.auth_repository import AuthRepository
from app.service.auth_service import AuthService
from app.service.readiness_service import ReadinessService

bearer = HTTPBearer(auto_error=False)


def get_auth_service() -> AuthService:
    """组装认证服务及其数据库访问层。"""
    return AuthService(AuthRepository())


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def database_session(request: Request) -> Iterator[Session]:
    """就绪后提供请求级数据库会话，并纳入维护操作的并发保护。"""
    status = ReadinessService.check(request.app.state.settings, request.app.state.database)
    if not status.ready:
        raise AppError(503, status.reason or "service_not_ready", "服务尚未就绪，请检查初始化状态")
    # 共享写锁覆盖 Session 退出，维护备份会等待所有已有请求完成。
    with request.app.state.maintenance_gate.writer():
        with request.app.state.database.sessions() as session:
            yield session


SessionDep = Annotated[Session, Depends(database_session)]


def authenticated(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    service: AuthServiceDep,
) -> AuthContext:
    """从 HTTP 认证信息中提取令牌，并交给认证服务校验。"""
    return service.authenticate(
        session,
        scheme=credentials.scheme if credentials else None,
        token=credentials.credentials if credentials else None,
    )


AuthDep = Annotated[AuthContext, Depends(authenticated)]


def current_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    service: AuthServiceDep,
) -> ActorDTO:
    """先完成鉴权并关闭只读 Session，避免上传期间长期占用数据库连接。"""
    database = request.app.state.database
    status = ReadinessService.check(request.app.state.settings, database)
    if not status.ready:
        raise AppError(503, status.reason or "service_not_ready", "服务尚未就绪")
    with request.app.state.maintenance_gate.writer():
        with database.sessions() as session:
            auth = service.authenticate(
                session,
                scheme=credentials.scheme if credentials else None,
                token=credentials.credentials if credentials else None,
            )
            return ActorDTO(id=auth.user.id, username=auth.user.username)


ActorDep = Annotated[ActorDTO, Depends(current_actor)]
