"""供应商 HTTP 接口，接入认证、查询和公共命令执行器。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.common.commands.http import CommandExecutorDep, command_response
from app.common.dependencies import ActorDep, AuthDep, SessionDep
from app.common.errors import AppError
from app.domain.dto.create_supplier_dto import CreateSupplierDTO
from app.domain.dto.source_key_dto import SourceKeyDTO
from app.domain.dto.update_supplier_dto import UpdateSupplierDTO
from app.domain.qo.supplier_list_qo import SupplierListQO
from app.domain.vo.supplier_list_vo import SupplierListVO
from app.domain.vo.supplier_vo import SupplierVO
from app.repository.supplier_repository import SupplierRepository
from app.service.supplier_service import SupplierService

router = APIRouter(prefix="/api/v1/suppliers", tags=["供应商"])


def get_supplier_service() -> SupplierService:
    """为当前请求创建供应商服务及其数据库访问组件。"""
    return SupplierService(SupplierRepository())


SupplierServiceDep = Annotated[SupplierService, Depends(get_supplier_service)]


@router.post("", status_code=201, response_model=SupplierVO)
def create_supplier_endpoint(
    command: CreateSupplierDTO,
    actor: ActorDep,
    executor: CommandExecutorDep,
    service: SupplierServiceDep,
) -> Response:
    """创建供应商，通过命令编号安全重试同一次新增。"""
    result = executor.execute(
        command_id=command.command_id,
        operation="suppliers.create",
        payload=command.command_payload(),
        actor_id=actor.id,
        handler=lambda context: service.create_supplier(context, command),
    )
    return command_response(result)


@router.put("/{supplier_id}", response_model=SupplierVO)
def update_supplier_endpoint(
    supplier_id: UUID,
    command: UpdateSupplierDTO,
    actor: ActorDep,
    executor: CommandExecutorDep,
    service: SupplierServiceDep,
) -> Response:
    """锁定供应商来源，交由业务服务检查版本并更新资料。"""
    if supplier_id != command.supplier_id:
        raise AppError(422, "supplier_id_mismatch", "路径与请求中的供应商ID必须一致")
    result = executor.execute(
        command_id=command.command_id,
        operation="suppliers.update",
        payload=command.command_payload(),
        actor_id=actor.id,
        sources=[SourceKeyDTO("supplier", str(supplier_id))],
        handler=lambda context: service.update_supplier(context, command),
    )
    return command_response(result)


@router.get("", response_model=SupplierListVO)
def list_suppliers_endpoint(
    query: Annotated[SupplierListQO, Query()],
    session: SessionDep,
    _auth: AuthDep,
    service: SupplierServiceDep,
) -> dict[str, object]:
    """查询当前分页，认证和数据库会话覆盖完整读取过程。"""
    return service.list_supplier(session, offset=query.offset, limit=query.limit)


@router.get("/{supplier_id}", response_model=SupplierVO)
def get_supplier_endpoint(
    supplier_id: UUID,
    session: SessionDep,
    _auth: AuthDep,
    service: SupplierServiceDep,
) -> dict[str, object]:
    """使用稳定 ID 读取供应商详情。"""
    return service.get_supplier(session, supplier_id)
