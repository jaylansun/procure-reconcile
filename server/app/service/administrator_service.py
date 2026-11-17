"""受控入口使用的唯一管理员初始化服务。"""

from sqlalchemy import text

from app.common.security.passwords import hash_password
from app.entity.administrator_do import AdministratorDO
from app.persistence.database import DatabaseManager
from app.persistence.schema import expected_schema_heads


class AdministratorService:
    """校验当前迁移版本，在事务中初始化唯一管理员。"""

    @staticmethod
    def initialize(database: DatabaseManager, username: str, password: str) -> None:
        """供命令行与测试入口显式初始化唯一管理员。"""
        username = username.strip()
        if not username or len(username) > 64 or any(ord(c) < 32 for c in username):
            raise ValueError("用户名须为1至64个字符且不含控制字符")
        if database.sessions is None:
            raise ValueError("请先配置PROCURE_DATABASE_URL")
        password_hash = hash_password(password)
        with database.sessions.begin() as session:
            # 串行执行初始化，数据库 CHECK 约束同时保证管理员唯一。
            session.execute(text("SELECT pg_advisory_xact_lock(7042935101)"))
            revisions = set(session.scalars(text("SELECT version_num FROM public.alembic_version")))
            if revisions != expected_schema_heads():
                raise ValueError("请先执行数据库迁移至当前版本")
            if session.get(AdministratorDO, 1) is not None:
                raise ValueError("管理员已初始化，不能覆盖账号或密码")
            session.add(AdministratorDO(id=1, username=username, password_hash=password_hash))
