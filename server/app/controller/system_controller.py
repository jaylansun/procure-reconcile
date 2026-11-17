"""存活检查、系统状态和已登录工作区的 HTTP 接口。"""

from fastapi import APIRouter, Request

from app.common.dependencies import AuthDep
from app.domain.vo.system_vo import SystemVO
from app.service.readiness_service import ReadinessService

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """返回进程存活状态。"""
    return {"status": "alive"}


@router.get("/api/v1/system", response_model=SystemVO)
def system(request: Request) -> SystemVO:
    """返回配置、数据库就绪与维护状态。"""
    configured = request.app.state.settings
    status = ReadinessService.check(configured, request.app.state.database)
    maintenance = request.app.state.maintenance_gate.is_maintenance()
    return SystemVO(
        environment=configured.environment,
        instance_id=configured.instance_id or "unconfigured",
        app_version=request.app.version,
        api_version="1",
        ready=status.ready and not maintenance,
        reason="maintenance" if maintenance else status.reason,
    )


@router.get("/api/v1/workspace")
def workspace(request: Request, auth: AuthDep) -> dict[str, str]:
    """返回已登录工作区的连接信息。"""
    configured = request.app.state.settings
    return {
        "message": "连接与登录已就绪",
        "environment": configured.environment,
        "instance_id": configured.instance_id,
        "username": auth.user.username,
        "database": "connected",
    }
