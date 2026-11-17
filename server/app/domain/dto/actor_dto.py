"""传递给业务命令的操作者身份快照。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActorDTO:
    """供业务命令使用的身份快照，不向回调传递口令或登录令牌。"""

    id: int
    username: str
