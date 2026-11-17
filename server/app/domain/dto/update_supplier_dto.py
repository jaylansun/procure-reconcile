"""修改供应商的命令传输对象。"""

from uuid import UUID

from app.domain.dto.supplier_dto import SupplierDTO
from app.domain.dto.versioned_command_dto import VersionedCommandDTO


class UpdateSupplierDTO(VersionedCommandDTO):
    """修改命令，提供供应商 ID、预期版本和完整资料。"""

    supplier_id: UUID
    data: SupplierDTO
