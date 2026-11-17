"""参与事务来源锁的稳定标识。"""

from dataclasses import dataclass


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
class SourceKeyDTO:
    """跨命令使用同一namespace和identifier，才能锁住同一稳定业务来源。

    namespace最多64字符，identifier最多256字符。仅拒绝纯空白和无效文本；
    其余内容原样参与锁键计算，例如前后空格不会被自动规范化。
    """

    namespace: str
    identifier: str

    def __post_init__(self) -> None:
        _source_text(self.namespace, name="来源命名空间", maximum=64)
        _source_text(self.identifier, name="来源标识", maximum=256)
