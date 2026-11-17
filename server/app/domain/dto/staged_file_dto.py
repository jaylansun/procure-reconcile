"""已完整写入的暂存文件传输对象。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StagedFileDTO:
    """已完整写入并同步的暂存文件，只能交回同一存储目录。"""

    path: Path
    sha256: str
    size_bytes: int
