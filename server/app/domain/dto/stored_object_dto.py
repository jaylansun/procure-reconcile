"""已发布内容对象的传输数据。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredObjectDTO:
    """已持久化的内容对象；created表示本次是否首次发布。"""

    path: Path
    sha256: str
    size_bytes: int
    created: bool
