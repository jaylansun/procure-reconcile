"""新增单位的命令传输对象。"""

from app.domain.dto.command_dto import CommandDTO
from app.domain.dto.unit_dto import UnitDTO


class CreateUnitDTO(CommandDTO):
    """接收命令编号和待新增的单位资料。"""

    data: UnitDTO