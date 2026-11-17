"""计量单位列表的接口展示对象。"""

from pydantic import BaseModel, Field

from app.domain.vo.unit_vo import UnitVO


class UnitListVO(BaseModel):
    """单位分页展示对象，返回当前页记录和分页参数。"""

    items: list[UnitVO]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
