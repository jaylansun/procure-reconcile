"""在独立PostgreSQL中验证供应商接口、版本、幂等与事务审计。"""

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

from app.errors import AppError
from app.main import create_app
from app.persistence.database import DatabaseManager
from app.persistence.models import AuditEventDO, CommandRecordDO, SupplierDO
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.services.command_executor import CommandContext
from app.services.locks import SourceKeyDTO, lock_sources
from app.settings import Settings

pytestmark = pytest.mark.postgres
_BASE = "/api/v1/suppliers"


@pytest.fixture
def supplier_api(postgres_database: DatabaseManager, tmp_path: Path) -> Iterator[TestClient]:
    """每次使用专用数据库、管理员和临时维护目录发起真实认证请求。"""
    password = new_session_token()
    initialize_administrator(postgres_database, "supplier-test-admin", password)
    settings = Settings(
        _env_file=None,
        environment="test",
        instance_id="supplier-api-test",
        database_url=postgres_database.engine.url.render_as_string(hide_password=False),
        files_dir=tmp_path / "files",
        maintenance_dir=tmp_path / "maintenance",
        command_lock_timeout_ms=100,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": "supplier-test-admin",
                "password": password,
                "device_name": "supplier-api-test",
            },
        )
        assert response.status_code == 200
        client.headers["Authorization"] = "Bearer " + response.json()["token"]
        yield client


def create_body(name: str = "华星供应商", contact_info: str | None = "联系人张三") -> dict:
    """为一次新增操作生成稳定命令内容，重试时复用返回的整个字典。"""
    return {
        "command_id": str(uuid4()),
        "data": {"name": name, "contact_info": contact_info},
    }


def update_body(supplier: dict, *, name: str = "修改后的供应商") -> dict:
    """携带已读版本和稳定供应商ID，联系信息省略时按完整提交清空。"""
    return {
        "command_id": str(uuid4()),
        "supplier_id": supplier["id"],
        "expected_version": supplier["version"],
        "data": {"name": name},
    }


def counts(database: DatabaseManager) -> tuple[int, int, int]:
    """从真实数据库读取供应商、成功命令和审计事件数量。"""
    with database.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (SupplierDO, CommandRecordDO, AuditEventDO)
        )


def test_create_detail_update_and_audit_snapshots(supplier_api, postgres_database):
    """增改结果对应真实行、版本及同事务保存的前后审计快照。"""
    creation = create_body("  华星供应商  ", "  中文联系信息  ")
    response = supplier_api.post(_BASE, json=creation)
    assert response.status_code == 201
    assert response.headers["x-command-replayed"] == "false"
    assert response.headers["cache-control"] == "no-store"
    first = response.json()
    assert set(first) == {"id", "name", "contact_info", "version", "created_at", "updated_at"}
    UUID(first["id"])
    assert first["name"] == "华星供应商"
    assert first["contact_info"] == "中文联系信息"
    assert first["version"] == 1
    assert datetime.fromisoformat(first["created_at"]).utcoffset() is not None
    assert datetime.fromisoformat(first["updated_at"]).utcoffset() is not None
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == first

    modification = update_body(first)
    response = supplier_api.put(f"{_BASE}/{first['id']}", json=modification)
    assert response.status_code == 200
    changed = response.json()
    assert changed["id"] == first["id"]
    assert changed["name"] == "修改后的供应商"
    assert changed["contact_info"] is None
    assert changed["version"] == 2
    assert changed["created_at"] == first["created_at"]
    assert datetime.fromisoformat(changed["updated_at"]) >= datetime.fromisoformat(
        first["updated_at"]
    )
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == changed
    assert counts(postgres_database) == (1, 2, 4)

    with postgres_database.sessions() as session:
        created_event = session.scalar(
            select(AuditEventDO).where(
                AuditEventDO.command_id == UUID(creation["command_id"]),
                AuditEventDO.target_type == "supplier",
            )
        )
        assert created_event.action == "suppliers.create"
        assert created_event.target_id == first["id"]
        assert created_event.before_version is None
        assert created_event.after_version == 1
        assert created_event.details == {"after": first}
        changed_event = session.scalar(
            select(AuditEventDO).where(
                AuditEventDO.command_id == UUID(modification["command_id"]),
                AuditEventDO.target_type == "supplier",
            )
        )
        assert changed_event.action == "suppliers.update"
        assert (changed_event.before_version, changed_event.after_version) == (1, 2)
        assert changed_event.details == {"before": first, "after": changed}
        stored = session.get(CommandRecordDO, UUID(modification["command_id"]))
        assert stored.response_body == changed
        assert stored.response_status == 200


def test_same_name_suppliers_have_independent_ids(supplier_api, postgres_database):
    """两笔独立命令能够创建同名资料，后续关联依赖各自稳定ID。"""
    first = supplier_api.post(_BASE, json=create_body("同名供应商")).json()
    second = supplier_api.post(_BASE, json=create_body("同名供应商")).json()
    assert first["id"] != second["id"]
    assert first["name"] == second["name"]
    assert counts(postgres_database) == (2, 2, 4)


def test_create_replay_keeps_original_snapshot_after_update(supplier_api, postgres_database):
    """新增命令在资料修改后仍重放首次201快照，重放保持审计数量。"""
    body = create_body()
    first = supplier_api.post(_BASE, json=body).json()
    changed = supplier_api.put(f"{_BASE}/{first['id']}", json=update_body(first)).json()
    replay = supplier_api.post(_BASE, json=body)
    assert replay.status_code == 201
    assert replay.json() == first
    assert replay.headers["x-command-replayed"] == "true"
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == changed
    assert counts(postgres_database) == (1, 2, 4)


def test_command_conflict_rejects_changed_payload_and_operation(supplier_api, postgres_database):
    """成功命令ID绑定原操作和内容，改变资料或用于修改均返回冲突。"""
    body = create_body()
    first = supplier_api.post(_BASE, json=body).json()
    different = {**body, "data": {"name": "不同资料"}}
    response = supplier_api.post(_BASE, json=different)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "command_conflict"
    modification = update_body(first)
    modification["command_id"] = body["command_id"]
    response = supplier_api.put(f"{_BASE}/{first['id']}", json=modification)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "command_conflict"
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == first
    assert counts(postgres_database) == (1, 1, 2)


def test_update_replay_and_stale_version_leave_consistent_records(supplier_api, postgres_database):
    """修改可重放，旧版本失败后同命令ID可携带刷新后的版本重试。"""
    first = supplier_api.post(_BASE, json=create_body()).json()
    body = update_body(first)
    response = supplier_api.put(f"{_BASE}/{first['id']}", json=body)
    changed = response.json()
    assert response.status_code == 200 and changed["version"] == 2
    replay = supplier_api.put(f"{_BASE}/{first['id']}", json=body)
    assert replay.status_code == 200 and replay.json() == changed
    assert replay.headers["x-command-replayed"] == "true"

    stale = update_body(first, name="旧页面的修改")
    failed = supplier_api.put(f"{_BASE}/{first['id']}", json=stale)
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "version_conflict"
    assert counts(postgres_database) == (1, 2, 4)
    with postgres_database.sessions() as session:
        assert session.get(CommandRecordDO, UUID(stale["command_id"])) is None
    stale["expected_version"] = changed["version"]
    retried = supplier_api.put(f"{_BASE}/{first['id']}", json=stale)
    assert retried.status_code == 200
    assert retried.json()["version"] == 3
    assert counts(postgres_database) == (1, 3, 6)
    assert supplier_api.put(f"{_BASE}/{first['id']}", json=body).json() == changed


def test_missing_supplier_and_mismatched_path_leave_no_success_records(
    supplier_api, postgres_database
):
    """不存在的供应商和路径/请求ID不一致都在成功记录保存前失败。"""
    missing = str(uuid4())
    response = supplier_api.get(f"{_BASE}/{missing}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "supplier_not_found"
    body = update_body({"id": missing, "version": 1})
    response = supplier_api.put(f"{_BASE}/{missing}", json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "supplier_not_found"
    response = supplier_api.put(f"{_BASE}/{uuid4()}", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "supplier_id_mismatch"
    assert supplier_api.get(f"{_BASE}/invalid-uuid").status_code == 422
    assert counts(postgres_database) == (0, 0, 0)


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic abc"])
def test_supplier_routes_require_authentication(supplier_api, postgres_database, authorization):
    """新增、修改、详情和列表共用真实会话认证。"""
    supplier_api.headers.pop("Authorization")
    headers = {} if authorization is None else {"Authorization": authorization}
    identifier = str(uuid4())
    responses = [
        supplier_api.post(_BASE, json=create_body(), headers=headers),
        supplier_api.put(
            f"{_BASE}/{identifier}",
            json=update_body({"id": identifier, "version": 1}),
            headers=headers,
        ),
        supplier_api.get(_BASE, headers=headers),
        supplier_api.get(f"{_BASE}/{identifier}", headers=headers),
    ]
    assert [response.status_code for response in responses] == [401] * 4
    assert all(set(response.json()) == {"error"} for response in responses)
    assert counts(postgres_database) == (0, 0, 0)


def test_logout_revokes_supplier_access(supplier_api, postgres_database):
    """退出登录后已有会话令牌失效，供应商入口重新要求认证。"""
    assert supplier_api.post("/api/v1/auth/logout").status_code == 204
    assert supplier_api.get(_BASE).status_code == 401
    assert supplier_api.post(_BASE, json=create_body()).status_code == 401
    assert counts(postgres_database) == (0, 0, 0)


def test_invalid_create_inputs_are_rejected_before_writes(supplier_api, postgres_database):
    """校验遗漏、额外字段、非法文本及长度限制，失败保持数据库为空。"""
    cases = [
        {},
        {"command_id": str(uuid4())},
        {"command_id": "bad-id", "data": {"name": "有效名称"}},
        {"command_id": str(uuid4()), "data": None},
        {**create_body(), "unexpected": "未声明字段"},
        {"command_id": str(uuid4()), "data": {}},
        {"command_id": str(uuid4()), "data": {"name": "有效名称", "unexpected": "多余"}},
    ]
    for value in [None, "", "  ", 123, "x" * 201, "空\0字符", "\ud800"]:
        cases.append({"command_id": str(uuid4()), "data": {"name": value}})
    for value in [123, "x" * 2001, "空\0字符", "\udfff"]:
        cases.append(
            {"command_id": str(uuid4()), "data": {"name": "有效名称", "contact_info": value}}
        )
    for body in cases:
        # 使用转义JSON将无效Unicode送达服务端校验，避免客户端编码先失败。
        response = supplier_api.post(
            _BASE,
            content=json.dumps(body, ensure_ascii=True).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    assert counts(postgres_database) == (0, 0, 0)


def test_valid_text_length_boundaries_and_nullable_contact(supplier_api):
    """最大允许长度及空联系信息能够经真实PG保存和读取。"""
    response = supplier_api.post(_BASE, json=create_body("名" * 200, "信" * 2000))
    assert response.status_code == 201
    created = response.json()
    assert len(created["name"]) == 200 and len(created["contact_info"]) == 2000
    assert supplier_api.get(f"{_BASE}/{created['id']}").json() == created
    response = supplier_api.post(_BASE, json=create_body(contact_info=None))
    assert response.status_code == 201 and response.json()["contact_info"] is None


def test_invalid_update_versions_and_identifiers_preserve_supplier(supplier_api, postgres_database):
    """预期版本必须是非负整数，修改请求始终携带正确UUID和完整名称。"""
    first = supplier_api.post(_BASE, json=create_body()).json()
    for version in [-1, True, "1", 1.0, None]:
        body = update_body(first)
        body["expected_version"] = version
        assert supplier_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    for key in ["command_id", "expected_version", "supplier_id", "data"]:
        body = update_body(first)
        del body[key]
        assert supplier_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    body = update_body(first)
    body["supplier_id"] = "invalid-id"
    assert supplier_api.put(f"{_BASE}/{first['id']}", json=body).status_code == 422
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == first
    assert counts(postgres_database) == (1, 1, 2)


@pytest.mark.parametrize(
    "parameters",
    [
        {"offset": -1},
        {"offset": 2**63},
        {"offset": "not-an-integer"},
        {"offset": "1.5"},
        {"limit": 0},
        {"limit": 101},
        {"limit": "true"},
        {"unknown": "extra"},
    ],
)
def test_invalid_pagination_is_rejected(supplier_api, parameters):
    """查询条件模型在进入分页业务前验证数值边界和额外参数。"""
    response = supplier_api.get(_BASE, params=parameters)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_pagination_has_stable_order_and_empty_page(supplier_api, postgres_database):
    """创建时间相同时以UUID确定次序，分页窗口与默认值准确。"""
    empty = supplier_api.get(_BASE)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "offset": 0, "limit": 20}
    maximum = supplier_api.get(_BASE, params={"offset": 2**63 - 1, "limit": 100})
    assert maximum.status_code == 200
    assert maximum.json() == {"items": [], "offset": 2**63 - 1, "limit": 100}
    suppliers = [
        supplier_api.post(_BASE, json=create_body(f"供应商{number}")).json() for number in range(3)
    ]
    with postgres_database.sessions.begin() as session:
        session.execute(update(SupplierDO).values(created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    expected = sorted(
        (item["id"] for item in suppliers), key=lambda value: UUID(value).int, reverse=True
    )
    full = supplier_api.get(_BASE, params={"limit": 100}).json()
    assert [item["id"] for item in full["items"]] == expected
    window = supplier_api.get(_BASE, params={"offset": 1, "limit": 1}).json()
    assert [item["id"] for item in window["items"]] == expected[1:2]
    assert window["offset"] == 1 and window["limit"] == 1
    assert supplier_api.get(_BASE, params={"offset": 3, "limit": 1}).json() == {
        "items": [],
        "offset": 3,
        "limit": 1,
    }


def test_version_exhaustion_rejects_update_without_partial_audit(supplier_api, postgres_database):
    """BIGINT版本达到上限时整笔修改失败，名称和成功记录保持原事实。"""
    first = supplier_api.post(_BASE, json=create_body()).json()
    with postgres_database.sessions.begin() as session:
        session.execute(update(SupplierDO).values(version=2**63 - 1))
    body = update_body({**first, "version": 2**63 - 1})
    response = supplier_api.put(f"{_BASE}/{first['id']}", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "supplier_version_exhausted"
    saved = supplier_api.get(f"{_BASE}/{first['id']}").json()
    assert saved["version"] == 2**63 - 1 and saved["name"] == first["name"]
    assert counts(postgres_database) == (1, 1, 2)


def test_audit_failure_rolls_back_new_supplier_and_allows_retry(
    supplier_api, postgres_database, monkeypatch
):
    """在flush之后注入审计失败，验证供应商与成功命令整体回滚。"""

    def fail_audit(self, change):
        raise AppError(503, "synthetic_audit_failure", "合成测试审计故障")

    body = create_body()
    with monkeypatch.context() as patch:
        patch.setattr(CommandContext, "audit", fail_audit)
        response = supplier_api.post(_BASE, json=body)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "synthetic_audit_failure"
    assert counts(postgres_database) == (0, 0, 0)
    assert supplier_api.post(_BASE, json=body).status_code == 201
    assert counts(postgres_database) == (1, 1, 2)


def test_update_uses_supplier_source_lock_and_retries_after_release(
    supplier_api, postgres_database
):
    """相同供应商来源锁阻止接口修改，超时回滚后原命令可以重试。"""
    first = supplier_api.post(_BASE, json=create_body()).json()
    body = update_body(first)
    with postgres_database.sessions.begin() as session:
        lock_sources(session, [SourceKeyDTO("supplier", first["id"])])
        response = supplier_api.put(f"{_BASE}/{first['id']}", json=body)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "resource_busy"
    assert counts(postgres_database) == (1, 1, 2)
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == first
    retried = supplier_api.put(f"{_BASE}/{first['id']}", json=body)
    assert retried.status_code == 200 and retried.json()["version"] == 2


def test_concurrent_updates_with_same_version_have_one_winner(supplier_api, postgres_database):
    """两个旧页面同时提交时仅一次修改成功，另一笔明确返回版本冲突。"""
    first = supplier_api.post(_BASE, json=create_body()).json()
    bodies = [update_body(first, name=f"并发修改{number}") for number in range(2)]
    barrier = Barrier(2)

    def send(body):
        barrier.wait(timeout=5)
        return supplier_api.put(f"{_BASE}/{first['id']}", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, bodies))
    assert sorted(response.status_code for response in responses) == [200, 409]
    failed = next(response for response in responses if response.status_code == 409)
    assert failed.json()["error"]["code"] == "version_conflict"
    assert supplier_api.get(f"{_BASE}/{first['id']}").json()["version"] == 2
    assert counts(postgres_database) == (1, 2, 4)


def test_maintenance_blocks_supplier_routes_and_replay(supplier_api, postgres_database):
    """维护期间拒绝新增、修改、读取及命令重放，开放后原会话可继续使用。"""
    creation = create_body()
    first = supplier_api.post(_BASE, json=creation).json()
    gate = supplier_api.app.state.maintenance_gate
    gate.set_maintenance(True, release_id="supplier-maintenance-test")
    try:
        responses = [
            supplier_api.post(_BASE, json=creation),
            supplier_api.put(f"{_BASE}/{first['id']}", json=update_body(first)),
            supplier_api.get(_BASE),
            supplier_api.get(f"{_BASE}/{first['id']}"),
        ]
        assert [response.status_code for response in responses] == [503] * 4
        assert all(response.json()["error"]["code"] == "maintenance" for response in responses)
        assert counts(postgres_database) == (1, 1, 2)
    finally:
        with gate.exclusive():
            gate.set_maintenance(False, release_id="supplier-maintenance-test")
    assert supplier_api.get(f"{_BASE}/{first['id']}").json() == first
