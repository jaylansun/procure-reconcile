"""供应商列表的接口展示对象。"""

from pydantic import BaseModel, Field

from app.domain.vo.supplier_vo import SupplierVO


class SupplierListVO(BaseModel):
    """供应商分页展示对象，返回当前页记录和分页参数。"""

    items: list[SupplierVO]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
