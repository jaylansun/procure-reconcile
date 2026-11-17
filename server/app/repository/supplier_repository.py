"""供应商数据库访问，使用调用方提供的事务会话。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity.supplier_do import SupplierDO


class SupplierRepository:
    """封装供应商查询和持久化，事务提交由调用方负责。"""

    def save(self, session: Session, supplier: SupplierDO) -> None:
        """保存当前供应商，并刷新数据库生成的字段。"""
        session.add(supplier)
        session.flush()

    def get(self, session: Session, supplier_id: UUID) -> SupplierDO | None:
        """按照稳定 ID 查询供应商。"""
        return session.get(SupplierDO, supplier_id)

    def get_for_update(self, session: Session, supplier_id: UUID) -> SupplierDO | None:
        """锁定待修改的供应商记录，锁持续到当前事务结束。"""
        return session.scalar(
            select(SupplierDO)
            .where(SupplierDO.id == supplier_id)
            .with_for_update()
        )

    def list(self, session: Session, *, offset: int, limit: int) -> list[SupplierDO]:
        """按创建时间和 ID 倒序查询，确保分页顺序稳定。"""
        return list(
            session.scalars(
                select(SupplierDO)
                .order_by(SupplierDO.created_at.desc(), SupplierDO.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
        )
