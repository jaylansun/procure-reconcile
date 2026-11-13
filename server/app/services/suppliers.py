"""供应商新增、修改、详情和列表业务。"""
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.persistence.models import SupplierDO
from app.schemas.suppliers import CreateSupplierDTO, UpdateSupplierDTO
from app.services.audit import AuditChangeDTO
from app.services.command_executor import CommandContext, CommandResultDTO


def _supplier_output(supplier: SupplierDO) -> dict[str, object]:
    """将数据库记录转换为可保存和返回的JSON数据。"""
    return {
        "id": str(supplier.id),
        "name": supplier.name,
        "contact_info": supplier.contact_info,
        "version": supplier.version,
        "created_at": supplier.created_at.isoformat(),
        "updated_at": supplier.updated_at.isoformat(),
    }


def create_supplier(
        context: CommandContext,
        command: CreateSupplierDTO,
) -> CommandResultDTO:
    """新增供应商，并在当前事务中记录审计。"""
    supplier = SupplierDO(
        name=command.data.name,
        contact_info=command.data.contact_info,
    )
    context.session.add(supplier)

    # flush取得数据库生成的字段，事务由公共执行器统一提交。
    context.session.flush()

    result = _supplier_output(supplier)

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
        context: CommandContext,
        command: UpdateSupplierDTO,
) -> CommandResultDTO:
    """检查记录和版本，再修改当前供应商资料。"""
    supplier = context.session.scalar(
        select(SupplierDO)
        .where(SupplierDO.id == command.supplier_id)
        .with_for_update()
    )

    if supplier is None:
        raise AppError(
            404,
            "supplier_not_found",
            "供应商不存在",
        )

    # 页面读到的版本必须与数据库当前版本一致。
    context.expect_version(
        supplier.version,
        command.expected_version,
    )

    if supplier.version >= 2 ** 63 - 1:
        raise AppError(
            409,
            "supplier_version_exhausted",
            "供应商版本已达到上限",
        )

    before = _supplier_output(supplier)
    previous_version = supplier.version

    supplier.name = command.data.name
    supplier.contact_info = command.data.contact_info
    supplier.version += 1
    supplier.updated_at = datetime.now(UTC)

    context.session.flush()

    after = _supplier_output(supplier)

    context.audit(
        AuditChangeDTO(
            target_type="supplier",
            target_id=str(supplier.id),
            before_version=previous_version,
            after_version=supplier.version,
            details={
                "before": before,
                "after": after,
            },
        )
    )

    return CommandResultDTO(200, after)


def get_supplier(
        session: Session,
        supplier_id: UUID,
) -> dict[str, object]:
    """按稳定ID查询供应商，记录不存在时返回明确错误。"""
    supplier = session.get(SupplierDO, supplier_id)
    if supplier is None:
        raise AppError(
            404,
            "supplier_not_found",
            "供应商不存在",
        )
    return _supplier_output(supplier)


def list_supplier(
        session: Session,
        *,
        offset: int = 0,
        limit: int = 20,
) -> dict[str, object]:
    """分页查询供应商，按创建时间和ID确定返回顺序。"""
    if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
    ):
        raise AppError(
            422,
            "invalid_pagination",
            "分页参数不合法",
        )

    suppliers = session.scalars(
        select(SupplierDO)
        .order_by(SupplierDO.created_at.desc(),
                  SupplierDO.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()


    return {
        "items": [
            _supplier_output(supplier)
            for supplier in suppliers
        ],
        "offset": offset,
        "limit": limit,
    }
