import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.settings import Settings


def test_liveness_does_not_require_database_or_administrator():
    configured = Settings(
        _env_file=None, environment="test", instance_id="health-test", database_url=None
    )
    with TestClient(create_app(configured)) as client:
        liveness = client.get("/health")
        assert liveness.json() == {"status": "alive"}
        assert len(liveness.headers["x-request-id"]) == 32
        response = client.get("/api/v1/system")
        assert response.status_code == 200
        assert response.json() == {
            "environment": "test",
            "instance_id": "health-test",
            "app_version": "0.1.0",
            "api_version": "1",
            "ready": False,
            "reason": "database_not_configured",
        }
        protected = client.get("/api/v1/workspace")
        assert protected.status_code == 503
        assert set(protected.json()) == {"error"}
        assert set(protected.json()["error"]) == {"code", "message"}


def test_missing_instance_id_never_generates_a_temporary_identity():
    configured = Settings(_env_file=None, instance_id=None, database_url=None)
    with TestClient(create_app(configured)) as client:
        for _ in range(2):
            assert client.get("/api/v1/system").json()["reason"] == "instance_not_configured"
            assert client.get("/api/v1/system").json()["instance_id"] == "unconfigured"
        assert client.get("/health").status_code == 200


def test_database_outage_does_not_break_liveness_or_leak_connection_secrets():
    configured = Settings(
        _env_file=None,
        instance_id="offline-test",
        database_url="postgresql+psycopg://nobody:never-expose@127.0.0.1:1/procure_test",
    )
    with TestClient(create_app(configured)) as client:
        response = client.get("/api/v1/system")
        assert response.json()["reason"] == "database_unavailable"
        assert "never-expose" not in response.text
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "mysql://example/db"])
def test_only_postgresql_is_supported(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url)


def test_settings_load_server_env_file_but_environment_takes_precedence(tmp_path, monkeypatch):
    for name in ("PROCURE_INSTANCE_ID", "PROCURE_SESSION_HOURS", "PROCURE_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("PROCURE_INSTANCE_ID=stable-config\nPROCURE_SESSION_HOURS=4\n")
    assert Settings(_env_file=env).instance_id == "stable-config"
    monkeypatch.setenv("PROCURE_SESSION_HOURS", "8")
    assert Settings(_env_file=env).session_hours == 8


def test_invalid_requests_use_safe_uniform_errors():
    configured = Settings(_env_file=None, database_url=None)
    with TestClient(create_app(configured)) as client:
        response = client.get("/not-a-route")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert client.post("/api/v1/auth/register").status_code == 404
