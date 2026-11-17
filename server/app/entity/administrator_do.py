"""管理员表的数据库映射。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class AdministratorDO(BaseDO):
    """管理员持久化对象，映射 administrators 表。"""

    __tablename__ = "administrators"
    __table_args__ = (CheckConstraint("id = 1", name="ck_single_administrator"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
