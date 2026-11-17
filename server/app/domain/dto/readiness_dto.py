"""服务就绪状态的内部传输对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessDTO:
    """服务就绪检查的内部传输对象，携带状态和原因。"""

    ready: bool
    reason: str | None = None
