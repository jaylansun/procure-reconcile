"""原件上传、元数据与受认证下载；本阶段不宣称已完成内容解析。"""

import logging
from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Annotated, BinaryIO
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api import ActorDep
from app.documents.service import FileService
from app.documents.storage import FileStore, StagedFile, StorageError
from app.errors import AppError
from app.services.http import command_response

logger = logging.getLogger("procure.files")
router = APIRouter(prefix="/api/v1/files", tags=["原件"])
_CHUNK_SIZE = 1024 * 1024


class FileMetadata(BaseModel):
    """对外的原件元数据；物理目录和内部操作者字段不出现在接口中。"""

    file_id: UUID
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_name: str
    media_type: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})


def _service(request: Request) -> FileService:
    """延迟准备附件目录，文件系统故障不妨碍独立存活接口。"""
    settings = request.app.state.settings
    try:
        store = FileStore(settings.files_dir, max_bytes=settings.max_file_bytes)
    except StorageError as error:
        raise AppError(503, "file_storage_unavailable", "附件存储暂不可用") from error
    return FileService(request.app.state.database, store, request.app.state.command_executor)


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    """在工作线程中分块读取暂存流，内存不随整份文件大小增长。"""
    while chunk := stream.read(_CHUNK_SIZE):
        yield chunk


@router.post(
    "",
    status_code=201,
    response_model=FileMetadata,
    responses={
        200: {"model": FileMetadata, "description": "复用已有内容，或重放原200结果"},
        409: {"description": "命令冲突或相关资源正忙"},
        413: {"description": "超过配置的单文件大小"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_file(
    request: Request,
    actor: ActorDep,
    command_id: Annotated[UUID, Header(alias="X-Command-ID")],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> Response:
    """请求体直接发送文件字节；文件名只作显示，不接收客户文件系统路径。"""
    # 上传暂存和finally清理都属于附件写入，必须一起等待维护排空。
    with request.app.state.maintenance_gate.writer():
        return await _receive_upload(request, actor, command_id, filename)


async def _receive_upload(
    request: Request, actor: ActorDep, command_id: UUID, filename: str
) -> Response:
    """在已持有写入门禁时接收、暂存并导入原件。"""
    service = _service(request)
    limit = request.app.state.settings.max_file_bytes
    length_header = request.headers.get("content-length")
    if length_header is not None:
        if not length_header.isdecimal():
            raise AppError(400, "invalid_content_length", "文件长度标头不合法")
        if int(length_header) > limit:
            raise AppError(413, "file_too_large", "文件超过允许的大小")
    staged: StagedFile | None = None
    try:
        # 小文件留在内存，超过1MiB后自动转临时文件；网络接收在SQL事务外。
        with SpooledTemporaryFile(max_size=_CHUNK_SIZE, mode="w+b") as spool:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit:
                    raise AppError(413, "file_too_large", "文件超过允许的大小")
                await run_in_threadpool(spool.write, chunk)
            spool.seek(0)
            staged = await run_in_threadpool(service.store.stage, _chunks(spool))
        result = await run_in_threadpool(
            service.import_staged,
            staged,
            command_id=command_id,
            filename=filename,
            actor_id=actor.id,
        )
        return command_response(result)
    except ClientDisconnect as error:
        raise AppError(400, "upload_interrupted", "文件上传已中断") from error
    except StorageError as error:
        status = 413 if error.code == "file_too_large" else 503
        raise AppError(status, error.code, error.message) from error
    finally:
        if staged is not None:
            try:
                await run_in_threadpool(service.store.discard, staged)
            except StorageError as error:
                # 只记录安全错误码，清理失败不覆盖已提交结果或原业务异常。
                logger.warning("staged_cleanup_failed code=%s", error.code)


@router.get("/{file_id}", response_model=FileMetadata)
def file_metadata(file_id: UUID, request: Request, actor: ActorDep) -> dict[str, object]:
    """返回可关联的原件标识与元数据，不泄露服务端物理路径。"""
    return _service(request).metadata(file_id)


@router.get(
    "/{file_id}/content",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "按原件媒体类型返回流式内容，文件名在Content-Disposition中",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def download_file(file_id: UUID, request: Request, actor: ActorDep) -> Response:
    """先关闭数据库会话，再流式发送已校验的不可变原件。"""
    metadata, stream = _service(request).open_file(file_id)

    def content() -> Iterator[bytes]:
        try:
            yield from _chunks(stream)
        finally:
            stream.close()

    disposition = f"attachment; filename*=UTF-8''{quote(str(metadata['original_name']), safe='')}"
    return StreamingResponse(
        content(),
        media_type=str(metadata["media_type"]),
        headers={
            "Content-Length": str(metadata["size_bytes"]),
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        },
    )
