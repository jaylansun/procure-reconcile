"""单个供应商的接口展示对象。"""

from uuid import UUID

from pydantic import BaseModel, Field


class SupplierVO(BaseModel):
    """供应商展示对象，返回稳定标识、资料、版本和时间。"""

    id: UUID
    name: str
    contact_info: str | None
    version: int = Field(ge=1)
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})
