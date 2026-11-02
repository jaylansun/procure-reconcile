from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Administrator(Base):
    __tablename__ = "administrators"
    __table_args__ = (CheckConstraint("id = 1", name="ck_single_administrator"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    administrator_id: Mapped[int] = mapped_column(
        ForeignKey("administrators.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommandRecord(Base):
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
    # Python None 表示没有响应体，例如204；JSON null可显式使用JSON.NULL。
    response_body: Mapped[object | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
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
    # 延迟检查使回调中的审计flush与最终成功记录可以处于同一个事务。
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


class StoredFile(Base):
    """不可变原件的数据库引用；物理路径只由内容摘要和服务端根目录推导。"""

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
