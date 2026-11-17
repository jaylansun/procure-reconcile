"""不可变原件引用表的数据库映射。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class StoredFileDO(BaseDO):
    """不可变原件的数据库引用，物理路径由内容摘要和服务端根目录推导。"""

    __tablename__ = "stored_files"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_file_sha256_format"),
        CheckConstraint("size_bytes >= 0", name="ck_file_size_nonnegative"),
        CheckConstraint("original_name ~ '[^[:space:]]'", name="ck_file_name_nonblank"),
        CheckConstraint("media_type ~ '[^[:space:]]'", name="ck_file_media_type_nonblank"),
    )

    file_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("administrators.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
