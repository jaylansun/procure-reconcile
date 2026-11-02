"""使用独立PG验证命令串行、事务原子性及明确SQLSTATE的整事务重试。"""

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.errors import AppError
from app.persistence.database import Database
from app.persistence.models import Administrator, AuditEvent, CommandRecord
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.services.audit import AuditChange
from app.services.command_executor import CommandContext, CommandExecutor, CommandResult
from app.services.locks import SourceKey, lock_command, lock_sources

pytestmark = pytest.mark.postgres


@pytest.fixture
def executor(postgres_database: Database) -> CommandExecutor:
    """仅初始化受保护测试库的管理员，不启动HTTP服务。"""
    initialize_administrator(postgres_database, "executor-admin", new_session_token())
    return CommandExecutor(postgres_database)


def run_command(
    executor: CommandExecutor,
    handler: Callable[[CommandContext], CommandResult],
    *,
    command_id: UUID | None = None,
    payload: dict[str, object] | None = None,
    operation: str = "test.change",
    sources: Sequence[SourceKey] = (),
    reason: str | None = "测试操作",
) -> CommandResult:
    """提供测试通用参数，保留每次调用的命令身份与业务内容。"""
    return executor.execute(
        command_id=command_id or uuid4(),
        operation=operation,
        payload={"quantity": "1.000000"} if payload is None else payload,
        actor_id=1,
        handler=handler,
        sources=sources,
        reason=reason,
    )


def update_demo_fact(context: CommandContext, username: str = "changed") -> None:
    """借测试管理员名称模拟可回滚的数据库业务写入，不增加产品业务表。"""
    context.session.execute(
        update(Administrator).where(Administrator.id == 1).values(username=username)
    )


def assert_no_partial_result(database: Database) -> None:
    """异常后命令、审计和示例数据库事实都不应留下半份结果。"""
    assert database.sessions is not None
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert session.get(Administrator, 1).username == "executor-admin"


def test_success_replay_and_custom_audit_are_committed_once(
    executor: CommandExecutor, postgres_database: Database
) -> None:
    """成功事务保存事实和审计，重放相同请求不重复调用回调或追加审计。"""
    command_id, calls = uuid4(), []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        assert context.actor_id == 1
        assert context.operation == "test.change"
        assert context.session.scalar(text("SHOW transaction_isolation")) == "read committed"
        update_demo_fact(context)
        context.audit(
            AuditChange("demo", "1", before_version=1, after_version=2, reason="模拟更新")
        )
        return CommandResult(201, {"message": "完成", "quantity": "1.000000"})

    first = run_command(executor, handler, command_id=command_id)
    replay = run_command(executor, handler, command_id=command_id)
    assert first == CommandResult(201, {"message": "完成", "quantity": "1.000000"})
    assert replay == CommandResult(201, first.body, replayed=True)
    assert calls == [command_id]
    assert postgres_database.sessions is not None
    with postgres_database.sessions() as session:
        events = session.scalars(select(AuditEvent)).all()
        assert len(events) == 2
        automatic = next(event for event in events if event.target_type == "command")
        assert automatic.target_id == str(command_id)
        assert automatic.actor_id == 1
        assert automatic.reason == "测试操作"
        assert next(event for event in events if event.target_type == "demo").after_version == 2
        assert session.get(Administrator, 1).username == "changed"


@pytest.mark.parametrize("change", ["payload", "operation", "reason"])
def test_reusing_id_with_different_content_is_a_conflict(
    executor: CommandExecutor, change: str
) -> None:
    """同命令ID改变内容时返回409，不能再次执行回调。"""
    command_id, calls = uuid4(), []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        return CommandResult(200, {"ok": True})

    run_command(executor, handler, command_id=command_id)
    with pytest.raises(AppError) as error:
        run_command(
            executor,
            handler,
            command_id=command_id,
            payload={"quantity": "2.000000"} if change == "payload" else None,
            operation="test.other" if change == "operation" else "test.change",
            reason="修改后的操作原因" if change == "reason" else "测试操作",
        )
    assert (error.value.status, error.value.code) == (409, "command_conflict")
    assert calls == [command_id]


@pytest.mark.parametrize("reason", [1, "", " \t\n", "x" * 4097, "bad\0reason", "\ud800"])
def test_invalid_reason_does_not_call_handler_or_write_any_result(
    executor: CommandExecutor, postgres_database: Database, reason: object
) -> None:
    """已知不合法的原因必须在回调及数据库写入前拒绝。"""
    with pytest.raises(ValueError):
        run_command(
            executor,
            lambda context: pytest.fail("不合法原因不应调用业务"),
            reason=cast(str, reason),
        )
    assert_no_partial_result(postgres_database)


def test_different_ids_with_same_payload_are_independent(executor: CommandExecutor) -> None:
    """不同命令可以提交相同内容，不能误用指纹去重。"""
    calls: list[UUID] = []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        return CommandResult(200, {"ok": True})

    assert run_command(executor, handler).replayed is False
    assert run_command(executor, handler).replayed is False
    assert len(set(calls)) == 2


def test_two_threads_execute_the_same_id_once(executor: CommandExecutor) -> None:
    """两个独立Session并发提交同命令时，一个执行、另一个等待后重放。"""
    start = threading.Barrier(2)
    command_id, calls = uuid4(), []
    count_lock = threading.Lock()

    def handler(context: CommandContext) -> CommandResult:
        with count_lock:
            calls.append(context.command_id)
        time.sleep(0.1)
        return CommandResult(201, {"ok": True})

    def submit() -> CommandResult:
        start.wait(timeout=5)
        return run_command(executor, handler, command_id=command_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert calls == [command_id]
    assert sorted(result.replayed for result in results) == [False, True]


def test_source_lock_serializes_different_commands(executor: CommandExecutor) -> None:
    """不同命令引用同一稳定来源时，业务回调仍不能并发覆盖该来源。"""
    start = threading.Barrier(2)
    source = SourceKey("executor-test", "shared")
    counter_lock = threading.Lock()
    active, maximum = 0, 0

    def handler(context: CommandContext) -> CommandResult:
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.08)
            return CommandResult(200, {"ok": True})
        finally:
            with counter_lock:
                active -= 1

    def submit() -> CommandResult:
        start.wait(timeout=5)
        return run_command(executor, handler, sources=[source])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        assert all(not future.result(timeout=10).replayed for future in futures)
    assert maximum == 1


@pytest.mark.parametrize("failure", ["business", "version"])
def test_failed_handler_rolls_back_all_state(
    executor: CommandExecutor, postgres_database: Database, failure: str
) -> None:
    """业务异常或版本冲突在已发出写SQL后，仍不能留下命令或审计。"""

    def handler(context: CommandContext) -> CommandResult:
        update_demo_fact(context)
        context.audit(AuditChange("demo", "1"))
        if failure == "version":
            context.expect_version(actual=2, expected=1)
        raise AppError(409, "business_failure", "模拟业务拒绝")

    with pytest.raises(AppError) as error:
        run_command(executor, handler)
    assert error.value.code == ("version_conflict" if failure == "version" else "business_failure")
    assert_no_partial_result(postgres_database)


@pytest.mark.parametrize(
    "control", ["commit", "rollback", "transaction_commit", "transaction_rollback", "close"]
)
def test_handler_cannot_end_transaction_even_if_it_catches_the_error(
    executor: CommandExecutor, postgres_database: Database, control: str
) -> None:
    """提前提交/回滚被锁存为失败，业务捕获异常也不能造成部分提交。"""

    def handler(context: CommandContext) -> CommandResult:
        update_demo_fact(context)
        try:
            if control.startswith("transaction_"):
                transaction = context.session.get_transaction()
                assert transaction is not None
                getattr(transaction, control.removeprefix("transaction_"))()
            else:
                getattr(context.session, control)()
        except AppError:
            pass
        return CommandResult(200, {"should_not_commit": True})

    with pytest.raises(AppError) as error:
        run_command(executor, handler)
    assert error.value.code == "transaction_control_forbidden"
    assert_no_partial_result(postgres_database)


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, {}), (True, {}), (204, {}), (205, []), (200, Decimal("1")), (200, float("nan"))],
)
def test_invalid_result_rolls_back_business_changes(
    executor: CommandExecutor, postgres_database: Database, status: int, body: object
) -> None:
    """状态、无内容响应及精确JSON结果不合法时整事务失败。"""

    def handler(context: CommandContext) -> CommandResult:
        update_demo_fact(context)
        return CommandResult(status, body)

    with pytest.raises(ValueError):
        run_command(executor, handler)
    assert_no_partial_result(postgres_database)


@pytest.mark.parametrize("status", [200, 204, 205])
def test_null_result_round_trip_and_replay(
    executor: CommandExecutor, postgres_database: Database, status: int
) -> None:
    """200的JSON null与204/205的SQL NULL区分保存，重放均返回Python None。"""
    command_id = uuid4()
    first = run_command(executor, lambda context: CommandResult(status), command_id=command_id)
    replay = run_command(
        executor, lambda context: pytest.fail("重放不应调用业务"), command_id=command_id
    )
    assert first == CommandResult(status)
    assert replay == CommandResult(status, replayed=True)
    assert postgres_database.sessions is not None
    with postgres_database.sessions() as session:
        is_sql_null = session.scalar(
            select(CommandRecord.response_body.is_(None)).where(
                CommandRecord.command_id == command_id
            )
        )
        assert is_sql_null is (status in (204, 205))


def test_missing_actor_does_not_call_handler_or_store_results(
    executor: CommandExecutor, postgres_database: Database
) -> None:
    """操作者必须存在，不能写入无有效身份的命令或审计。"""
    with pytest.raises(AppError) as error:
        executor.execute(
            command_id=uuid4(),
            operation="test.change",
            payload={},
            actor_id=999,
            handler=lambda context: pytest.fail("无效身份不应进入业务"),
        )
    assert (error.value.status, error.value.code) == (403, "actor_not_found")
    assert_no_partial_result(postgres_database)


@pytest.mark.parametrize("held_lock", ["command", "source"])
def test_real_lock_timeout_is_resource_busy_and_does_not_retry_handler(
    executor: CommandExecutor, postgres_database: Database, held_lock: str
) -> None:
    """真实PG锁等待超时映射409；失败命令释放锁后可由调用方再次提交。"""
    command_id, source = uuid4(), SourceKey("executor-test", "busy")
    calls: list[UUID] = []
    assert postgres_database.sessions is not None

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        return CommandResult(200, {"ok": True})

    limited = CommandExecutor(postgres_database, lock_timeout_ms=50)
    with postgres_database.sessions.begin() as holder:
        if held_lock == "command":
            lock_command(holder, command_id)
        else:
            lock_sources(holder, [source])
        with pytest.raises(AppError) as error:
            run_command(limited, handler, command_id=command_id, sources=[source])
        assert (error.value.status, error.value.code) == (409, "resource_busy")
        assert calls == []
        assert_no_partial_result(postgres_database)
    assert run_command(executor, handler, command_id=command_id, sources=[source]).replayed is False
    assert calls == [command_id]


@pytest.mark.parametrize("state", ["40001", "40P01"])
def test_pg_transaction_errors_retry_with_fresh_session(
    executor: CommandExecutor, postgres_database: Database, state: str
) -> None:
    """让PG真实抛出事务失败码，验证仅该类失败会重开完整事务。"""
    seen_names: list[str] = []
    sessions: list[object] = []

    def handler(context: CommandContext) -> CommandResult:
        sessions.append(context.session)
        seen_names.append(context.session.get(Administrator, 1).username)
        update_demo_fact(context, f"attempt-{len(seen_names)}")
        if len(seen_names) == 1:
            # state仅来自上面的固定测试参数，不插入外部输入。
            context.session.execute(
                text(f"DO $$ BEGIN RAISE EXCEPTION 'retry-test' USING ERRCODE = '{state}'; END $$")
            )
        return CommandResult(200, {"attempt": len(seen_names)})

    result = run_command(executor, handler)
    assert result.body == {"attempt": 2}
    assert seen_names == ["executor-admin", "executor-admin"]
    assert sessions[0] is not sessions[1]
    assert postgres_database.sessions is not None
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


@pytest.mark.parametrize("state", ["40001", "23505"])
def test_retry_limit_and_nonretryable_errors_leave_no_partial_state(
    executor: CommandExecutor, postgres_database: Database, state: str
) -> None:
    """事务失败最多尝试三次，其他PG错误只执行一次并向上传递。"""
    calls: list[UUID] = []

    def handler(context: CommandContext) -> CommandResult:
        calls.append(context.command_id)
        update_demo_fact(context)
        context.session.execute(
            text(f"DO $$ BEGIN RAISE EXCEPTION 'failure-test' USING ERRCODE = '{state}'; END $$")
        )
        return CommandResult(200)

    with pytest.raises(DBAPIError) as error:
        run_command(executor, handler)
    assert error.value.orig.sqlstate == state
    assert len(calls) == (3 if state == "40001" else 1)
    assert_no_partial_result(postgres_database)
