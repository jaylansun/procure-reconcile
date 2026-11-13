"""通过专用PG测试库验证供应商迁移的升级、回退及原有资料保留。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text

from app.persistence.database import DatabaseManager
from app.persistence.models import (
    AdministratorDO,
    AuditEventDO,
    AuthSessionDO,
    CommandRecordDO,
    StoredFileDO,
    SupplierDO,
)
from app.persistence.schema import expected_schema_heads

pytestmark = pytest.mark.postgres


def test_supplier_migration_round_trip_preserves_existing_records(
    postgres_database: DatabaseManager,
) -> None:
    """验证回退范围及再次升级，并在断言失败后恢复当前迁移版本。"""
    engine = postgres_database.engine
    sessions = postgres_database.sessions
    assert engine is not None and sessions is not None
    legacy_models = (
        AdministratorDO,
        AuthSessionDO,
        CommandRecordDO,
        AuditEventDO,
        StoredFileDO,
    )
    command_id = uuid4()
    with sessions.begin() as session:
        session.add(
            AdministratorDO(id=1, username="migration-admin", password_hash="synthetic-test-hash")
        )
        session.flush()
        session.add_all(
            [
                AuthSessionDO(
                    administrator_id=1,
                    token_hash="a" * 64,
                    device_name="supplier-migration-test",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                CommandRecordDO(
                    command_id=command_id,
                    operation="migration.verify",
                    request_fingerprint="b" * 64,
                    response_status=200,
                    response_body={"message": "原有命令记录"},
                ),
                AuditEventDO(
                    command_id=command_id,
                    actor_id=1,
                    action="migration.verify",
                    target_type="command",
                    target_id=str(command_id),
                    details={"message": "原有审计记录"},
                ),
                StoredFileDO(
                    sha256="c" * 64,
                    size_bytes=42,
                    original_name="迁移验收.pdf",
                    media_type="application/pdf",
                    created_by_id=1,
                ),
                SupplierDO(name="回退范围验收供应商"),
            ]
        )

    with engine.connect() as connection:
        previous = {
            model.__tablename__: connection.execute(select(model.__table__)).mappings().all()
            for model in legacy_models
        }
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    # 连接由受保护fixture选定，沿用专用测试库进行迁移验证。
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    try:
        command.downgrade(config, "0003_infrastructure")
        inspector = inspect(engine)
        assert not inspector.has_table("suppliers", schema="public")
        with engine.connect() as connection:
            assert set(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            ) == {"0003_infrastructure"}
            for model in legacy_models:
                assert inspector.has_table(model.__tablename__, schema="public")
                assert (
                    connection.execute(select(model.__table__)).mappings().all()
                    == previous[model.__tablename__]
                )
        command.upgrade(config, "head")
        assert inspect(engine).has_table("suppliers", schema="public")
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(SupplierDO)) == 0
            assert set(session.scalars(text("SELECT version_num FROM public.alembic_version"))) == (
                expected_schema_heads()
            )
            for model in legacy_models:
                assert (
                    session.execute(select(model.__table__)).mappings().all()
                    == previous[model.__tablename__]
                )
    finally:
        command.upgrade(config, "head")
