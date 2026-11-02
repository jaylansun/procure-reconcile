from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.database import Database
from app.persistence.models import Administrator, AuthSession
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
            if not session.scalar(
                text("SELECT to_regclass('public.administrators')")
            ) or not session.scalar(text("SELECT to_regclass('public.auth_sessions')")):
                return Readiness(False, "schema_not_ready")
            if not session.get(Administrator, 1):
                return Readiness(False, "administrator_not_initialized")
            session.execute(select(AuthSession.id).limit(1))
            return Readiness(True)
    except MigrationMetadataError:
        return Readiness(False, "schema_not_ready")
    except SQLAlchemyError:
        # No URLs, credentials, or SQL details are exposed by the public handshake.
        return Readiness(False, "database_unavailable")
