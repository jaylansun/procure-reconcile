"""验证增量迁移与ORM一致，缺少公共表时保持未就绪。"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.persistence.database import Database
from app.persistence.models import Base
from app.readiness import check_readiness
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.settings import Settings

pytestmark = pytest.mark.postgres


def test_migrated_schema_matches_models(postgres_database: Database) -> None:
    """新增迁移的类型、外键、索引和可空性与实际模型保持一致。"""
    assert postgres_database.engine is not None
    with postgres_database.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []


@pytest.mark.parametrize("table", ["audit_events", "stored_files"])
def test_missing_infrastructure_table_is_not_ready(postgres_database: Database, table: str) -> None:
    """即使版本号正确，物理公共表缺失仍应明确拒绝业务请求。"""
    initialize_administrator(postgres_database, "schema-admin", new_session_token())
    settings = Settings(_env_file=None, environment="test", instance_id="schema-test")
    assert postgres_database.engine is not None
    with postgres_database.engine.begin() as connection:
        # 表名只来自固定参数集合，不使用任何请求输入拼接SQL。
        connection.execute(text(f'ALTER TABLE "{table}" RENAME TO "hidden_{table}"'))
    try:
        status = check_readiness(settings, postgres_database)
        assert not status.ready
        assert status.reason == "schema_not_ready"
    finally:
        with postgres_database.engine.begin() as connection:
            connection.execute(text(f'ALTER TABLE "hidden_{table}" RENAME TO "{table}"'))
