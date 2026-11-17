"""业务接口复用的命令输入、执行器依赖与响应转换。"""

from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse

from app.common.commands.command_executor import CommandExecutor
from app.domain.dto.command_result_dto import CommandResultDTO


def get_command_executor(request: Request) -> CommandExecutor:
    """使用当前服务实例的数据库与锁等待设置，不切换环境。"""
    return request.app.state.command_executor


CommandExecutorDep = Annotated[CommandExecutor, Depends(get_command_executor)]


def command_response(result: CommandResultDTO) -> Response:
    """第一次执行与重放使用相同状态和内容，只增加重放标记。"""
    headers = {
        "X-Command-Replayed": str(result.replayed).lower(),
        "Cache-Control": "no-store",
    }
    if result.status_code in (204, 205):
        return Response(status_code=result.status_code, headers=headers)
    return JSONResponse(content=result.body, status_code=result.status_code, headers=headers)
