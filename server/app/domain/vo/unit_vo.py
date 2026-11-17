"""单个计量单位的接口展示对象。"""

from uuid import UUID

from pydantic import BaseModel, Field


class UnitVO(BaseModel):
    """单位展示对象，返回稳定标识、名称、版本和时间。"""

    id: UUID
    name: str
    version: int = Field(ge=1)
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})
