from sqlalchemy import text

from app.persistence.database import Database
from app.persistence.models import Administrator
from app.persistence.schema import expected_schema_heads
from app.security.passwords import hash_password


def initialize_administrator(database: Database, username: str, password: str) -> None:
    """Controlled CLI/test entry only. No public registration or reset route."""
    username = username.strip()
    if not username or len(username) > 64 or any(ord(c) < 32 for c in username):
        raise ValueError("用户名须为1至64个字符且不含控制字符")
    if database.sessions is None:
        raise ValueError("请先配置PROCURE_DATABASE_URL")
    password_hash = hash_password(password)
    with database.sessions.begin() as session:
        # Serialize bootstrap attempts; the singleton CHECK also enforces this in PostgreSQL.
        session.execute(text("SELECT pg_advisory_xact_lock(7042935101)"))
        revisions = set(session.scalars(text("SELECT version_num FROM public.alembic_version")))
        if revisions != expected_schema_heads():
            raise ValueError("请先执行数据库迁移至当前版本")
        if session.get(Administrator, 1) is not None:
            raise ValueError("管理员已初始化，不能覆盖账号或密码")
        session.add(Administrator(id=1, username=username, password_hash=password_hash))
