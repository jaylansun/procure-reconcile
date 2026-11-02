"""验证来源标识与锁键协议，不连接任何数据库。"""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.locks import SourceKey, lock_command, lock_sources


@pytest.mark.parametrize(
    ("namespace", "identifier"),
    [
        ("", "id"),
        (" \t", "id"),
        ("type", ""),
        ("type", "\n"),
        ("x" * 65, "id"),
        ("type", "x" * 257),
        (None, "id"),
        ("type", 1),
        ("type", "bad\0id"),
        ("type", "bad\ud800id"),
    ],
)
def test_invalid_source_identifiers_are_rejected(namespace: object, identifier: object) -> None:
    """坏标识在接触PG前拒绝，不把输入内容放进错误消息。"""
    with pytest.raises(ValueError):
        SourceKey(cast(str, namespace), cast(str, identifier))


def test_source_identity_is_immutable_and_does_not_implicitly_trim() -> None:
    """同一稳定来源的规范化由业务负责，底层不悄悄合并不同标识。"""
    source = SourceKey(" receipt-line ", "行-1 ")
    assert source.namespace == " receipt-line "
    assert source.identifier == "行-1 "
    assert source != SourceKey("receipt-line", "行-1")
    with pytest.raises(FrozenInstanceError):
        source.identifier = "行-2"  # type: ignore[misc]


def test_lock_keys_are_stable_across_python_hash_seeds() -> None:
    """不同进程、不同集合迭代顺序仍得到相同的有符号64位锁键顺序。"""
    script = (
        "import json; from app.services.locks import SourceKey,_source_keys; "
        "print(json.dumps(_source_keys({SourceKey('receipt','行-1'),"
        "SourceKey('order','行-2'),SourceKey('receipt','行-1')})))"
    )
    outputs = []
    for seed in ("1", "937"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
            capture_output=True,
            check=True,
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 2
    assert outputs[0] == sorted(outputs[0])
    assert all(type(key) is int and -(2**63) <= key <= 2**63 - 1 for key in outputs[0])


@pytest.mark.parametrize("timeout", [0, -1, 60_001, True, 1.5, "3000"])
def test_invalid_timeouts_fail_before_database_access(timeout: object) -> None:
    """0不能关闭超时，bool也不能被当作毫秒数。"""
    with Session() as session:
        with pytest.raises(ValueError, match="锁等待超时"):
            lock_sources(session, [], timeout_ms=cast(int, timeout))
        with pytest.raises(ValueError, match="锁等待超时"):
            lock_command(session, uuid4(), timeout_ms=cast(int, timeout))


def test_empty_sources_do_not_open_a_transaction() -> None:
    """没有稳定来源时不访问数据库，也不替调用方新开事务。"""
    with Session() as session:
        lock_sources(session, [])
        assert not session.in_transaction()


def test_nonempty_sources_require_an_existing_transaction() -> None:
    """锁服务不自行决定外层业务事务的边界。"""
    with Session() as session:
        with pytest.raises(ValueError, match="调用方开启"):
            lock_sources(session, [SourceKey("receipt", "1")])
        assert not session.in_transaction()


def test_non_postgresql_dialect_is_rejected_before_execution() -> None:
    """即使存在事务，也不能在其他数据库静默退化成无锁执行。"""
    session = SimpleNamespace(
        in_transaction=lambda: True,
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="other")),
    )
    with pytest.raises(ValueError, match="仅支持PostgreSQL"):
        lock_command(cast(Session, session), uuid4())


@pytest.mark.parametrize("sources", [None, [SourceKey("receipt", "1"), "bad"]])
def test_the_entire_source_collection_is_validated_before_acquiring_locks(sources: object) -> None:
    """最后一个来源无效时，也不应先取得前面的来源锁。"""
    with Session() as session:
        with pytest.raises(ValueError, match="SourceKey"):
            lock_sources(session, sources)  # type: ignore[arg-type]
        assert not session.in_transaction()
