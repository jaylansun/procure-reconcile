"""管理员登录、会话认证与退出业务。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.common.security.auth_context import AuthContext
from app.common.security.login_rate_limiter import LoginRateLimiter
from app.common.security.passwords import hasher, new_session_token, token_digest, verify_password
from app.domain.dto.login_dto import LoginDTO
from app.domain.vo.login_vo import LoginVO
from app.domain.vo.user_vo import UserVO
from app.entity.auth_session_do import AuthSessionDO
from app.repository.auth_repository import AuthRepository

# 用户名不存在时也执行一次密码校验，保持登录校验过程一致。
DUMMY_PASSWORD_HASH = hasher.hash(new_session_token())


class AuthService:
    """通过数据访问层管理管理员认证及各设备独立的登录会话。"""

    def __init__(self, repository: AuthRepository) -> None:
        self.repository = repository

    def login(
        self,
        payload: LoginDTO,
        session: Session,
        *,
        peer: str,
        limiter: LoginRateLimiter,
        session_hours: int,
    ) -> LoginVO:
        """校验账号密码并保存新会话，提交成功后返回登录结果。"""
        limiter.check(peer)
        user = self.repository.find_administrator_by_username(session, payload.username)
        matches = verify_password(
            payload.password.get_secret_value(), user.password_hash if user else DUMMY_PASSWORD_HASH
        )
        if user is None or not matches:
            limiter.failed(peer)
            raise AppError(401, "invalid_credentials", "用户名或密码错误")
        token = new_session_token()
        expires_at = datetime.now(UTC) + timedelta(hours=session_hours)
        self.repository.add_session(
            session,
            AuthSessionDO(
                administrator_id=user.id,
                token_hash=token_digest(token),
                device_name=payload.device_name,
                expires_at=expires_at,
            ),
        )
        session.commit()
        limiter.succeeded(peer)
        return LoginVO(token=token, expires_at=expires_at, user=UserVO(username=user.username))

    def authenticate(
        self,
        session: Session,
        *,
        scheme: str | None,
        token: str | None,
    ) -> AuthContext:
        """校验令牌类型、长度、到期时间及撤销状态，返回当前认证上下文。"""
        if scheme is None or scheme.lower() != "bearer" or token is None:
            raise AppError(401, "authentication_required", "请先登录")
        if not 32 <= len(token) <= 128:
            raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
        stored = self.repository.find_session_by_token_hash(session, token_digest(token))
        if (
            stored is None
            or stored.revoked_at is not None
            or stored.expires_at <= datetime.now(UTC)
        ):
            raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
        user = self.repository.get_administrator(session, stored.administrator_id)
        if user is None:
            raise AppError(401, "session_invalid", "会话无效或已过期，请重新登录")
        return AuthContext(user=user, session=stored)

    def logout(self, session: Session, auth: AuthContext) -> None:
        """撤销当前设备的登录会话并提交。"""
        auth.session.revoked_at = datetime.now(UTC)
        session.commit()
