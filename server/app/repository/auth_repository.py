"""管理员与登录会话的数据库访问。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity.administrator_do import AdministratorDO
from app.entity.auth_session_do import AuthSessionDO


class AuthRepository:
    """在调用方提供的数据库会话中查询管理员、读取和保存登录会话。"""

    def find_administrator_by_username(
        self, session: Session, username: str
    ) -> AdministratorDO | None:
        """按用户名查询管理员，供登录时校验密码。"""
        return session.scalar(
            select(AdministratorDO).where(AdministratorDO.username == username)
        )

    def get_administrator(self, session: Session, administrator_id: int) -> AdministratorDO | None:
        """按管理员标识读取当前会话所属的账号。"""
        return session.get(AdministratorDO, administrator_id)

    def find_session_by_token_hash(
        self, session: Session, token_hash: str
    ) -> AuthSessionDO | None:
        """按令牌摘要查询登录会话。"""
        return session.scalar(
            select(AuthSessionDO).where(AuthSessionDO.token_hash == token_hash)
        )

    def add_session(self, session: Session, auth_session: AuthSessionDO) -> None:
        """将新会话加入调用方事务，由认证服务完成登录提交。"""
        session.add(auth_session)
