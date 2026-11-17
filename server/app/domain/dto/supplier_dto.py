"""新增和修改供应商共用的资料传输对象。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupplierDTO(BaseModel):
    """新增和修改共用的供应商完整资料。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    contact_info: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "contact_info")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        """拒绝 PostgreSQL 文本无法保存的空字符和无效 Unicode。"""
        if value is None:
            return None
        if "\0" in value:
            raise ValueError("供应商名称或联系信息不能包含空字符")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("供应商资料必须是有效的UTF-8文本") from error
        return value
