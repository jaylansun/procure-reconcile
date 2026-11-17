"""供应商新增、修改、详情和列表业务。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.common.commands.command_context import CommandContext
from app.common.errors import AppError
from app.domain.dto.audit_change_dto import AuditChangeDTO
from app.domain.dto.command_result_dto import CommandResultDTO
from app.domain.dto.create_supplier_dto import CreateSupplierDTO
from app.domain.dto.update_supplier_dto import UpdateSupplierDTO
from app.entity.supplier_do import SupplierDO
from app.repository.supplier_repository import SupplierRepository


class SupplierService:
    """校验供应商业务并记录审计，复用命令执行器的事务。"""

    def __init__(self, repository: SupplierRepository) -> None:
        self.repository = repository

    @staticmethod
    def _supplier_output(supplier: SupplierDO) -> dict[str, object]:
        """将数据库记录转换为可保存和返回的 JSON 数据。"""
        return {
            "id": str(supplier.id),
            "name": supplier.name,
            "contact_info": supplier.contact_info,
            "version": supplier.version,
            "created_at": supplier.created_at.isoformat(),
            "updated_at": supplier.updated_at.isoformat(),
        }

    def create_supplier(
        self,
        context: CommandContext,
        command: CreateSupplierDTO,
    ) -> CommandResultDTO:
        """新增供应商，并在当前事务中记录审计。"""
        supplier = SupplierDO(
            name=command.data.name,
            contact_info=command.data.contact_info,
        )
        # 刷新取得数据库生成的字段，事务由公共执行器统一提交。
        self.repository.save(context.session, supplier)

        result = self._supplier_output(supplier)
        context.audit(
            AuditChangeDTO(
                target_type="supplier",
                target_id=str(supplier.id),
                after_version=supplier.version,
                details={"after": result},
            )
        )
        return CommandResultDTO(201, result)

    def update_supplier(
        self,
        context: CommandContext,
        command: UpdateSupplierDTO,
    ) -> CommandResultDTO:
        """检查记录和版本，再修改当前供应商资料。"""
        supplier = self.repository.get_for_update(context.session, command.supplier_id)
        if supplier is None:
            raise AppError(404, "supplier_not_found", "供应商不存在")

        # 页面读到的版本必须与数据库当前版本一致。
        context.expect_version(supplier.version, command.expected_version)
        if supplier.version >= 2**63 - 1:
            raise AppError(409, "supplier_version_exhausted", "供应商版本已达到上限")

        before = self._supplier_output(supplier)
        previous_version = supplier.version
        supplier.name = command.data.name
        supplier.contact_info = command.data.contact_info
        supplier.version += 1
        supplier.updated_at = datetime.now(UTC)
        self.repository.save(context.session, supplier)

        after = self._supplier_output(supplier)
        context.audit(
            AuditChangeDTO(
                target_type="supplier",
                target_id=str(supplier.id),
                before_version=previous_version,
                after_version=supplier.version,
                details={"before": before, "after": after},
            )
        )
        return CommandResultDTO(200, after)

    def get_supplier(self, session: Session, supplier_id: UUID) -> dict[str, object]:
        """按稳定 ID 查询供应商，记录不存在时返回明确错误。"""
        supplier = self.repository.get(session, supplier_id)
        if supplier is None:
            raise AppError(404, "supplier_not_found", "供应商不存在")
        return self._supplier_output(supplier)

    def list_supplier(
        self,
        session: Session,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, object]:
        """分页查询供应商，按创建时间和 ID 确定返回顺序。"""
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise AppError(422, "invalid_pagination", "分页参数不合法")

        suppliers = self.repository.list(session, offset=offset, limit=limit)
        return {
            "items": [self._supplier_output(supplier) for supplier in suppliers],
            "offset": offset,
            "limit": limit,
        }
