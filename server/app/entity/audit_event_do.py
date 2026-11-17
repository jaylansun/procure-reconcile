"""追加审计事件表的数据库映射。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.base_do import BaseDO


class AuditEventDO(BaseDO):
    """随业务事务一起提交的追加审计记录，重放命令不重复追加。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("action ~ '[^[:space:]]'", name="ck_audit_action_nonblank"),
        CheckConstraint("target_type ~ '[^[:space:]]'", name="ck_audit_target_type_nonblank"),
        CheckConstraint("target_id ~ '[^[:space:]]'", name="ck_audit_target_id_nonblank"),
        CheckConstraint("before_version >= 0", name="ck_audit_before_version_nonnegative"),
        CheckConstraint("after_version >= 0", name="ck_audit_after_version_nonnegative"),
        CheckConstraint(
            "reason IS NULL OR (reason ~ '[^[:space:]]' AND char_length(reason) <= 4096)",
            name="ck_audit_reason_valid",
        ),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_audit_details_object"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # 延迟检查使回调中的审计 flush 与最终成功记录可以处于同一个事务。
    command_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "command_records.command_id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"
        ),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("administrators.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
