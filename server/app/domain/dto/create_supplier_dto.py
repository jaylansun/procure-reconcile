"""新增供应商的命令传输对象。"""

from app.domain.dto.command_dto import CommandDTO
from app.domain.dto.supplier_dto import SupplierDTO


class CreateSupplierDTO(CommandDTO):
    """新增命令，包含命令编号和供应商资料。"""

    data: SupplierDTO
