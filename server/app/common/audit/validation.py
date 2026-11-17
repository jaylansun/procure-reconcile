"""构造显式业务审计，保存动作与版本依据，不自动抓取请求或凭据。"""

import json
import re

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
