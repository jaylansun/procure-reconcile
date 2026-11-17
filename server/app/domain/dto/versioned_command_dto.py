"""需要校验记录版本的业务请求基类。"""

from pydantic import Field

from app.domain.dto.command_dto import CommandDTO


class VersionedCommandDTO(CommandDTO):
    """变更操作继承此类，读取并锁定来源后检查预期版本。"""

    expected_version: int = Field(strict=True, ge=0)
