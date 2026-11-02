"""业务接口复用的命令输入、执行器依赖与响应转换。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.command_executor import CommandExecutor, CommandResult


class CommandInput(BaseModel):
    """业务输入继承此类，保留客户端重试时不变的命令编号。"""

    model_config = ConfigDict(extra="forbid")
    command_id: UUID

    def command_payload(self) -> dict[str, object]:
        """完整业务输入参与指纹，仅移除独立保存的命令编号。"""
        return self.model_dump(mode="json", exclude={"command_id"})


class VersionedCommandInput(CommandInput):
    """变更操作继承此类，读取并锁定来源后检查预期版本。"""

    expected_version: int = Field(strict=True, ge=0)


def get_command_executor(request: Request) -> CommandExecutor:
    """使用当前服务实例的数据库与锁等待设置，不切换环境。"""
    return request.app.state.command_executor


CommandExecutorDep = Annotated[CommandExecutor, Depends(get_command_executor)]


def command_response(result: CommandResult) -> Response:
    """第一次执行与重放使用相同状态和内容，只增加重放标记。"""
    headers = {
        "X-Command-Replayed": str(result.replayed).lower(),
        "Cache-Control": "no-store",
    }
    if result.status_code in (204, 205):
        return Response(status_code=result.status_code, headers=headers)
    return JSONResponse(content=result.body, status_code=result.status_code, headers=headers)
