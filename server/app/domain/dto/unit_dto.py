"""新增和修改单位共用的资料传输对象。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UnitDTO(BaseModel):
    """接收完整的单位资料，并校验名称"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str = Field(min_length=1, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """拒绝 PostgreSQL 无法保存的空字符和无效 Unicode。"""
        if "\0" in value:
            raise ValueError("单位名称不能包含空字符")

        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("单位名称必须是有效的UTF-8文本") from error

        return value
