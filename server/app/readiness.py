from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.database import DatabaseManager
from app.persistence.models import AdministratorDO, AuthSessionDO, CommandRecordDO, SupplierDO
from app.persistence.schema import MigrationMetadataError, expected_schema_heads
from app.settings import Settings


@dataclass(frozen=True)
class ReadinessDTO:
    """服务就绪检查的内部传输对象，携带状态和原因。"""

    ready: bool
    reason: str | None = None


def check_readiness(settings: Settings, database: DatabaseManager) -> ReadinessDTO:
    if not settings.instance_id:
        return ReadinessDTO(False, "instance_not_configured")
    if database.engine is None or database.sessions is None:
        return ReadinessDTO(False, "database_not_configured")
    try:
        with database.sessions() as session:
            session.execute(text("SELECT 1"))
            has_version = session.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if not has_version:
                return ReadinessDTO(False, "schema_not_ready")
            revisions = set(session.scalars(text("SELECT version_num FROM public.alembic_version")))
            if revisions != expected_schema_heads():
                return ReadinessDTO(False, "schema_not_ready")
            # 迁移版本与实际必需表同时校验，避免缺表时仍宣布服务就绪。
            for table_name in (
                "administrators",
                "auth_sessions",
                "command_records",
                "audit_events",
                "stored_files",
                "suppliers",
            ):
                existing_table = session.scalar(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": f"public.{table_name}"},
                )
                if existing_table is None:
                    return ReadinessDTO(False, "schema_not_ready")
            if not session.get(AdministratorDO, 1):
                return ReadinessDTO(False, "administrator_not_initialized")
            session.execute(select(AuthSessionDO.id).limit(1))
            session.execute(select(CommandRecordDO.command_id).limit(1))
            session.execute(select(SupplierDO.id).limit(1))
            return ReadinessDTO(True)
    except MigrationMetadataError:
        return ReadinessDTO(False, "schema_not_ready")
    except SQLAlchemyError:
        # No URLs, credentials, or SQL details are exposed by the public handshake.
        return ReadinessDTO(False, "database_unavailable")
