from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.http import CommandDTO, VersionedCommandDTO


class SupplierDTO(BaseModel):
    """新增和修改共用的供应商完整资料。"""

    model_config = ConfigDict(
        extra= "forbid",
        str_strip_whitespace=True,
    )

    name: str = Field(
        min_length=1,
        max_length=200,
    )
    contact_info: str | None = Field(
        default=None,
        max_length=2000,
    )

    @field_validator("name","contact_info")
    def validate_text(cls,value:str | None) -> str | None:
        """拒绝PostgreSQL文本无法保存的空字符和无效Unicode。"""
        if value is None:
            return None
        if "\0" in value:
            raise ValueError("供应商名称或联系信息不能包含空字符")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("供应商资料必须是有效的UTF-8文本") from error
        return value

class CreateSupplierDTO(CommandDTO):
    """新增命令：包含命令编号和供应商资料。"""

    data: SupplierDTO


class UpdateSupplierDTO(VersionedCommandDTO):
    """修改命令：还须提供供应商ID和预期版本。"""

    supplier_id: UUID
    data: SupplierDTO


class SupplierListQO(BaseModel):
    """供应商列表查询条件，校验偏移量和单页条数。"""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=20, ge=1, le=100)


class SupplierVO(BaseModel):
    """供应商展示对象，返回稳定标识、资料、版本和时间。"""

    id: UUID
    name: str
    contact_info: str | None
    version: int = Field(ge=1)
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})


class SupplierListVO(BaseModel):
    """供应商分页展示对象，返回当前页记录和分页参数。"""

    items: list[SupplierVO]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
