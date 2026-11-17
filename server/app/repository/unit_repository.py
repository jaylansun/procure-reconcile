"""计量单位的数据库访问。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity.unit_do import UnitDO


class UnitRepository:
    """使用调用方的 Session 查询和保存单位。"""

    def save(self, session: Session, unit: UnitDO) -> None:
        """保存单位，并刷新数据库生成的字段。"""
        session.add(unit)
        session.flush()

    def get(self, session: Session, unit_id: UUID) -> UnitDO | None:
        """根据 ID 查询单位。"""
        return session.get(UnitDO, unit_id)

    def get_for_update(self,
        session: Session,
        unit_id: UUID
        ) -> UnitDO | None:
        """取得待修改记录的行锁，持续至当前事务结束。"""
        return session.scalar(
            select(UnitDO)
            .where(UnitDO.id == unit_id)
            .with_for_update()
        )

    def list(
            self,
            session: Session,
            *,
            offset: int,
            limit: int,
    ) -> list[UnitDO]:
        """按创建时间和 ID 倒序分页查询。"""
        return list(
            session.scalars(
                select(UnitDO).
                order_by(
                    UnitDO.created_at.desc(),
                    UnitDO.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            ).all()
        )
