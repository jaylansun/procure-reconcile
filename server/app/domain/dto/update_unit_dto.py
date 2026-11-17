"""修改单位的命令传输对象。"""

from uuid import UUID

from app.domain.dto.unit_dto import UnitDTO
from app.domain.dto.versioned_command_dto import VersionedCommandDTO


class UpdateUnitDTO(VersionedCommandDTO):
    """接收单位 ID、预期版本和修改后的完整资料。"""

    unit_id: UUID
    data: UnitDTO