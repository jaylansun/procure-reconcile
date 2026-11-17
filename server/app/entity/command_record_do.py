"""业务命令成功记录表的数据库映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class CommandRecordDO(BaseDO):
    """保存已成功执行的业务命令，以及后续重试时可重放的响应。

    同一次操作使用同一个 command_id，不同操作可以产生相同的请求指纹。
    后续执行器必须在同一数据库事务内提交业务变更与此记录。
    此模型仅定义持久化结构，本身不执行命令或处理并发重试。
    """

    __tablename__ = "command_records"
    __table_args__ = (
        CheckConstraint(
            "operation ~ '[^[:space:]]'",
            name="ck_command_operation_nonblank",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_command_fingerprint_format",
        ),
        CheckConstraint(
            "response_status >= 200 AND response_status < 300",
            name="ck_command_response_success",
        ),
    )

    # 必须由调用方提供稳定的操作编号，重试时不能自动生成新编号。
    command_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # 只保存成功响应。状态码没有默认值，避免误存尚未执行完的结果。
    response_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Python None 表示没有响应体，例如 204；JSON null 可显式使用 JSON.NULL。
    response_body: Mapped[object | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
