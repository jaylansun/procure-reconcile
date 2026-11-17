"""计量单位表的数据库映射。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class UnitDO(BaseDO):
    """保存计量单位，后续物料和单据通过稳定 ID 引用。"""

    __tablename__ = "units"
    __table_args__ = (
        CheckConstraint(
            "name ~ '[^[:space:]]'",
            name="ck_unit_name_nonblank"
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_unit_version_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )

    # 单位名称用于展示，业务引用保存稳定 ID。
    name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # 每次成功修改递增，防止旧页面覆盖新资料
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
