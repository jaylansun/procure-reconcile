from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.database import Database
from app.persistence.models import Administrator, AuthSession, CommandRecord
from app.persistence.schema import MigrationMetadataError, expected_schema_heads
from app.settings import Settings


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str | None = None


def check_readiness(settings: Settings, database: Database) -> Readiness:
    if not settings.instance_id:
        return Readiness(False, "instance_not_configured")
    if database.engine is None or database.sessions is None:
        return Readiness(False, "database_not_configured")
    try:
        with database.sessions() as session:
            session.execute(text("SELECT 1"))
            has_version = session.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if not has_version:
                return Readiness(False, "schema_not_ready")
            revisions = set(session.scalars(text("SELECT version_num FROM public.alembic_version")))
            if revisions != expected_schema_heads():
                return Readiness(False, "schema_not_ready")
            # 迁移版本与实际必需表同时校验，避免缺表时仍宣布服务就绪。
            for table_name in (
                "administrators",
                "auth_sessions",
                "command_records",
                "audit_events",
                "stored_files",
            ):
                existing_table = session.scalar(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": f"public.{table_name}"},
                )
                if existing_table is None:
                    return Readiness(False, "schema_not_ready")
            if not session.get(Administrator, 1):
                return Readiness(False, "administrator_not_initialized")
            session.execute(select(AuthSession.id).limit(1))
            session.execute(select(CommandRecord.command_id).limit(1))
            return Readiness(True)
    except MigrationMetadataError:
        return Readiness(False, "schema_not_ready")
    except SQLAlchemyError:
        # No URLs, credentials, or SQL details are exposed by the public handshake.
        return Readiness(False, "database_unavailable")
