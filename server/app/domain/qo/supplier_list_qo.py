"""供应商列表的查询条件对象。"""

from pydantic import BaseModel, ConfigDict, Field


class SupplierListQO(BaseModel):
    """供应商列表查询条件，校验偏移量和单页条数。"""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=20, ge=1, le=100)
