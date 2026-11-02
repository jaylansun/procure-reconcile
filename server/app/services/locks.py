"""在调用方事务内串行业务命令与稳定来源，不提交或回滚外层事务。"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# 域前缀参与哈希，不能与用户提供的namespace拼接混用，也不要随意更改协议。
_COMMAND_DOMAIN = b"procure-reconcile/command/v1\0"
_SOURCE_DOMAIN = b"procure-reconcile/source/v1\0"


def _source_text(value: str, *, name: str, maximum: int) -> None:
    """只检查来源标识，不隐式去除空格或改变大小写。"""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name}必须为1至{maximum}个字符的非空白字符串")
    try:
        value.encode("utf-8")
    except UnicodeError as failure:
        raise ValueError(f"{name}必须是有效的UTF-8字符串") from failure
    if "\0" in value:
        raise ValueError(f"{name}不能包含空字符")


@dataclass(frozen=True, slots=True)
class SourceKey:
    """跨命令使用同一namespace和identifier，才能锁住同一稳定业务来源。

    namespace最多64字符，identifier最多256字符。仅拒绝纯空白和无效文本；
    其余内容原样参与锁键计算，例如前后空格不会被自动规范化。
    """

    namespace: str
    identifier: str

    def __post_init__(self) -> None:
        _source_text(self.namespace, name="来源命名空间", maximum=64)
        _source_text(self.identifier, name="来源标识", maximum=256)


def _advisory_key(domain: bytes, payload: bytes) -> int:
    """使用SHA-256前8字节生成PG接受的有符号64位整数，不使用Python hash。"""
    digest = hashlib.sha256(domain + payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _source_keys(sources: Iterable[SourceKey]) -> list[int]:
    """先校验完整输入，再按实际锁键排序去重，避免取得部分锁后才发现坏输入。"""
    try:
        supplied = list(sources)
    except TypeError as failure:
        raise ValueError("业务来源必须是SourceKey的可迭代集合") from failure
    keys: set[int] = set()
    for source in supplied:
        if not isinstance(source, SourceKey):
            raise ValueError("业务来源集合只能包含SourceKey")
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


def lock_sources(session: Session, sources: Iterable[SourceKey], *, timeout_ms: int = 3000) -> None:
    """锁定去重后的稳定来源；不同命令锁住相同来源时仍会相互等待。

    非空请求要求既有PG事务。空集合不访问数据库。当前函数不翻译数据库异常，
    执行器应在整体回滚后将SQLSTATE 55P03转换成resource_busy。
    """
    timeout = _timeout(timeout_ms)
    keys = _source_keys(sources)
    if keys:
        _lock_keys(session, keys, timeout)
