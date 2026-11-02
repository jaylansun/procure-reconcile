"""验证原件HTTP协议、鉴权与公开标识，只连接专用PG测试库。"""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.documents.storage import FileStore
from app.main import create_app
from app.persistence.database import Database
from app.persistence.models import AuditEvent, CommandRecord, StoredFile
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.settings import Settings

pytestmark = pytest.mark.postgres
_LIMIT = 1024


@pytest.fixture
def file_api(postgres_database: Database, tmp_path: Path) -> Iterator[TestClient]:
    """本次API使用隔离数据库、附件目录和临时管理员口令。"""
    password = new_session_token()
    initialize_administrator(postgres_database, "file-admin", password)
    configured = Settings(
        _env_file=None,
        environment="test",
        instance_id="file-api-test",
        database_url=postgres_database.engine.url.render_as_string(hide_password=False),
        files_dir=tmp_path / "files",
        max_file_bytes=_LIMIT,
    )
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "file-admin", "password": password, "device_name": "file-api-test"},
        )
        assert response.status_code == 200
        client.headers["Authorization"] = f"Bearer {response.json()['token']}"
        yield client


def post_file(
    client: TestClient,
    *,
    content: bytes = b"raw original",
    filename: str = "采购凭证.pdf",
    command_id: UUID | None = None,
    headers: dict[str, str] | None = None,
):
    """按真实raw上传协议发送，不依赖multipart或客户端物理路径。"""
    return client.post(
        "/api/v1/files",
        params={"filename": filename},
        headers={"X-Command-ID": str(command_id or uuid4()), **(headers or {})},
        content=content,
    )


def assert_no_upload_records(database: Database) -> None:
    """无效上传不能留下引用、成功命令或文件审计。"""
    with database.sessions() as session:
        for model in (StoredFile, CommandRecord, AuditEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def assert_empty_staging(client: TestClient) -> None:
    """请求结束后上传暂存应已清理，正式对象仍交由PG引用保护。"""
    staging = client.app.state.settings.files_dir / ".staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_raw_upload_metadata_and_chinese_named_download(file_api: TestClient) -> None:
    """字节原样保存，服务器生成媒体类型，中文名称通过RFC5987下载头传递。"""
    content, filename = b"%PDF-1.7\nraw binary\x00content", "中文 采购凭证.pdf"
    response = post_file(
        file_api,
        content=content,
        filename=filename,
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 201
    assert response.headers["x-command-replayed"] == "false"
    assert response.headers["cache-control"] == "no-store"
    metadata = response.json()
    UUID(metadata["file_id"])
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert metadata["size_bytes"] == len(content)
    assert metadata["original_name"] == filename
    assert metadata["media_type"] == "application/pdf"
    assert "path" not in metadata
    file_id = metadata["file_id"]

    read = file_api.get(f"/api/v1/files/{file_id}")
    assert read.status_code == 200 and read.json() == metadata
    download = file_api.get(f"/api/v1/files/{file_id}/content")
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-length"] == str(len(content))
    assert download.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''" + quote(filename, safe="")
    )
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert_empty_staging(file_api)


def test_chunked_raw_body_does_not_require_content_length(file_api: TestClient) -> None:
    """没有Content-Length的分块原件也按实际字节数成功保存。"""
    response = file_api.post(
        "/api/v1/files",
        params={"filename": "chunked.png"},
        headers={"X-Command-ID": str(uuid4()), "Content-Type": "application/octet-stream"},
        content=iter([b"first", b"second"]),
    )
    assert response.status_code == 201
    downloaded = file_api.get(f"/api/v1/files/{response.json()['file_id']}/content")
    assert downloaded.content == b"firstsecond"
    assert downloaded.headers["content-type"] == "image/png"
    assert_empty_staging(file_api)


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic abc"])
def test_all_file_routes_require_authentication(
    file_api: TestClient, postgres_database: Database, authorization: str | None
) -> None:
    """上传、元数据和下载都不能靠公开文件ID绕过管理员会话。"""
    file_api.headers.pop("Authorization")
    headers = {} if authorization is None else {"Authorization": authorization}
    headers["X-Command-ID"] = str(uuid4())
    file_id = uuid4()
    responses = [
        file_api.post(
            "/api/v1/files", params={"filename": "private.pdf"}, headers=headers, content=b"private"
        ),
        file_api.get(f"/api/v1/files/{file_id}", headers=headers),
        file_api.get(f"/api/v1/files/{file_id}/content", headers=headers),
    ]
    assert all(response.status_code == 401 for response in responses)
    assert all(set(response.json()) == {"error"} for response in responses)
    assert_no_upload_records(postgres_database)


def test_logout_revokes_file_access(file_api: TestClient) -> None:
    """同一会话退出后，已经知道文件ID仍不能读取文件或元数据。"""
    uploaded = post_file(file_api)
    file_id = uploaded.json()["file_id"]
    assert file_api.post("/api/v1/auth/logout").status_code == 204
    assert file_api.get(f"/api/v1/files/{file_id}").status_code == 401
    assert file_api.get(f"/api/v1/files/{file_id}/content").status_code == 401


def test_dedup_replay_and_conflict_preserve_first_result(
    file_api: TestClient, postgres_database: Database
) -> None:
    """同内容复用首个ID，命令重放仍为首次201，改名或改内容冲突409。"""
    command_id = uuid4()
    first = post_file(file_api, command_id=command_id, filename="first.pdf")
    assert first.status_code == 201
    reused = post_file(file_api, filename="second.jpg")
    assert reused.status_code == 200 and reused.json() == first.json()
    assert reused.headers["x-command-replayed"] == "false"
    replay = post_file(file_api, command_id=command_id, filename="first.pdf")
    assert replay.status_code == 201 and replay.json() == first.json()
    assert replay.headers["x-command-replayed"] == "true"
    conflicts = [
        post_file(file_api, command_id=command_id, filename="changed.pdf"),
        post_file(file_api, command_id=command_id, filename="first.pdf", content=b"changed"),
    ]
    assert all(response.status_code == 409 for response in conflicts)
    assert all(response.json()["error"]["code"] == "command_conflict" for response in conflicts)
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(StoredFile)) == 1
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 2
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4
    assert_empty_staging(file_api)


@pytest.mark.parametrize(
    ("filename", "content", "expected_code"),
    [
        ("empty.pdf", b"", "empty_file"),
        ("../outside.pdf", b"x", "invalid_filename"),
        ("/tmp/outside.pdf", b"x", "invalid_filename"),
        ("dir\\outside.pdf", b"x", "invalid_filename"),
        ("line\nbreak.pdf", b"x", "invalid_filename"),
        ("unsupported.txt", b"x", "unsupported_file_type"),
    ],
)
def test_invalid_upload_is_safe_and_leaves_no_partial_records(
    file_api: TestClient,
    postgres_database: Database,
    filename: str,
    content: bytes,
    expected_code: str,
) -> None:
    """坏名、未支持后缀与空文件统一拒绝，不留下原件和成功记录。"""
    response = post_file(file_api, filename=filename, content=content)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code
    assert set(response.json()["error"]) == {"code", "message"}
    assert str(file_api.app.state.settings.files_dir) not in response.text
    assert_no_upload_records(postgres_database)
    assert_empty_staging(file_api)


@pytest.mark.parametrize("missing", ["command", "filename", "invalid_command", "long_filename"])
def test_required_headers_and_filename_query_are_validated(
    file_api: TestClient, postgres_database: Database, missing: str
) -> None:
    """命令头和文件名参数不可缺失，也不能静默接受无效UUID或超长名称。"""
    headers = {} if missing == "command" else {"X-Command-ID": str(uuid4())}
    if missing == "invalid_command":
        headers["X-Command-ID"] = "secret-invalid-command"
    params = {} if missing == "filename" else {"filename": "valid.pdf"}
    if missing == "long_filename":
        params["filename"] = "x" * 252 + ".pdf"
    response = file_api.post("/api/v1/files", params=params, headers=headers, content=b"file")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "secret-invalid-command" not in response.text
    assert_no_upload_records(postgres_database)


@pytest.mark.parametrize("length", ["-1", "invalid", "1.5"])
def test_invalid_content_length_is_rejected(
    file_api: TestClient, postgres_database: Database, length: str
) -> None:
    """无法解释为非负整数的长度头不能进入暂存或SQL写入。"""
    response = post_file(file_api, headers={"Content-Length": length})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_content_length"
    assert_no_upload_records(postgres_database)
    assert_empty_staging(file_api)


@pytest.mark.parametrize("mode", ["declared", "actual", "chunked"])
def test_configured_size_limit_applies_to_headers_and_real_stream(
    file_api: TestClient, postgres_database: Database, mode: str
) -> None:
    """提前声明或实际接收超限都返回413，不能通过少报长度或分块规避。"""
    if mode == "declared":
        response = post_file(file_api, headers={"Content-Length": str(_LIMIT + 1)})
    elif mode == "actual":
        response = post_file(file_api, content=b"x" * (_LIMIT + 1), headers={"Content-Length": "1"})
    else:
        response = file_api.post(
            "/api/v1/files",
            params={"filename": "large.pdf"},
            headers={"X-Command-ID": str(uuid4())},
            content=iter([b"x" * _LIMIT, b"x"]),
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert_no_upload_records(postgres_database)
    assert_empty_staging(file_api)


def test_exact_size_limit_is_allowed(file_api: TestClient) -> None:
    """技术默认限额的边界是可导入的字节数，不多拒绝一个字节。"""
    response = post_file(file_api, content=b"x" * _LIMIT)
    assert response.status_code == 201
    assert response.json()["size_bytes"] == _LIMIT
    assert_empty_staging(file_api)


@pytest.mark.parametrize("suffix", ["", "/content"])
def test_missing_file_id_is_404(file_api: TestClient, suffix: str) -> None:
    """已鉴权但不存在的文件ID返回404，响应不包含物理路径。"""
    response = file_api.get(f"/api/v1/files/{uuid4()}{suffix}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


@pytest.mark.parametrize("suffix", ["", "/content"])
def test_invalid_file_id_is_422(file_api: TestClient, suffix: str) -> None:
    """下载与元数据入口都严格验证UUID，不将任意路径传给存储。"""
    response = file_api.get(f"/api/v1/files/not-a-uuid{suffix}")
    assert response.status_code == 422


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_referenced_object_failure_returns_503_without_destroying_metadata(
    file_api: TestClient, postgres_database: Database, damage: str
) -> None:
    """引用尚在而物理对象缺失或损坏时明确不可用，不伪装404或重复导入成功。"""
    uploaded = post_file(file_api)
    metadata = uploaded.json()
    store = FileStore(file_api.app.state.settings.files_dir)
    if damage == "missing":
        store.remove(metadata["sha256"])
    else:
        path = store.root / "objects" / metadata["sha256"]
        path.chmod(0o600)
        path.write_bytes(b"damaged")
    read = file_api.get(f"/api/v1/files/{metadata['file_id']}")
    assert read.status_code == 200 and read.json() == metadata
    content = file_api.get(f"/api/v1/files/{metadata['file_id']}/content")
    duplicate = post_file(file_api)
    for response in (content, duplicate):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "file_unavailable"
        assert str(store.root) not in response.text
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(StoredFile)) == 1
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 1
    assert_empty_staging(file_api)
