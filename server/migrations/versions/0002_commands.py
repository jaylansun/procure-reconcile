"""新增业务命令成功记录表，为后续请求幂等与结果重放提供持久化结构。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_commands"
down_revision = "0001_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建命令记录表，不修改现有管理员与登录会话数据。"""
    op.create_table(
        "command_records",
        # 不设置默认UUID，调用方必须提供在重试之间保持不变的编号。
        sa.Column("command_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "operation ~ '[^[:space:]]'",
            name="ck_command_operation_nonblank",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_command_fingerprint_format",
        ),
        sa.CheckConstraint(
            "response_status >= 200 AND response_status < 300",
            name="ck_command_response_success",
        ),
    )


def downgrade() -> None:
    """撤销本次表结构；已有命令结果会随表删除，运行前应确认回退范围。"""
    op.drop_table("command_records")
