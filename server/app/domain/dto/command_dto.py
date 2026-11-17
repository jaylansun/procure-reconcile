"""带稳定命令编号的业务请求基类。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CommandDTO(BaseModel):
    """业务输入继承此类，保留客户端重试时不变的命令编号。"""

    model_config = ConfigDict(extra="forbid")
    command_id: UUID

    def command_payload(self) -> dict[str, object]:
        """完整业务输入参与指纹，仅移除独立保存的命令编号。"""
        return self.model_dump(mode="json", exclude={"command_id"})
