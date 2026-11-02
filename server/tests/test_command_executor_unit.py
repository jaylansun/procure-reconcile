"""验证无需数据库连接的命令配置与版本比较边界。"""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.errors import AppError
from app.persistence.database import Database
from app.services.command_executor import CommandContext, CommandExecutor, CommandResult


@pytest.mark.parametrize("attempts", [0, 4, True])
def test_transaction_attempts_are_bounded(attempts: int) -> None:
    """尝试次数不能配置成零、无限重试或布尔值。"""
    with pytest.raises(ValueError):
        CommandExecutor(Database(None), max_attempts=attempts)


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_lock_timeout_must_be_positive(timeout: int) -> None:
    """禁止无界锁等待或无效毫秒值。"""
    with pytest.raises(ValueError):
        CommandExecutor(Database(None), lock_timeout_ms=timeout)


def test_expected_version_conflict_is_a_business_error() -> None:
    """相同整数版本允许继续，过期版本返回明确409。"""
    with Session() as session:
        context = CommandContext(session, uuid4(), 1, "test.change")
        context.expect_version(actual=2, expected=2)
        with pytest.raises(AppError) as error:
            context.expect_version(actual=2, expected=1)
    assert (error.value.status, error.value.code) == (409, "version_conflict")


@pytest.mark.parametrize("payload", [[], None, "not-an-object"])
def test_payload_root_remains_a_dictionary_after_envelope_is_added(payload: object) -> None:
    """内部指纹封装不能意外放宽公开接口对payload根字典的要求。"""
    with pytest.raises(ValueError):
        CommandExecutor(Database(None)).execute(
            command_id=uuid4(),
            operation="test.change",
            payload=cast(dict[str, object], payload),
            actor_id=1,
            handler=lambda context: CommandResult(200),
        )
