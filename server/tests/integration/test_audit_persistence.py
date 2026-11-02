"""验证审计与命令的真实PG持久化、延迟约束和事务回滚，不与unit模块重名。"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.database import Database
from app.persistence.models import Administrator, AuditEvent, CommandRecord
from app.services.audit import AuditChange, append_audit

pytestmark = pytest.mark.postgres


@pytest.fixture
def audit_sessions(postgres_database: Database) -> sessionmaker[Session]:
    """为审计外键准备测试操作者；不创建可用于真实登录的凭据。"""
    factory = postgres_database.sessions
    assert factory is not None
    with factory.begin() as session:
        session.add(Administrator(id=1, username="audit-test", password_hash="not-a-login-hash"))
    return factory


def command_record(command_id: UUID) -> CommandRecord:
    """构造同事务内的成功记录，响应内容不承担审计明细职责。"""
    return CommandRecord(
        command_id=command_id,
        operation="receipt.accept",
        request_fingerprint="a" * 64,
        response_status=204,
        response_body=None,
    )


def test_audit_fields_and_redacted_precise_details_round_trip(
    audit_sessions: sessionmaker[Session],
) -> None:
    """审计可先flush，命令记录随后保存；字段和精确数值往返不丢失。"""
    command_id = uuid4()
    with audit_sessions.begin() as session:
        change = AuditChange(
            "receipt-line",
            "行-1",
            0,
            1,
            " 人工复核接受 ",
            {
                "amount": "0.01",
                "quantity": "1.000001",
                "large": 2**80,
                "nested": [{"api_key": "synthetic-only", "token": "synthetic-token"}],
            },
        )
        audit = append_audit(
            session, command_id=command_id, actor_id=1, operation="receipt.accept", change=change
        )
        session.flush()
        assert audit.created_at is not None
        session.add(command_record(command_id))
        audit_id = audit.id
    with audit_sessions() as session:
        stored = session.get(AuditEvent, audit_id)
        assert stored is not None
        assert stored.command_id == command_id
        assert stored.actor_id == 1
        assert stored.action == "receipt.accept"
        assert (stored.target_type, stored.target_id) == ("receipt-line", "行-1")
        assert (stored.before_version, stored.after_version) == (0, 1)
        assert stored.reason == " 人工复核接受 "
        assert stored.created_at.utcoffset() is not None
        assert stored.details == {
            "amount": "0.01",
            "quantity": "1.000001",
            "large": 2**80,
            "nested": [{"api_key": "[REDACTED]", "token": "[REDACTED]"}],
        }


def test_outer_rollback_removes_audit_and_command_together(
    audit_sessions: sessionmaker[Session],
) -> None:
    """即使已flush到PG，业务失败后也不留下孤立的成功审计或命令。"""
    command_id = uuid4()
    with pytest.raises(RuntimeError, match="模拟业务失败"), audit_sessions.begin() as session:
        session.add(command_record(command_id))
        append_audit(
            session,
            command_id=command_id,
            actor_id=1,
            operation="receipt.accept",
            change=AuditChange("receipt-line", "行-1"),
        )
        session.flush()
        raise RuntimeError("模拟业务失败")
    with audit_sessions() as session:
        assert session.get(CommandRecord, command_id) is None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_missing_command_fails_at_commit_and_does_not_leave_an_audit(
    audit_sessions: sessionmaker[Session],
) -> None:
    """延迟外键允许回调内flush，但不允许在事务结束后保留无命令审计。"""
    with pytest.raises(IntegrityError) as failure, audit_sessions.begin() as session:
        append_audit(
            session,
            command_id=uuid4(),
            actor_id=1,
            operation="receipt.accept",
            change=AuditChange("receipt-line", "行-1"),
        )
        session.flush()
    assert failure.value.orig.sqlstate == "23503"
    with audit_sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_repeated_appends_and_default_details_do_not_overwrite_existing_events(
    audit_sessions: sessionmaker[Session],
) -> None:
    """同一命令可有多个对象审计，默认字典独立且不会按目标键覆盖记录。"""
    command_id = uuid4()
    with audit_sessions.begin() as session:
        session.add(command_record(command_id))
        first = append_audit(
            session,
            command_id=command_id,
            actor_id=1,
            operation="receipt.accept",
            change=AuditChange("receipt", "1"),
        )
        second = append_audit(
            session,
            command_id=command_id,
            actor_id=1,
            operation="receipt.accept",
            change=AuditChange("receipt", "1"),
        )
        assert first.details is not second.details
        assert first.id != second.id
    with audit_sessions() as session:
        stored = session.scalars(
            select(AuditEvent).where(AuditEvent.command_id == command_id)
        ).all()
        assert len(stored) == 2
        assert all(item.details == {} for item in stored)
        assert all(item.before_version is None and item.after_version is None for item in stored)


def test_append_does_not_flush_unrelated_pending_changes(
    audit_sessions: sessionmaker[Session],
) -> None:
    """审计追加不触发尚未校验的其他模型写入，也不替业务提交事务。"""
    with audit_sessions() as session:
        session.begin()
        flushes: list[bool] = []

        def record_flush(session, context) -> None:
            flushes.append(True)

        event.listen(session, "after_flush", record_flush)
        # 此记录故意违反操作名约束；append若提前flush，测试会在此处失败。
        invalid = command_record(uuid4())
        invalid.operation = ""
        session.add(invalid)
        audit = append_audit(
            session,
            command_id=invalid.command_id,
            actor_id=1,
            operation="receipt.accept",
            change=AuditChange("receipt", "1"),
        )
        assert audit in session.new
        assert flushes == []
        assert session.in_transaction()
        session.rollback()
    with audit_sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
