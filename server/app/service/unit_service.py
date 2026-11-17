"""计量单位新增、修改、详情和列表业务。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.common.commands.command_context import CommandContext
from app.common.errors import AppError
from app.domain.dto.audit_change_dto import AuditChangeDTO
from app.domain.dto.command_result_dto import CommandResultDTO
from app.domain.dto.create_unit_dto import CreateUnitDTO
from app.domain.dto.update_unit_dto import UpdateUnitDTO
from app.entity.unit_do import UnitDO
from app.repository.unit_repository import UnitRepository


class UnitService:
    """处理单位业务，通过公共命令执行器保存变更与审计。"""

    def __init__(self, repository: UnitRepository) -> None:
        self.repository = repository

    @staticmethod
    def _unit_output(unit: UnitDO) -> dict[str, object]:
        """转换为可以保存为命令快照并返回的 JSON 数据。"""
        return {
            "id": str(unit.id),
            "name": unit.name,
            "version": unit.version,
            "created_at": unit.created_at.isoformat(),
            "updated_at": unit.updated_at.isoformat(),
        }

    def create_unit(
            self,
            context: CommandContext,
            command: CreateUnitDTO,
    ) -> CommandResultDTO:
        """新增单位，并记录创建后的资料快照。"""
        unit = UnitDO(name=command.data.name)

        # 刷新后才能取得 ID、初始版本和数据库生成的时间。
        self.repository.save(context.session, unit)
        result = self._unit_output(unit)

        context.audit(
            AuditChangeDTO(
                target_type="unit",
                target_id=str(unit.id),
                after_version=unit.version,
                details={"after": result},
            )
        )
        return CommandResultDTO(201, result)

    def update_unit(
            self,
            context: CommandContext,
            command: UpdateUnitDTO,
    ) -> CommandResultDTO:
        """锁定单位，校验版本，再保存资料和变更审计。"""
        unit = self.repository.get_for_update(
            context.session,
            command.unit_id,
        )
        if unit is None:
            raise AppError(
                404,
                "unit_not_found",
                "单位不存在",
            )
        # 比较页面读到的版本与数据库当前版本。
        context.expect_version(
            unit.version,
            command.expected_version,
        )

        # PostgreSQL BIGINT 的最大值为 2**63 - 1。
        if unit.version >= 2 ** 63 - 1:
            raise AppError(
                409,
                "unit_version_exhausted",
                "单位版本已达到上限",
            )

        before = self._unit_output(unit)
        previous_version = unit.version

        unit.name = command.data.name
        unit.version += 1
        unit.updated_at = datetime.now(UTC)

        self.repository.save(context.session, unit)
        after = self._unit_output(unit)

        context.audit(
            AuditChangeDTO(
                target_type="unit",
                target_id=str(unit.id),
                before_version=previous_version,
                after_version=unit.version,
                details={
                    "before": before,
                    "after": after
                },
            )
        )
        return CommandResultDTO(200, after)

    def get_unit(
            self,
            session: Session,
            unit_id: UUID,
    ) -> dict[str, object]:
        """查询单位详情，记录缺失时返回明确错误。"""
        unit = self.repository.get(session, unit_id)

        if unit is None:
            raise AppError(
                404,
                "unit_not_found",
                "单位不存在",
            )
        return self._unit_output(unit)

    def list_units(
            self,
            session: Session,
            *,
            offset: int = 0,
            limit: int = 20,
    ) -> dict[str, object]:
        """校验分页条件，返回当前页的单位资料。"""
        # type 检查同时排除 bool，偏移量上限对齐 PostgreSQL BIGINT。
        if (
                type(offset) is not int
                or not 0 <= offset <= 2 ** 63 - 1
                or type(limit) is not int
                or not 1 <= limit <= 100
        ):
            raise AppError(
                422,
                "invalid_pagination",
                "分页参数不合法",
            )
        units = self.repository.list(
            session,
            offset=offset,
            limit=limit,
        )

        return {
            "items": [
                self._unit_output(unit)
                for unit in units
            ],
            "offset": offset,
            "limit": limit,
        }
