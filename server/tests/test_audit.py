"""验证审计输入边界、明细快照和追加语义，不连接数据库。"""

from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.audit import AuditChange, append_audit


class AddOnlySession:
    """只允许排队新增，任何意外flush或事务操作都会令测试失败。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


def test_details_are_explicit_independent_snapshots_with_recursive_redaction() -> None:
    """嵌套凭据被遮盖，原输入不改写，追加后的明细不受外部继续编辑影响。"""
    original = {
        "quantity": "1.000001",
        "amount": "12.30",
        "large_integer": 2**80,
        "nested": [{"PASSWORD": "synthetic-password", "api-Key": "synthetic-key"}],
        "authorization": "synthetic-header",
        "token_hash": "synthetic-hash",
        "order_key": "order-1",
        "monkey": "保留业务值",
    }
    change = AuditChange(" receipt ", "行-1 ", 0, 1, " 人工复核 ", original)
    session = AddOnlySession()
    event = append_audit(
        cast(Session, session),
        command_id=uuid4(),
        actor_id=1,
        operation=" receipt.accept ",
        change=change,
    )
    original["quantity"] = "99.000000"
    change.details["amount"] = "999.00"
    assert len(session.added) == 1
    assert isinstance(event.id, UUID)
    assert event.action == " receipt.accept "
    assert (event.target_type, event.target_id, event.reason) == (
        " receipt ",
        "行-1 ",
        " 人工复核 ",
    )
    assert (event.before_version, event.after_version) == (0, 1)
    assert event.details["quantity"] == "1.000001"
    assert event.details["amount"] == "12.30"
    assert event.details["large_integer"] == 2**80
    assert event.details["nested"] == [{"PASSWORD": "[REDACTED]", "api-Key": "[REDACTED]"}]
    assert event.details["authorization"] == event.details["token_hash"] == "[REDACTED]"
    assert event.details["order_key"] == "order-1"
    assert event.details["monkey"] == "保留业务值"
    assert original["nested"] == [{"PASSWORD": "synthetic-password", "api-Key": "synthetic-key"}]


def test_defaults_and_repeated_appends_never_share_or_replace_records() -> None:
    """同一命令可追加多项审计，默认明细互不污染，也不查询覆盖既有记录。"""
    first, second = AuditChange("receipt", "1"), AuditChange("receipt", "2")
    first.details["note"] = "仅第一条"
    assert second.details == {}
    session = AddOnlySession()
    command_id = uuid4()
    events = [
        append_audit(
            cast(Session, session),
            command_id=command_id,
            actor_id=1,
            operation="receipt.accept",
            change=change,
        )
        for change in (first, second)
    ]
    assert len(session.added) == 2
    assert events[0].id != events[1].id
    assert all(event.command_id == command_id for event in events)


@pytest.mark.parametrize(
    "details",
    [
        {"value": 1.0},
        {"value": float("nan")},
        {"value": Decimal("1.20")},
        {"value": datetime(2026, 1, 1)},
        {"value": (1, 2)},
        {"value": {1, 2}},
        {"value": b"bytes"},
        {1: "non-string-key"},
        {"value": "bad\0text"},
        {"value": "bad\ud800text"},
        {"password": object()},
        [],
    ],
)
def test_inexact_or_non_json_details_are_rejected(details: object) -> None:
    """包括敏感键在内的所有输入先校验类型，不能靠遮盖绕过JSON约束。"""
    with pytest.raises(ValueError):
        AuditChange("receipt", "1", details=cast(dict[str, object], details))


def test_cycles_are_rejected_but_shared_noncyclic_values_are_allowed() -> None:
    """只拒绝递归循环，两个字段引用同一合法列表仍是有效JSON。"""
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="循环引用"):
        AuditChange("receipt", "1", details=cyclic)
    shared = ["物料甲", None, True]
    change = AuditChange("receipt", "1", details={"first": shared, "second": shared})
    assert change.details == {"first": shared, "second": shared}
    shared.append("后来修改")
    assert change.details["first"] == ["物料甲", None, True]


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_type": " "},
        {"target_type": "x" * 65},
        {"target_id": ""},
        {"target_id": "x" * 257},
        {"reason": "\t"},
        {"reason": "x" * 4097},
        {"before_version": -1},
        {"after_version": True},
        {"after_version": 1.0},
        {"before_version": 2**63},
    ],
)
def test_invalid_target_reason_and_versions_fail_before_session_add(overrides: dict) -> None:
    """字段长度与64位版本范围在入队前确定，bool不作为版本数。"""
    values = {"target_type": "receipt", "target_id": "1", **overrides}
    with pytest.raises(ValueError):
        AuditChange(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_id": True},
        {"actor_id": 0},
        {"command_id": "not-uuid"},
        {"operation": " "},
        {"operation": "x" * 129},
    ],
)
def test_invalid_append_metadata_does_not_queue_an_event(overrides: dict) -> None:
    """无效操作者、命令和动作不能产生待写入审计。"""
    session = AddOnlySession()
    values = {"command_id": uuid4(), "actor_id": 1, "operation": "receipt.accept", **overrides}
    with pytest.raises(ValueError):
        append_audit(cast(Session, session), **values, change=AuditChange("receipt", "1"))
    assert session.added == []


def test_mutated_details_are_revalidated_at_append_time() -> None:
    """frozen只固定字段引用，调用方后来注入浮点也必须在追加前拒绝。"""
    change = AuditChange("receipt", "1")
    change.details["amount"] = 1.1
    session = AddOnlySession()
    with pytest.raises(ValueError):
        append_audit(
            cast(Session, session),
            command_id=uuid4(),
            actor_id=1,
            operation="receipt.accept",
            change=change,
        )
    assert session.added == []
