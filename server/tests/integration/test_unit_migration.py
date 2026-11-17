"""在独立 PostgreSQL 验证单位增量迁移与既有资料保留。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError

from app.entity.administrator_do import AdministratorDO
from app.entity.audit_event_do import AuditEventDO
from app.entity.auth_session_do import AuthSessionDO
from app.entity.command_record_do import CommandRecordDO
from app.entity.stored_file_do import StoredFileDO
from app.entity.supplier_do import SupplierDO
from app.entity.unit_do import UnitDO
from app.persistence.database import DatabaseManager
from app.persistence.schema import expected_schema_heads

pytestmark = pytest.mark.postgres


def test_unit_migration_round_trip_preserves_existing_records(
    postgres_database: DatabaseManager,
) -> None:
    """回退单位迁移时保留原六张表，升级后重新获得完整当前结构。"""
    engine = postgres_database.engine
    sessions = postgres_database.sessions
    assert engine is not None and sessions is not None
    legacy_models = (
        AdministratorDO,
        AuthSessionDO,
        CommandRecordDO,
        AuditEventDO,
        StoredFileDO,
        SupplierDO,
    )
    command_id = uuid4()
    with sessions.begin() as session:
        session.add(
            AdministratorDO(id=1, username="unit-migration-admin", password_hash="synthetic-hash")
        )
        session.flush()
        session.add_all(
            [
                AuthSessionDO(
                    administrator_id=1,
                    token_hash="d" * 64,
                    device_name="unit-migration-test",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                CommandRecordDO(
                    command_id=command_id,
                    operation="migration.unit.verify",
                    request_fingerprint="e" * 64,
                    response_status=200,
                    response_body={"name": "原有成功结果"},
                ),
                AuditEventDO(
                    command_id=command_id,
                    actor_id=1,
                    action="migration.unit.verify",
                    target_type="command",
                    target_id=str(command_id),
                    details={"name": "原有审计记录"},
                ),
                StoredFileDO(
                    sha256="f" * 64,
                    size_bytes=42,
                    original_name="单位迁移验收.pdf",
                    media_type="application/pdf",
                    created_by_id=1,
                ),
                SupplierDO(name="单位迁移前供应商", contact_info="原有联系信息"),
                UnitDO(name="待回退单位"),
            ]
        )

    with engine.connect() as connection:
        previous = {
            model.__tablename__: connection.execute(select(model.__table__)).mappings().all()
            for model in legacy_models
        }
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    # 沿用受保护 fixture 选定的专用测试库，不读取开发或部署配置。
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    try:
        command.downgrade(config, "0004_suppliers")
        inspector = inspect(engine)
        assert not inspector.has_table("units", schema="public")
        with engine.connect() as connection:
            assert set(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            ) == {"0004_suppliers"}
            for model in legacy_models:
                assert inspector.has_table(model.__tablename__, schema="public")
                assert (
                    connection.execute(select(model.__table__)).mappings().all()
                    == previous[model.__tablename__]
                )
        command.upgrade(config, "head")
        assert inspect(engine).has_table("units", schema="public")
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(UnitDO)) == 0
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


def test_unit_database_defaults_include_version_and_timezone(postgres_database):
    """直接 SQL 插入时，版本和时间也由数据库提供默认值。"""
    with postgres_database.engine.begin() as connection:
        row = connection.execute(
            text(
                "INSERT INTO units (id, name) VALUES (:id, :name) "
                "RETURNING version, created_at, updated_at"
            ),
            {"id": uuid4(), "name": "件"},
        ).one()
        assert row.version == 1
        assert row.created_at.utcoffset() is not None
        assert row.updated_at == row.created_at


@pytest.mark.parametrize(
    ("values", "sqlstate"),
    [
        ({"name": " \t\n"}, "23514"),
        ({"name": "件", "version": 0}, "23514"),
        ({"name": "件" * 51}, "22001"),
    ],
    ids=["blank-name", "nonpositive-version", "long-name"],
)
def test_unit_database_constraints_reject_invalid_values(postgres_database, values, sqlstate):
    """绕过 DTO 写入时，数据库仍校验空白名称、版本及长度。"""
    with pytest.raises((IntegrityError, DataError)) as error:
        with postgres_database.sessions.begin() as session:
            session.add(UnitDO(**values))
            session.flush()
    assert error.value.orig.sqlstate == sqlstate
