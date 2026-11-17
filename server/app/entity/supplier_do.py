"""供应商基础资料表的数据库映射。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class SupplierDO(BaseDO):
    """供应商基础资料，后续单据通过稳定 ID 关联。"""

    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint(
            "name ~ '[^[:space:]]'",
            name="ck_supplier_name_nonblank",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_supplier_version_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )

    # 名称不承担身份识别职责，同名供应商通过不同 ID 区分。
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    contact_info: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )

    # 每次成功修改递增，防止旧页面覆盖新资料。
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
