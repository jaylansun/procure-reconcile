from datetime import UTC, datetime, timedelta
from shutil import copytree

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.persistence import schema
from app.persistence.models import Administrator, AuthSession
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token, token_digest
from app.security.rate_limit import LoginRateLimiter
from app.settings import Settings

pytestmark = pytest.mark.postgres


@pytest.fixture
def api(postgres_database):
    configured = Settings(
        _env_file=None,
        environment="test",
        instance_id="authentication-test",
        database_url=postgres_database.engine.url.render_as_string(hide_password=False),
    )
    with TestClient(create_app(configured)) as client:
        yield client


@pytest.fixture(params=["042619", None], ids=["six-digit-password", "long-password"])
def administrator(postgres_database, request):
    password = request.param if request.param is not None else new_session_token()
    initialize_administrator(postgres_database, "administrator", password)
    return password


def login(api, password, device="test-device", username="administrator"):
    return api.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password, "device_name": device},
    )


def test_uninitialized_then_initialized_readiness_and_single_administrator(api, postgres_database):
    assert api.get("/health").status_code == 200
    assert api.get("/api/v1/system").json()["reason"] == "administrator_not_initialized"
    not_initialized = login(api, "irrelevant-test-input")
    assert not_initialized.status_code == 503
    initialize_administrator(postgres_database, "administrator", new_session_token())
    assert api.get("/api/v1/system").json()["ready"] is True
    with pytest.raises(ValueError, match="已初始化"):
        initialize_administrator(postgres_database, "other", new_session_token())
    with pytest.raises(IntegrityError), postgres_database.sessions.begin() as session:
        session.add(Administrator(id=2, username="other", password_hash="not-a-real-account"))


def test_login_hash_storage_multiple_devices_and_independent_logout(
    api, postgres_database, administrator
):
    assert api.get("/api/v1/workspace").status_code == 401
    assert login(api, "wrong-password").status_code == 401
    assert login(api, administrator, username="nonexistent").status_code == 401
    first_response = login(api, administrator, "Mac")
    second_response = login(api, administrator, "Windows")
    assert first_response.status_code == 200
    first, second = first_response.json(), second_response.json()
    assert first["user"] == {"username": "administrator"}
    assert "expires_at" in first
    assert first["token"] != second["token"]
    assert first_response.headers["cache-control"] == "no-store"
    with postgres_database.sessions() as session:
        stored = session.scalars(select(AuthSession)).all()
        assert len(stored) == 2
        assert {record.token_hash for record in stored} == {
            token_digest(first["token"]),
            token_digest(second["token"]),
        }
        assert {record.device_name for record in stored} == {"Mac", "Windows"}
        assert all(record.token_hash not in {first["token"], second["token"]} for record in stored)
    headers = {"Authorization": f"Bearer {first['token']}"}
    assert api.get("/api/v1/auth/session", headers=headers).json()["username"] == "administrator"
    assert api.get("/api/v1/workspace", headers=headers).json() == {
        "message": "连接与登录已就绪",
        "environment": "test",
        "instance_id": "authentication-test",
        "username": "administrator",
        "database": "connected",
    }
    assert api.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert api.get("/api/v1/auth/session", headers=headers).status_code == 401
    assert (
        api.get(
            "/api/v1/workspace", headers={"Authorization": f"Bearer {second['token']}"}
        ).status_code
        == 200
    )


def test_expired_or_forged_sessions_are_rejected(api, postgres_database, administrator):
    token = login(api, administrator).json()["token"]
    with postgres_database.sessions.begin() as session:
        stored = session.scalar(
            select(AuthSession).where(AuthSession.token_hash == token_digest(token))
        )
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        api.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    assert (
        api.get(
            "/api/v1/workspace", headers={"Authorization": f"Bearer {new_session_token()}"}
        ).status_code
        == 401
    )


def test_schema_version_mismatch_is_not_ready_and_does_not_break_liveness(api, postgres_database):
    with postgres_database.sessions.begin() as session:
        original_revisions = list(
            session.scalars(text("SELECT version_num FROM public.alembic_version"))
        )
        session.execute(text("DELETE FROM public.alembic_version"))
        session.execute(
            text("INSERT INTO public.alembic_version (version_num) VALUES ('outdated-test')")
        )
    try:
        assert api.get("/api/v1/system").json()["reason"] == "schema_not_ready"
        assert api.get("/health").status_code == 200
    finally:
        with postgres_database.sessions.begin() as session:
            session.execute(text("DELETE FROM public.alembic_version"))
            for revision in original_revisions:
                session.execute(
                    text("INSERT INTO public.alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )


def test_new_migration_head_requires_upgrade_before_readiness_and_admin_initialization(
    api, postgres_database, tmp_path, monkeypatch
):
    original_heads = schema.expected_schema_heads()
    original_config = schema.ALEMBIC_CONFIG
    copytree(original_config.parent / "migrations", tmp_path / "migrations")
    config_path = tmp_path / "alembic.ini"
    config_path.write_text("[alembic]\nscript_location = %(here)s/migrations\n")
    parent = (
        next(iter(original_heads)) if len(original_heads) == 1 else tuple(sorted(original_heads))
    )
    (tmp_path / "migrations" / "versions" / "test_next.py").write_text(
        "revision = 'test_next'\n"
        f"down_revision = {parent!r}\n"
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade():\n    pass\n"
        "def downgrade():\n    pass\n"
    )
    monkeypatch.setattr(schema, "ALEMBIC_CONFIG", config_path)
    try:
        assert schema.expected_schema_heads() == {"test_next"}
        assert api.get("/api/v1/system").json()["reason"] == "schema_not_ready"
        with pytest.raises(ValueError, match="迁移"):
            initialize_administrator(postgres_database, "administrator", "042619")

        migration = Config(str(config_path))
        migration.attributes["database_url"] = postgres_database.engine.url.render_as_string(
            hide_password=False
        )
        command.upgrade(migration, "heads")
        assert api.get("/api/v1/system").json()["reason"] == "administrator_not_initialized"
        initialize_administrator(postgres_database, "administrator", "042619")
        assert api.get("/api/v1/system").json()["ready"] is True

        monkeypatch.setattr(schema, "ALEMBIC_CONFIG", tmp_path / "missing.ini")
        assert api.get("/api/v1/system").json()["reason"] == "schema_not_ready"
        assert api.get("/health").status_code == 200
    finally:
        # The added revision is a no-op. Restore version rows so the shared,
        # isolated test database remains usable with the real migration tree.
        with postgres_database.sessions.begin() as session:
            session.execute(text("DELETE FROM public.alembic_version"))
            for revision in original_heads:
                session.execute(
                    text("INSERT INTO public.alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )


def test_rate_limit_and_validation_do_not_create_sessions(api, postgres_database, administrator):
    api.app.state.login_limiter = LoginRateLimiter(attempts=2)
    assert login(api, "wrong-password").status_code == 401
    assert login(api, "wrong-password").status_code == 401
    assert login(api, administrator).status_code == 429
    invalid = api.post(
        "/api/v1/auth/login",
        json={"username": "administrator", "password": "secret-must-not-echo", "device_name": ""},
    )
    assert invalid.status_code == 422
    assert "secret-must-not-echo" not in invalid.text
    assert set(invalid.json()["error"]) == {"code", "message"}
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0
