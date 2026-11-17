"""在调用方事务内串行业务命令与稳定来源，不提交或回滚外层事务。"""

import hashlib
import json
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.dto.source_key_dto import SourceKeyDTO

# 域前缀参与哈希，不能与用户提供的namespace拼接混用，也不要随意更改协议。
_COMMAND_DOMAIN = b"procure-reconcile/command/v1\0"
_SOURCE_DOMAIN = b"procure-reconcile/source/v1\0"


def _advisory_key(domain: bytes, payload: bytes) -> int:
    """使用SHA-256前8字节生成PG接受的有符号64位整数，不使用Python hash。"""
    digest = hashlib.sha256(domain + payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _source_keys(sources: Iterable[SourceKeyDTO]) -> list[int]:
    """先校验完整输入，再按实际锁键排序去重，避免取得部分锁后才发现坏输入。"""
    try:
        supplied = list(sources)
    except TypeError as failure:
        raise ValueError("业务来源必须是SourceKeyDTO的可迭代集合") from failure
    keys: set[int] = set()
    for source in supplied:
        if not isinstance(source, SourceKeyDTO):
            raise ValueError("业务来源集合只能包含SourceKeyDTO")
        # JSON数组保留边界，避免namespace/identifier中含分隔符时出现拼接歧义。
        payload = json.dumps(
            [source.namespace, source.identifier], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        keys.add(_advisory_key(_SOURCE_DOMAIN, payload))
    return sorted(keys)


def _timeout(timeout_ms: int) -> int:
    """拒绝bool及0，避免把无界等待误当成有效的锁超时配置。"""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60_000:
        raise ValueError("锁等待超时必须是1至60000的整数毫秒")
    return timeout_ms


def _lock_keys(session: Session, keys: list[int], timeout_ms: int) -> None:
    """成功后恢复原lock_timeout；55P03发生时保留原异常，由调用方整体回滚。"""
    if not session.in_transaction():
        raise ValueError("取得业务锁前必须由调用方开启数据库事务")
    if session.get_bind().dialect.name != "postgresql":
        raise ValueError("业务事务锁仅支持PostgreSQL")
    original = session.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    # set_config的第三个参数true等价SET LOCAL，同时支持安全的绑定参数。
    setting = text("SELECT set_config('lock_timeout', :timeout, true)")
    session.execute(setting, {"timeout": f"{timeout_ms}ms"})
    for key in keys:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    # 锁等待失败后PG事务已处于失败状态，不能在finally里再执行SQL掩盖55P03。
    session.execute(setting, {"timeout": original})


def lock_command(session: Session, command_id: UUID, *, timeout_ms: int = 3000) -> None:
    """先锁命令ID，再由执行器调用lock_sources；锁在事务结束时自动释放。"""
    timeout = _timeout(timeout_ms)
    if not isinstance(command_id, UUID):
        raise ValueError("命令标识必须为UUID")
    _lock_keys(session, [_advisory_key(_COMMAND_DOMAIN, command_id.bytes)], timeout)


def lock_sources(
    session: Session, sources: Iterable[SourceKeyDTO], *, timeout_ms: int = 3000
) -> None:
    """锁定去重后的稳定来源；不同命令锁住相同来源时仍会相互等待。

    非空请求要求既有PG事务。空集合不访问数据库。当前函数不翻译数据库异常，
    执行器应在整体回滚后将SQLSTATE 55P03转换成resource_busy。
    """
    timeout = _timeout(timeout_ms)
    keys = _source_keys(sources)
    if keys:
        _lock_keys(session, keys, timeout)
