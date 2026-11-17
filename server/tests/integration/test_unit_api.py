"""使用独立 PostgreSQL 验证单位接口及其事务、版本和审计。"""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.common.commands.command_context import CommandContext
from app.common.commands.locks import lock_sources
from app.common.errors import AppError
from app.common.security.passwords import new_session_token
from app.config.settings import Settings
from app.domain.dto.source_key_dto import SourceKeyDTO
from app.entity.audit_event_do import AuditEventDO
from app.entity.command_record_do import CommandRecordDO
from app.entity.unit_do import UnitDO
from app.main import create_app
from app.persistence.database import DatabaseManager
from app.service.administrator_service import AdministratorService

pytestmark = pytest.mark.postgres
_BASE = "/api/v1/units"


@pytest.fixture
def unit_api(postgres_database: DatabaseManager, tmp_path: Path) -> Iterator[TestClient]:
    """创建独立管理员及维护目录，使用真实登录会话调用单位接口。"""
    password = new_session_token()
    AdministratorService.initialize(postgres_database, "unit-test-admin", password)
    settings = Settings(
        _env_file=None,
        environment="test",
        instance_id="unit-api-test",
        database_url=postgres_database.engine.url.render_as_string(hide_password=False),
        files_dir=tmp_path / "files",
        maintenance_dir=tmp_path / "maintenance",
        command_lock_timeout_ms=100,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": "unit-test-admin",
                "password": password,
                "device_name": "unit-api-test",
            },
        )
        assert response.status_code == 200
        client.headers["Authorization"] = "Bearer " + response.json()["token"]
        yield client


def create_body(name: str = "个") -> dict:
    """为独立新增操作生成命令，重试时复用整份请求。"""
    return {"command_id": str(uuid4()), "data": {"name": name}}


def update_body(unit: dict, *, name: str = "件") -> dict:
    """修改请求携带页面已读取的稳定 ID 和版本。"""
    return {
        "command_id": str(uuid4()),
        "unit_id": unit["id"],
        "expected_version": unit["version"],
        "data": {"name": name},
    }


def counts(database: DatabaseManager) -> tuple[int, int, int]:
    """检查单位、成功命令和审计事件是否一起保存。"""
    with database.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (UnitDO, CommandRecordDO, AuditEventDO)
        )


def test_create_detail_update_and_audit_snapshots(unit_api, postgres_database):
    """真实增改读取使用同一 JSON 表示，审计保存修改前后的完整快照。"""
    creation = create_body("  个  ")
    response = unit_api.post(_BASE, json=creation)
    assert response.status_code == 201
    assert response.headers["x-command-replayed"] == "false"
    assert response.headers["cache-control"] == "no-store"
    first = response.json()
    assert set(first) == {"id", "name", "version", "created_at", "updated_at"}
    UUID(first["id"])
    assert first["name"] == "个" and first["version"] == 1
    for field in ("created_at", "updated_at"):
        assert datetime.fromisoformat(first[field]).utcoffset() is not None
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first

    modification = update_body(first, name="  件  ")
    response = unit_api.put(f"{_BASE}/{first['id']}", json=modification)
    assert response.status_code == 200
    changed = response.json()
    assert changed["id"] == first["id"]
    assert changed["name"] == "件" and changed["version"] == 2
    assert changed["created_at"] == first["created_at"]
    assert datetime.fromisoformat(changed["updated_at"]) >= datetime.fromisoformat(
        first["updated_at"]
    )
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == changed
    assert counts(postgres_database) == (1, 2, 4)

    with postgres_database.sessions() as session:
        events = {
            event.action: event
            for event in session.scalars(
                select(AuditEventDO).where(AuditEventDO.target_type == "unit")
            )
        }
        created_event = events["units.create"]
        assert created_event.command_id == UUID(creation["command_id"])
        assert created_event.target_id == first["id"]
        assert created_event.before_version is None and created_event.after_version == 1
        assert created_event.details == {"after": first}
        updated_event = events["units.update"]
        assert updated_event.command_id == UUID(modification["command_id"])
        assert updated_event.target_id == first["id"]
        assert (updated_event.before_version, updated_event.after_version) == (1, 2)
        assert updated_event.details == {"before": first, "after": changed}
        stored = session.get(CommandRecordDO, UUID(modification["command_id"]))
        assert stored.operation == "units.update"
        assert stored.response_status == 200 and stored.response_body == changed


def test_independent_same_name_units_keep_distinct_ids(unit_api, postgres_database):
    """单位名称相同的两笔新增仍有各自稳定标识。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    second = unit_api.post(_BASE, json=create_body()).json()
    assert first["id"] != second["id"]
    assert first["name"] == second["name"] == "个"
    assert counts(postgres_database) == (2, 2, 4)


def test_create_replay_keeps_original_snapshot_after_later_updates(unit_api, postgres_database):
    """已修改的单位重放新增命令时仍返回首次成功快照。"""
    body = create_body()
    first = unit_api.post(_BASE, json=body).json()
    changed = unit_api.put(f"{_BASE}/{first['id']}", json=update_body(first)).json()
    replay = unit_api.post(_BASE, json=body)
    assert replay.status_code == 201 and replay.json() == first
    assert replay.headers["x-command-replayed"] == "true"
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == changed
    assert counts(postgres_database) == (1, 2, 4)


def test_command_id_rejects_changed_payload_and_other_operations(unit_api, postgres_database):
    """同一命令标识不能换单位资料、改为修改或用于供应商新增。"""
    body = create_body()
    first = unit_api.post(_BASE, json=body).json()
    modification = {**update_body(first), "command_id": body["command_id"]}
    responses = [
        unit_api.post(_BASE, json={**body, "data": {"name": "不同名称"}}),
        unit_api.put(f"{_BASE}/{first['id']}", json=modification),
        unit_api.post("/api/v1/suppliers", json=body),
    ]
    assert [response.status_code for response in responses] == [409] * 3
    assert all(response.json()["error"]["code"] == "command_conflict" for response in responses)
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first
    assert counts(postgres_database) == (1, 1, 2)


def test_update_replay_stale_version_and_retry_keep_consistent_records(unit_api, postgres_database):
    """修改可重放，旧版本失败不留下成功记录，刷新后可复用失败命令重试。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    body = update_body(first)
    response = unit_api.put(f"{_BASE}/{first['id']}", json=body)
    assert response.status_code == 200
    changed = response.json()
    replay = unit_api.put(f"{_BASE}/{first['id']}", json=body)
    assert replay.status_code == 200 and replay.json() == changed
    assert replay.headers["x-command-replayed"] == "true"

    stale = update_body(first, name="箱")
    rejected = unit_api.put(f"{_BASE}/{first['id']}", json=stale)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "version_conflict"
    assert counts(postgres_database) == (1, 2, 4)
    with postgres_database.sessions() as session:
        assert session.get(CommandRecordDO, UUID(stale["command_id"])) is None
    stale["expected_version"] = changed["version"]
    retried = unit_api.put(f"{_BASE}/{first['id']}", json=stale)
    assert retried.status_code == 200 and retried.json()["version"] == 3
    assert unit_api.put(f"{_BASE}/{first['id']}", json=body).json() == changed
    assert counts(postgres_database) == (1, 3, 6)


def test_missing_unit_and_mismatched_path_fail_before_success_records(unit_api, postgres_database):
    """缺失单位、无效 UUID 及路径与命令标识不一致均有明确错误。"""
    identifier = str(uuid4())
    body = update_body({"id": identifier, "version": 1})
    for response in (
        unit_api.get(f"{_BASE}/{identifier}"),
        unit_api.put(f"{_BASE}/{identifier}", json=body),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "unit_not_found"
    mismatch = unit_api.put(f"{_BASE}/{uuid4()}", json=body)
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "unit_id_mismatch"
    assert unit_api.get(f"{_BASE}/invalid-uuid").status_code == 422
    assert counts(postgres_database) == (0, 0, 0)


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic abc"])
def test_all_unit_routes_require_authentication(unit_api, postgres_database, authorization):
    """所有单位读写入口共用管理员会话认证。"""
    unit_api.headers.pop("Authorization")
    headers = {} if authorization is None else {"Authorization": authorization}
    identifier = str(uuid4())
    responses = [
        unit_api.post(_BASE, json=create_body(), headers=headers),
        unit_api.put(
            f"{_BASE}/{identifier}",
            json=update_body({"id": identifier, "version": 1}),
            headers=headers,
        ),
        unit_api.get(_BASE, headers=headers),
        unit_api.get(f"{_BASE}/{identifier}", headers=headers),
    ]
    assert [response.status_code for response in responses] == [401] * 4
    assert counts(postgres_database) == (0, 0, 0)


def test_logout_revokes_unit_access(unit_api):
    """退出后的旧会话不能继续查询或新增单位。"""
    assert unit_api.post("/api/v1/auth/logout").status_code == 204
    assert unit_api.get(_BASE).status_code == 401
    assert unit_api.post(_BASE, json=create_body()).status_code == 401


def test_invalid_unit_inputs_are_rejected_before_writes(unit_api, postgres_database):
    """覆盖单位专有长度边界、空白、非法 Unicode 和额外字段。"""
    cases = [
        {},
        {"command_id": str(uuid4())},
        {"command_id": "invalid-uuid", "data": {"name": "个"}},
        {**create_body(), "unexpected": True},
        {"command_id": str(uuid4()), "data": {}},
        {"command_id": str(uuid4()), "data": {"name": "个", "contact_info": "多余字段"}},
    ]
    for value in (None, "", " \t\n", 123, "件" * 51, "空\0字符", "\ud800"):
        cases.append({"command_id": str(uuid4()), "data": {"name": value}})
    for body in cases:
        # 转义无效 Unicode，使请求进入服务端校验。
        response = unit_api.post(
            _BASE,
            content=json.dumps(body, ensure_ascii=True).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    assert counts(postgres_database) == (0, 0, 0)


def test_maximum_name_length_round_trips_through_postgresql(unit_api):
    """允许五十个中文字符，并在修改时继续应用名称校验。"""
    response = unit_api.post(_BASE, json=create_body("件" * 50))
    assert response.status_code == 201
    first = response.json()
    assert len(first["name"]) == 50
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first
    body = update_body(first, name="箱" * 51)
    assert unit_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first


def test_invalid_update_envelopes_preserve_existing_unit(unit_api, postgres_database):
    """修改需要完整命令，版本严格为非负整数。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    for version in (-1, True, "1", 1.0, None):
        body = {**update_body(first), "expected_version": version}
        assert unit_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    for field in ("command_id", "unit_id", "expected_version", "data"):
        body = update_body(first)
        del body[field]
        assert unit_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first
    assert counts(postgres_database) == (1, 1, 2)


@pytest.mark.parametrize(
    "parameters",
    [
        {"offset": -1},
        {"offset": 2**63},
        {"offset": "1.5"},
        {"offset": "invalid"},
        {"limit": 0},
        {"limit": 101},
        {"limit": "true"},
        {"unknown": "extra"},
    ],
)
def test_invalid_unit_pagination_is_rejected(unit_api, parameters):
    """查询模型验证边界，额外查询字段同样被拒绝。"""
    response = unit_api.get(_BASE, params=parameters)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unit_list_returns_arrays_with_stable_order_and_empty_pages(unit_api, postgres_database):
    """回归集合推导式错误，空页和非空页均返回 JSON 数组。"""
    assert unit_api.get(_BASE).json() == {"items": [], "offset": 0, "limit": 20}
    units = [unit_api.post(_BASE, json=create_body(name)).json() for name in ("个", "件", "箱")]
    with postgres_database.sessions.begin() as session:
        session.execute(update(UnitDO).values(created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    expected = sorted((unit["id"] for unit in units), key=lambda item: UUID(item).int, reverse=True)
    complete = unit_api.get(_BASE, params={"limit": 100}).json()
    assert [unit["id"] for unit in complete["items"]] == expected
    page = unit_api.get(_BASE, params={"offset": 1, "limit": 1}).json()
    assert [unit["id"] for unit in page["items"]] == expected[1:2]
    assert page["offset"] == 1 and page["limit"] == 1
    for offset in (3, 2**63 - 1):
        assert unit_api.get(_BASE, params={"offset": offset, "limit": 100}).json() == {
            "items": [],
            "offset": offset,
            "limit": 100,
        }


def test_maximum_version_rejects_update_without_partial_changes(unit_api, postgres_database):
    """版本达到 BIGINT 上限后，名称、成功命令和审计保持一致。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    with postgres_database.sessions.begin() as session:
        session.execute(update(UnitDO).values(version=2**63 - 1))
    body = update_body({**first, "version": 2**63 - 1})
    response = unit_api.put(f"{_BASE}/{first['id']}", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unit_version_exhausted"
    saved = unit_api.get(f"{_BASE}/{first['id']}").json()
    assert saved["name"] == first["name"] and saved["version"] == 2**63 - 1
    assert counts(postgres_database) == (1, 1, 2)


def test_audit_failure_rolls_back_create_and_update_and_allows_retry(
    unit_api, postgres_database, monkeypatch
):
    """在刷新后注入审计故障，新增和修改都随同命令事务回滚。"""

    def fail_audit(self, change):
        raise AppError(503, "synthetic_audit_failure", "合成测试审计故障")

    body = create_body()
    with monkeypatch.context() as patch:
        patch.setattr(CommandContext, "audit", fail_audit)
        response = unit_api.post(_BASE, json=body)
    assert response.status_code == 503
    assert counts(postgres_database) == (0, 0, 0)
    created = unit_api.post(_BASE, json=body)
    assert created.status_code == 201
    first = created.json()
    modification = update_body(first)
    with monkeypatch.context() as patch:
        patch.setattr(CommandContext, "audit", fail_audit)
        response = unit_api.put(f"{_BASE}/{first['id']}", json=modification)
    assert response.status_code == 503
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first
    assert counts(postgres_database) == (1, 1, 2)
    assert unit_api.put(f"{_BASE}/{first['id']}", json=modification).status_code == 200
    assert counts(postgres_database) == (1, 2, 4)


def test_update_uses_unit_source_namespace_and_retries_after_release(unit_api, postgres_database):
    """单位锁阻止同来源修改，供应商命名空间下相同 ID 的锁独立。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    with postgres_database.sessions.begin() as session:
        lock_sources(session, [SourceKeyDTO("supplier", first["id"])])
        response = unit_api.put(f"{_BASE}/{first['id']}", json=update_body(first))
        assert response.status_code == 200
    changed = response.json()
    modification = update_body(changed, name="箱")
    with postgres_database.sessions.begin() as session:
        lock_sources(session, [SourceKeyDTO("unit", first["id"])])
        response = unit_api.put(f"{_BASE}/{first['id']}", json=modification)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "resource_busy"
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == changed
    assert counts(postgres_database) == (1, 2, 4)
    retried = unit_api.put(f"{_BASE}/{first['id']}", json=modification)
    assert retried.status_code == 200 and retried.json()["version"] == 3


def test_concurrent_unit_updates_have_one_winner(unit_api, postgres_database):
    """两个相同预期版本的修改并发执行时，仅一笔成功并留下审计。"""
    first = unit_api.post(_BASE, json=create_body()).json()
    bodies = [update_body(first, name=name) for name in ("箱", "件")]
    barrier = Barrier(2)

    def send(body):
        barrier.wait(timeout=5)
        return unit_api.put(f"{_BASE}/{first['id']}", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, bodies))
    assert sorted(response.status_code for response in responses) == [200, 409]
    failed = next(response for response in responses if response.status_code == 409)
    assert failed.json()["error"]["code"] == "version_conflict"
    assert unit_api.get(f"{_BASE}/{first['id']}").json()["version"] == 2
    assert counts(postgres_database) == (1, 2, 4)


def test_maintenance_blocks_unit_reads_writes_and_replay(unit_api, postgres_database):
    """维护期间统一拒绝所有单位操作，解除后原会话可继续读取。"""
    body = create_body()
    first = unit_api.post(_BASE, json=body).json()
    gate = unit_api.app.state.maintenance_gate
    gate.set_maintenance(True, release_id="unit-maintenance-test")
    try:
        responses = [
            unit_api.post(_BASE, json=body),
            unit_api.put(f"{_BASE}/{first['id']}", json=update_body(first)),
            unit_api.get(_BASE),
            unit_api.get(f"{_BASE}/{first['id']}"),
        ]
        assert [response.status_code for response in responses] == [503] * 4
        assert all(response.json()["error"]["code"] == "maintenance" for response in responses)
        assert counts(postgres_database) == (1, 1, 2)
    finally:
        with gate.exclusive():
            gate.set_maintenance(False, release_id="unit-maintenance-test")
    assert unit_api.get(f"{_BASE}/{first['id']}").json() == first
