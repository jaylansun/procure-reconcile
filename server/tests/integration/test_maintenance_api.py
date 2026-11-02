"""维护窗口保持存活，拒绝认证写入与原件暂存，开放后原会话仍可用。"""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.main import create_app
from app.persistence.models import AuthSession, CommandRecord, StoredFile
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.settings import Settings

pytestmark = pytest.mark.postgres


def test_maintenance_blocks_http_writers_without_partial_files(postgres_database, tmp_path):
    """维护不接受新会话、退出或上传，已有数据库事实和原件保持完整。"""
    password = new_session_token()
    initialize_administrator(postgres_database, "maintenance-http", password)
    settings = Settings(
        _env_file=None,
        environment="test",
        instance_id="maintenance-http-test",
        database_url=SecretStr(os.environ["TEST_DATABASE_URL"]),
        files_dir=tmp_path / "files",
        maintenance_dir=tmp_path / "maintenance",
    )
    application = create_app(settings)
    with TestClient(application) as client:
        login_body = {
            "username": "maintenance-http",
            "password": password,
            "device_name": "isolated-test",
        }
        logged_in = client.post("/api/v1/auth/login", json=login_body)
        assert logged_in.status_code == 200
        headers = {"Authorization": "Bearer " + logged_in.json()["token"]}
        assert client.get("/api/v1/system").json()["ready"] is True

        gate = application.state.maintenance_gate
        gate.set_maintenance(True, release_id="http-maintenance-test")
        assert client.get("/health").json() == {"status": "alive"}
        status = client.get("/api/v1/system").json()
        assert status["ready"] is False
        assert status["reason"] == "maintenance"
        requests = [
            client.post("/api/v1/auth/login", json=login_body),
            client.get("/api/v1/auth/session", headers=headers),
            client.post("/api/v1/auth/logout", headers=headers),
            client.post(
                "/api/v1/files",
                params={"filename": "采购.pdf"},
                headers={**headers, "X-Command-ID": str(uuid4())},
                content=b"not-received-during-maintenance",
            ),
        ]
        for response in requests:
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "maintenance"
        assert not settings.files_dir.exists()
        with postgres_database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(CommandRecord)) == 0
            assert session.scalar(select(func.count()).select_from(StoredFile)) == 0
            assert session.scalar(select(func.count()).select_from(AuthSession)) == 1
            assert session.scalar(select(AuthSession.revoked_at)) is None

        with gate.exclusive():
            gate.set_maintenance(False)
        assert client.get("/api/v1/system").json()["ready"] is True
        assert client.get("/api/v1/auth/session", headers=headers).status_code == 200
