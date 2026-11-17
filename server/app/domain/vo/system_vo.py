"""接口返回的系统运行状态。"""

from pydantic import BaseModel


class SystemVO(BaseModel):
    """系统状态展示对象，返回环境、实例及服务就绪状态。"""

    environment: str
    instance_id: str
    app_version: str
    api_version: str
    ready: bool
    reason: str | None
