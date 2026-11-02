"""构造显式业务审计，保存动作与版本依据，不自动抓取请求或凭据。"""

import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.persistence.models import AuditEvent

_MAX_VERSION = 2**63 - 1
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passwordhash",
        "token",
        "tokenhash",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "authtoken",
        "apikey",
        "secret",
        "clientsecret",
        "privatekey",
        "authorization",
        "credentials",
        "databaseurl",
    }
)


def _text(value: str, *, name: str, maximum: int | None = None) -> None:
    """校验PG可保存的文本，保留调用方原始空格，不回显输入内容。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}必须是非空白字符串")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name}不能超过{maximum}个字符")
    _json_text(value)


def _json_text(value: str) -> None:
    """JSONB不能保存空字符；无效Unicode同样在入库前拒绝。"""
    if "\0" in value:
        raise ValueError("审计文本不能包含空字符")
    try:
        value.encode("utf-8")
    except UnicodeError as failure:
        raise ValueError("审计文本必须是有效的UTF-8字符串") from failure


def _copy_json(value: object, ancestors: set[int]) -> object:
    """复制精确JSON值并递归遮盖已知敏感键，拒绝浮点与循环引用。"""
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, str):
        _json_text(value)
        return value
    if not isinstance(value, (dict, list)):
        raise ValueError("审计明细仅支持字典、列表、字符串、整数、布尔值和None")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("审计明细不能包含循环引用")
    ancestors.add(identity)
    try:
        if isinstance(value, list):
            return [_copy_json(item, ancestors) for item in value]
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("审计明细的字典键必须是字符串")
            _json_text(key)
            checked = _copy_json(item, ancestors)
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            copied[key] = _REDACTED if normalized in _SENSITIVE_KEYS else checked
        return copied
    finally:
        ancestors.remove(identity)


def _snapshot_details(details: dict[str, object]) -> dict[str, object]:
    """形成独立明细快照，序列化检查失败时不携带原值。"""
    if not isinstance(details, dict):
        raise ValueError("审计明细必须是字典")
    try:
        snapshot = _copy_json(details, set())
        assert isinstance(snapshot, dict)
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return snapshot
    except (RecursionError, UnicodeError, OverflowError) as failure:
        raise ValueError("审计明细无法转换为精确的UTF-8 JSON") from failure


@dataclass(frozen=True, slots=True)
class AuditChange:
    """调用方显式提供必要业务字段，不传整份请求、口令或会话内容。

    details只接受精确JSON类型，金额数量用十进制字符串。构造及追加时均复制，
    已知password/token/api_key等键（忽略大小写和分隔符）被递归替换为[REDACTED]。
    遮盖不是完整的秘密发现工具；自定义敏感字段仍须由调用方排除。
    """

    target_type: str
    target_id: str
    before_version: int | None = None
    after_version: int | None = None
    reason: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.target_type, name="审计对象类型", maximum=64)
        _text(self.target_id, name="审计对象标识", maximum=256)
        for version in (self.before_version, self.after_version):
            if version is not None and (
                type(version) is not int or not 0 <= version <= _MAX_VERSION
            ):
                raise ValueError("审计版本必须是非负的64位整数或None")
        if self.reason is not None:
            _text(self.reason, name="审计原因", maximum=4096)
        object.__setattr__(self, "details", _snapshot_details(self.details))


def append_audit(
    session: Session, *, command_id: UUID, actor_id: int, operation: str, change: AuditChange
) -> AuditEvent:
    """只加入当前Session，不flush/commit；由执行器与业务和命令记录原子提交。

    command_id外键由数据库延迟检查，允许执行器在同一事务稍后保存成功命令记录。
    每次调用追加新事件，不按command_id或对象标识查询并覆盖既有记录。
    """
    if not isinstance(command_id, UUID):
        raise ValueError("审计命令标识必须为UUID")
    if type(actor_id) is not int or not 1 <= actor_id <= _MAX_VERSION:
        raise ValueError("审计操作者必须为正整数")
    _text(operation, name="审计操作", maximum=128)
    if not isinstance(change, AuditChange):
        raise ValueError("审计变更必须为AuditChange")
    # details仍可能被外部显式修改；入Session前重新检查并脱离调用方容器。
    checked = AuditChange(
        target_type=change.target_type,
        target_id=change.target_id,
        before_version=change.before_version,
        after_version=change.after_version,
        reason=change.reason,
        details=change.details,
    )
    event = AuditEvent(
        id=uuid4(),
        command_id=command_id,
        actor_id=actor_id,
        action=operation,
        target_type=checked.target_type,
        target_id=checked.target_id,
        before_version=checked.before_version,
        after_version=checked.after_version,
        reason=checked.reason,
        details=checked.details,
    )
    session.add(event)
    return event
