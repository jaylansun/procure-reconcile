"""建立计量单位基础资料表。"""

import sqlalchemy as sa
from alembic import op

revision = "0005_units"
down_revision = "0004_suppliers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立稳定标识、单位名称、版本及创建和修改时间字段。"""
    op.create_table(
        "units",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name="ck_unit_name_nonblank"),
        sa.CheckConstraint("version >= 1", name="ck_unit_version_positive"),
    )


def downgrade() -> None:
    """撤销单位表及其中的资料。"""
    op.drop_table("units")
