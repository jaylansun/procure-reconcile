"""新增事务审计与不可变原件引用，保留既有认证和命令记录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_infrastructure"
down_revision = "0002_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立审计和文件引用表，文件内容仍保存在服务端附件目录。"""
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.SmallInteger(), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("before_version", sa.BigInteger(), nullable=True),
        sa.Column("after_version", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(none_as_null=True), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["command_records.command_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["administrators.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("action ~ '[^[:space:]]'", name="ck_audit_action_nonblank"),
        sa.CheckConstraint("target_type ~ '[^[:space:]]'", name="ck_audit_target_type_nonblank"),
        sa.CheckConstraint("target_id ~ '[^[:space:]]'", name="ck_audit_target_id_nonblank"),
        sa.CheckConstraint("before_version >= 0", name="ck_audit_before_version_nonnegative"),
        sa.CheckConstraint("after_version >= 0", name="ck_audit_after_version_nonnegative"),
        sa.CheckConstraint(
            "reason IS NULL OR (reason ~ '[^[:space:]]' AND char_length(reason) <= 4096)",
            name="ck_audit_reason_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_audit_details_object"),
    )
    op.create_index("ix_audit_events_command_id", "audit_events", ["command_id"])
    op.create_table(
        "stored_files",
        sa.Column("file_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("created_by_id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["administrators.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_file_sha256_format"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_file_size_nonnegative"),
        sa.CheckConstraint("original_name ~ '[^[:space:]]'", name="ck_file_name_nonblank"),
        sa.CheckConstraint("media_type ~ '[^[:space:]]'", name="ck_file_media_type_nonblank"),
    )


def downgrade() -> None:
    """只删除本次新增的结构，不改动既有认证与命令表。"""
    op.drop_table("stored_files")
    op.drop_index("ix_audit_events_command_id", table_name="audit_events")
    op.drop_table("audit_events")
