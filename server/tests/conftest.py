import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app.entity.administrator_do import AdministratorDO
from app.entity.audit_event_do import AuditEventDO
from app.entity.auth_session_do import AuthSessionDO
from app.entity.command_record_do import CommandRecordDO
from app.entity.stored_file_do import StoredFileDO
from app.entity.supplier_do import SupplierDO
from app.entity.unit_do import UnitDO
from app.persistence.database import DatabaseManager, normalize_database_url

SERVER_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def postgres_database():
    """Destructive auth test fixture: never infer a URL from application settings.

    Only explicitly selected dedicated test databases are accepted. This fixture
    migrates and clears auth/infrastructure tables, never creates/drops a database.
    Do not run simultaneous test jobs against the same database.
    """
    supplied = os.environ.get("TEST_DATABASE_URL")
    if not supplied:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    url = make_url(normalize_database_url(supplied))
    if not url.database or not re.search(r"(^test$|^test_|_test$)", url.database):
        pytest.fail("TEST_DATABASE_URL must name a dedicated test/test_*/ *_test database")
    migration = Config(str(SERVER_ROOT / "alembic.ini"))
    migration.attributes["database_url"] = supplied
    command.upgrade(migration, "head")
    database = DatabaseManager(supplied)
    with database.sessions.begin() as session:
        session.execute(delete(UnitDO))
        session.execute(delete(SupplierDO))
        session.execute(delete(AuditEventDO))
        session.execute(delete(CommandRecordDO))
        session.execute(delete(StoredFileDO))
        session.execute(delete(AuthSessionDO))
        session.execute(delete(AdministratorDO))
    try:
        yield database
    finally:
        with database.sessions.begin() as session:
            session.execute(delete(UnitDO))
            session.execute(delete(SupplierDO))
            session.execute(delete(AuditEventDO))
            session.execute(delete(CommandRecordDO))
            session.execute(delete(StoredFileDO))
            session.execute(delete(AuthSessionDO))
            session.execute(delete(AdministratorDO))
        database.close()
