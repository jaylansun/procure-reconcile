"""原件数据库引用的数据访问。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity.stored_file_do import StoredFileDO


class FileRepository:
    """使用调用方的会话查询和保存原件引用，事务由上层管理。"""

    def find_by_sha256(self, session: Session, digest: str) -> StoredFileDO | None:
        """按内容摘要查询原件，供持有内容锁的导入流程判断是否复用。"""
        return session.scalar(select(StoredFileDO).where(StoredFileDO.sha256 == digest))

    def find_by_id(self, session: Session, file_id: UUID) -> StoredFileDO | None:
        """按稳定文件编号查询原件引用。"""
        return session.get(StoredFileDO, file_id)

    def find_id_by_sha256(self, session: Session, digest: str) -> UUID | None:
        """只读取引用编号，供持有内容锁的回收流程再次确认引用状态。"""
        return session.scalar(select(StoredFileDO.file_id).where(StoredFileDO.sha256 == digest))

    def add(self, session: Session, row: StoredFileDO) -> None:
        """新增原件引用并刷新，以获取数据库生成字段。"""
        session.add(row)
        session.flush()
