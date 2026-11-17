"""提供给客户端的原件元数据。"""

from uuid import UUID

from pydantic import BaseModel, Field


class FileMetadataVO(BaseModel):
    """对外的原件元数据；物理目录和内部操作者字段不出现在接口中。"""

    file_id: UUID
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_name: str
    media_type: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})
