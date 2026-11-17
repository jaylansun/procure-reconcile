"""验证增量迁移与ORM一致，缺少公共表时保持未就绪。"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.common.security.passwords import new_session_token
from app.config.settings import Settings
from app.persistence.database import DatabaseManager
from app.persistence.model_registry import BaseDO
from app.service.administrator_service import AdministratorService
from app.service.readiness_service import ReadinessService

pytestmark = pytest.mark.postgres


def test_migrated_schema_matches_models(postgres_database: DatabaseManager) -> None:
    """新增迁移的类型、外键、索引和可空性与实际模型保持一致。"""
    assert postgres_database.engine is not None
    with postgres_database.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, BaseDO.metadata) == []


@pytest.mark.parametrize("table", ["audit_events", "stored_files", "suppliers", "units"])
def test_missing_infrastructure_table_is_not_ready(
    postgres_database: DatabaseManager, table: str
) -> None:
    """即使版本号正确，物理公共表缺失仍应明确拒绝业务请求。"""
    AdministratorService.initialize(postgres_database, "schema-admin", new_session_token())
    settings = Settings(_env_file=None, environment="test", instance_id="schema-test")
    assert postgres_database.engine is not None
    with postgres_database.engine.begin() as connection:
        # 表名只来自固定参数集合，不使用任何请求输入拼接SQL。
        connection.execute(text(f'ALTER TABLE "{table}" RENAME TO "hidden_{table}"'))
    try:
        status = ReadinessService.check(settings, postgres_database)
        assert not status.ready
        assert status.reason == "schema_not_ready"
    finally:
        with postgres_database.engine.begin() as connection:
            connection.execute(text(f'ALTER TABLE "hidden_{table}" RENAME TO "{table}"'))
