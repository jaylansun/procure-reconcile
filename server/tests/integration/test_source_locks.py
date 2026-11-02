"""在专用PG中验证事务锁的实际排序、互斥与释放，不使用业务库。"""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.database import Database
from app.services.locks import SourceKey, lock_command, lock_sources

pytestmark = pytest.mark.postgres


def sessions(database: Database) -> sessionmaker[Session]:
    """只使用conftest已校验为专用测试库的Session工厂。"""
    assert database.sessions is not None
    return database.sessions


def test_real_postgresql_receives_sorted_unique_source_locks(postgres_database: Database) -> None:
    """反序及重复输入在PG中只取得两把锁，实际提交顺序按锁键排序。"""
    assert postgres_database.engine is not None
    acquired: list[int] = []

    def capture(connection, cursor, statement, parameters, context, executemany) -> None:
        if "SELECT pg_advisory_xact_lock(" in statement:
            acquired.append(parameters["key"])

    event.listen(postgres_database.engine, "before_cursor_execute", capture)
    try:
        with sessions(postgres_database).begin() as session:
            a, b = SourceKey("receipt", "行-A"), SourceKey("order", "行-B")
            lock_sources(session, [b, a, b, a])
            count = session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND granted"
                )
            )
            assert count == 2
    finally:
        event.remove(postgres_database.engine, "before_cursor_execute", capture)
    assert len(acquired) == 2
    assert acquired == sorted(set(acquired))


def _wait_until_blocked(factory: sessionmaker[Session], pid: int) -> bool:
    """查询真实PG等待状态，避免仅凭线程还没结束就推断它正在等待锁。"""
    deadline = time.monotonic() + 2
    with factory() as observer:
        while time.monotonic() < deadline:
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            waiting = observer.scalar(
                text(
                    "SELECT wait_event_type = 'Lock' AND wait_event = 'advisory' "
                    "FROM pg_stat_activity WHERE pid = :pid"
                ),
                {"pid": pid},
            )
            if waiting:
                return True
            time.sleep(0.01)
    return False


def test_different_commands_share_source_lock_and_rollback_releases_it(
    postgres_database: Database,
) -> None:
    """两个命令ID不能绕过同一来源锁，持有方回滚后另一个线程可继续。"""
    factory = sessions(postgres_database)
    source = SourceKey("receipt-line", str(uuid4()))
    worker_pid: Queue[int] = Queue()

    def contender() -> str:
        with factory.begin() as session:
            lock_command(session, uuid4())
            worker_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
            lock_sources(session, [source], timeout_ms=3000)
            return "已取得稳定来源"

    with factory() as holder:
        holder.begin()
        lock_command(holder, uuid4())
        lock_sources(holder, [source])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            try:
                pid = worker_pid.get(timeout=3)
                assert _wait_until_blocked(factory, pid)
                assert not future.done()
            finally:
                holder.rollback()
            assert future.result(timeout=4) == "已取得稳定来源"


def test_lock_timeout_keeps_55p03_and_transaction_can_be_retried(
    postgres_database: Database,
) -> None:
    """超时原样交给执行器，失败事务回滚且持有方释放后可重试。"""
    factory = sessions(postgres_database)
    source = SourceKey("receipt-line", str(uuid4()))
    with factory() as holder:
        holder.begin()
        lock_sources(holder, [source])
        with pytest.raises(DBAPIError) as failure, factory.begin() as contender:
            lock_sources(contender, [source], timeout_ms=60)
        assert failure.value.orig.sqlstate == "55P03"
        assert holder.in_transaction()
        holder.rollback()
    with factory.begin() as retried:
        lock_sources(retried, [source], timeout_ms=100)
        assert retried.scalar(text("SELECT 1")) == 1


def test_helpers_keep_outer_transaction_and_restore_its_timeout(
    postgres_database: Database,
) -> None:
    """事务ID保持不变，取得锁后调用方原有的LOCAL超时也保持不变。"""
    with sessions(postgres_database).begin() as session:
        session.execute(text("SELECT set_config('lock_timeout', '17s', true)"))
        transaction = session.get_transaction()
        xid = session.scalar(text("SELECT txid_current()"))
        lock_command(session, uuid4(), timeout_ms=80)
        lock_sources(session, [SourceKey("file-content", "a" * 64)], timeout_ms=80)
        assert session.get_transaction() is transaction
        assert session.scalar(text("SELECT txid_current()")) == xid
        assert session.scalar(text("SELECT current_setting('lock_timeout')")) == "17s"


def test_command_and_source_domains_do_not_alias(postgres_database: Database) -> None:
    """用户来源即使命名为command，也不会占用命令ID保留域。"""
    factory = sessions(postgres_database)
    command_id = uuid4()
    with factory.begin() as holder:
        lock_command(holder, command_id)
        with factory.begin() as other:
            lock_sources(other, [SourceKey("command", str(command_id))], timeout_ms=60)
            lock_sources(other, [SourceKey("receipt-line", "独立来源")], timeout_ms=60)


def test_same_command_id_also_waits_for_transaction_completion(
    postgres_database: Database,
) -> None:
    """命令记录出现前，同一命令ID就已由事务级锁串行保护。"""
    factory = sessions(postgres_database)
    command_id = uuid4()
    with factory.begin() as holder:
        lock_command(holder, command_id)
        with pytest.raises(DBAPIError) as failure, factory.begin() as other:
            lock_command(other, command_id, timeout_ms=60)
        assert failure.value.orig.sqlstate == "55P03"
