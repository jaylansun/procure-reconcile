"""管理员登录、当前会话和退出登录的 HTTP 接口。"""

from fastapi import APIRouter, Request, Response

from app.common.dependencies import AuthDep, AuthServiceDep, SessionDep
from app.domain.dto.login_dto import LoginDTO
from app.domain.vo.login_vo import LoginVO
from app.domain.vo.session_vo import SessionVO

router = APIRouter()


@router.post("/api/v1/auth/login", response_model=LoginVO)
def login(
    payload: LoginDTO,
    request: Request,
    session: SessionDep,
    service: AuthServiceDep,
) -> LoginVO:
    """接收登录资料，调用认证服务并返回登录会话。"""
    peer = request.client.host if request.client else "unknown"
    return service.login(
        payload,
        session,
        peer=peer,
        limiter=request.app.state.login_limiter,
        session_hours=request.app.state.settings.session_hours,
    )


@router.get("/api/v1/auth/session", response_model=SessionVO)
def current_session(auth: AuthDep) -> SessionVO:
    """返回已认证用户及当前登录会话的到期时间。"""
    return SessionVO(username=auth.user.username, expires_at=auth.session.expires_at)


@router.post("/api/v1/auth/logout", status_code=204)
def logout(auth: AuthDep, session: SessionDep, service: AuthServiceDep) -> Response:
    """撤销当前会话，返回空响应。"""
    service.logout(session, auth)
    return Response(status_code=204)
