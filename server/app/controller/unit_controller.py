"""计量单位 HTTP 接口，接入认证、查询和公共命令执行器。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.common.commands.http import CommandExecutorDep, command_response
from app.common.dependencies import ActorDep, AuthDep, SessionDep
from app.common.errors import AppError
from app.domain.dto.create_unit_dto import CreateUnitDTO
from app.domain.dto.source_key_dto import SourceKeyDTO
from app.domain.dto.update_unit_dto import UpdateUnitDTO
from app.domain.qo.unit_list_qo import UnitListQO
from app.domain.vo.unit_list_vo import UnitListVO
from app.domain.vo.unit_vo import UnitVO
from app.repository.unit_repository import UnitRepository
from app.service.unit_service import UnitService

router = APIRouter(prefix="/api/v1/units", tags=["计量单位"])


def get_unit_service() -> UnitService:
    """为当前请求创建单位服务及其数据库访问组件。"""
    return UnitService(UnitRepository())


UnitServiceDep = Annotated[UnitService, Depends(get_unit_service)]


@router.post("", status_code=201, response_model=UnitVO)
def create_unit_endpoint(
    command: CreateUnitDTO,
    actor: ActorDep,
    executor: CommandExecutorDep,
    service: UnitServiceDep,
) -> Response:
    """创建单位，通过命令编号安全重试同一次新增。"""
    result = executor.execute(
        command_id=command.command_id,
        operation="units.create",
        payload=command.command_payload(),
        actor_id=actor.id,
        handler=lambda context: service.create_unit(context, command),
    )
    return command_response(result)


@router.put("/{unit_id}", response_model=UnitVO)
def update_unit_endpoint(
    unit_id: UUID,
    command: UpdateUnitDTO,
    actor: ActorDep,
    executor: CommandExecutorDep,
    service: UnitServiceDep,
) -> Response:
    """锁定单位来源，交由业务服务检查版本并更新名称。"""
    if unit_id != command.unit_id:
        raise AppError(422, "unit_id_mismatch", "路径与请求中的单位ID必须一致")
    result = executor.execute(
        command_id=command.command_id,
        operation="units.update",
        payload=command.command_payload(),
        actor_id=actor.id,
        sources=[SourceKeyDTO("unit", str(unit_id))],
        handler=lambda context: service.update_unit(context, command),
    )
    return command_response(result)


@router.get("", response_model=UnitListVO)
def list_units_endpoint(
    query: Annotated[UnitListQO, Query()],
    session: SessionDep,
    _auth: AuthDep,
    service: UnitServiceDep,
) -> dict[str, object]:
    """查询当前分页，认证和数据库会话覆盖完整读取过程。"""
    return service.list_units(session, offset=query.offset, limit=query.limit)


@router.get("/{unit_id}", response_model=UnitVO)
def get_unit_endpoint(
    unit_id: UUID,
    session: SessionDep,
    _auth: AuthDep,
    service: UnitServiceDep,
) -> dict[str, object]:
    """使用稳定 ID 读取单位详情。"""
    return service.get_unit(session, unit_id)
