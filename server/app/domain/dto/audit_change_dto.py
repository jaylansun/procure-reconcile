"""业务审计变更的传输对象。"""

from dataclasses import dataclass, field

from app.common.audit.validation import _MAX_VERSION, _snapshot_details, _text


@dataclass(frozen=True, slots=True)
class AuditChangeDTO:
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
