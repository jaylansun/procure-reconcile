"""当前请求使用的数据库认证上下文。"""

from dataclasses import dataclass

from app.entity.administrator_do import AdministratorDO
from app.entity.auth_session_do import AuthSessionDO


@dataclass
class AuthContext:
    """当前请求的认证上下文，关联管理员记录和登录会话。"""

    user: AdministratorDO
    session: AuthSessionDO
