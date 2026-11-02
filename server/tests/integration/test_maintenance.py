"""用独立PG验证门禁覆盖真实命令提交、回滚和成功结果重放。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.errors import AppError
from app.maintenance import MaintenanceGate
from app.persistence.database import Database
from app.persistence.models import Administrator, AuditEvent, CommandRecord
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.services.command_executor import CommandContext, CommandExecutor, CommandResult

pytestmark = pytest.mark.postgres


@pytest.fixture
def guarded_executor(postgres_database: Database, tmp_path: Path):
    """只使用受保护测试库和每个用例自己的维护目录。"""
    initialize_administrator(postgres_database, "maintenance-admin", new_session_token())
    gate = MaintenanceGate(tmp_path / "maintenance")
    return CommandExecutor(postgres_database, gate=gate), gate


def _execute(executor: CommandExecutor, command_id: UUID, handler) -> CommandResult:
    return executor.execute(
        command_id=command_id,
        operation="test.maintenance-change",
        payload={"name": "changed"},
        actor_id=1,
        handler=handler,
    )


def _counts(database: Database) -> tuple[int, int]:
    assert database.sessions is not None
    with database.sessions() as session:
        commands = session.scalar(select(func.count()).select_from(CommandRecord))
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
    return commands, audits


def test_maintenance_prevents_new_command_before_callback(
    guarded_executor, postgres_database: Database
) -> None:
    """维护中的新命令没有业务效果，也没有伪造成功记录或审计。"""
    executor, gate = guarded_executor
    gate.set_maintenance(True, release_id="test-new-command")
    calls = []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        return CommandResult(200, {"ok": True})

    with pytest.raises(AppError) as error:
        _execute(executor, uuid4(), handler)
    assert (error.value.status, error.value.code) == (503, "maintenance")
    assert calls == []
    assert _counts(postgres_database) == (0, 0)


def test_successful_command_replay_also_respects_maintenance(
    guarded_executor, postgres_database: Database
) -> None:
    """重放也遵守维护边界，不能绕过维护窗口访问命令成功结果。"""
    executor, gate = guarded_executor
    command_id = uuid4()
    calls = []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        return CommandResult(201, {"ok": True})

    original = _execute(executor, command_id, handler)
    gate.set_maintenance(True)
    with pytest.raises(AppError) as error:
        _execute(executor, command_id, handler)
    assert error.value.code == "maintenance"
    assert _counts(postgres_database) == (1, 1)
    with gate.exclusive():
        gate.set_maintenance(False)
    replay = _execute(executor, command_id, handler)
    assert replay.replayed is True
    assert (replay.status_code, replay.body) == (original.status_code, original.body)
    assert calls == [command_id]
    assert _counts(postgres_database) == (1, 1)


@pytest.mark.parametrize("fail", [False, True], ids=["commit", "rollback"])
def test_exclusive_waits_for_the_entire_real_transaction(
    guarded_executor, postgres_database: Database, fail: bool
) -> None:
    """备份独占锁只能在业务/命令/审计一起提交或一起回滚后取得。"""
    executor, gate = guarded_executor
    entered, finish = threading.Event(), threading.Event()

    def handler(context: CommandContext) -> CommandResult:
        context.session.execute(
            update(Administrator).where(Administrator.id == 1).values(username="changed")
        )
        entered.set()
        assert finish.wait(5)
        if fail:
            raise AppError(422, "test_change_rejected", "模拟业务拒绝")
        return CommandResult(200, {"name": "changed"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(_execute, executor, uuid4(), handler)
        try:
            assert entered.wait(3)
            gate.set_maintenance(True, release_id="test-drain")
            with pytest.raises(TimeoutError), gate.exclusive(timeout_seconds=0.05):
                pytest.fail("业务事务尚未结束，不得开始备份")
        finally:
            finish.set()
        if fail:
            with pytest.raises(AppError) as error:
                result.result(timeout=4)
            assert error.value.code == "test_change_rejected"
        else:
            assert result.result(timeout=4).status_code == 200

    with gate.exclusive(timeout_seconds=1):
        assert postgres_database.sessions is not None
        with postgres_database.sessions() as session:
            administrator = session.get(Administrator, 1)
            assert administrator is not None
            assert administrator.username == ("maintenance-admin" if fail else "changed")
        assert _counts(postgres_database) == ((0, 0) if fail else (1, 1))
    assert gate.is_maintenance()
