"""业务命令的成功结果传输对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResultDTO:
    """命令的成功JSON结果；replayed由执行器决定，不由业务回调决定。"""

    status_code: int
    body: object | None = None
    replayed: bool = False
