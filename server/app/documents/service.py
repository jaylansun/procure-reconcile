"""把文件存储、PG引用、命令幂等和审计连接起来，不承载采购业务。"""

import logging
import math
import os
import time
import unicodedata
from pathlib import PurePath, PureWindowsPath
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.documents.storage import FileStore, StagedFile, StorageError
from app.errors import AppError
from app.persistence.database import Database
from app.persistence.models import StoredFile
from app.services.audit import AuditChange
from app.services.command_executor import CommandContext, CommandExecutor, CommandResult
from app.services.locks import SourceKey, lock_sources

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _media_type(filename: str) -> str:
    """校验不含路径的原始名称，只按已确认后缀生成服务端媒体类型。"""
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or PureWindowsPath(filename).drive
        or any(unicodedata.category(character).startswith("C") for character in filename)
    ):
        raise AppError(422, "invalid_filename", "文件名格式不正确")
    try:
        filename.encode("utf-8")
    except UnicodeError:
        raise AppError(422, "invalid_filename", "文件名格式不正确") from None
    media_type = _MEDIA_TYPES.get(PurePath(filename).suffix.lower())
    if media_type is None:
        raise AppError(422, "unsupported_file_type", "不支持此文件类型")
    return media_type


def _metadata(row: StoredFile) -> dict[str, object]:
    """只返回公开标识与元数据，不返回物理路径或操作者秘密。"""
    return {
        "file_id": str(row.file_id),
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "original_name": row.original_name,
        "media_type": row.media_type,
        "created_at": row.created_at.isoformat(),
    }


def _storage_error(error: StorageError, *, reading: bool = False) -> AppError:
    """将底层存储错误转成固定安全提示，不拼接原始异常和物理路径。"""
    if error.code == "file_too_large":
        return AppError(413, "file_too_large", "文件超过允许的大小")
    if reading:
        return AppError(503, "file_unavailable", "文件暂不可用，请检查存储")
    return AppError(500, "file_storage_error", "文件存储操作失败")


class FileService:
    """以PG内容锁保护对象引用与回收，调用方仍须finally discard暂存文件。"""

    def __init__(self, database: Database, store: FileStore, executor: CommandExecutor) -> None:
        self.database = database
        self.store = store
        self.executor = executor

    def _sessions(self) -> sessionmaker:
        """数据库未配置时拒绝执行，不猜测本机或其他环境的连接。"""
        if self.database.sessions is None:
            raise AppError(503, "database_not_configured", "数据库尚未配置")
        return self.database.sessions

    def import_staged(
        self,
        staged: StagedFile,
        *,
        command_id: UUID,
        filename: str,
        actor_id: int,
    ) -> CommandResult:
        """导入已完成暂存的内容；不消费请求流，不清理调用方的staged。

        发布是幂等且受同一内容锁保护的文件副作用；若SQL事务失败，事务结束后
        重新取得内容锁再查引用。补偿失败时宁可保留可回收孤儿，不覆盖原异常。
        """
        media_type = _media_type(filename)
        if not isinstance(staged, StagedFile) or type(staged.size_bytes) is not int:
            raise AppError(422, "invalid_file", "暂存文件信息不正确")
        if staged.size_bytes <= 0:
            raise AppError(422, "empty_file", "不能导入空文件")
        payload = {
            "sha256": staged.sha256,
            "size_bytes": staged.size_bytes,
            "original_name": filename,
            "media_type": media_type,
        }
        source = SourceKey("file-content", staged.sha256)
        publish_attempted = False

        def import_content(context: CommandContext) -> CommandResult:
            """在命令执行器持有内容锁的事务中发布对象并保存唯一引用。"""
            nonlocal publish_attempted
            row = context.session.scalar(
                select(StoredFile).where(StoredFile.sha256 == staged.sha256)
            )
            reused = row is not None
            if row is not None:
                self._verify_existing(row, staged.size_bytes)
            else:
                # 标记放在调用前，连已创建目录项但同步失败的情况也走安全补偿。
                publish_attempted = True
                stored = self.store.publish(staged)
                row = StoredFile(
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    original_name=filename,
                    media_type=media_type,
                    created_by_id=actor_id,
                )
                context.session.add(row)
                context.session.flush()
            context.audit(
                AuditChange(
                    target_type="file",
                    target_id=str(row.file_id),
                    details={
                        "sha256": row.sha256,
                        "size_bytes": row.size_bytes,
                        "reused": reused,
                    },
                )
            )
            return CommandResult(200 if reused else 201, _metadata(row))

        try:
            return self.executor.execute(
                command_id=command_id,
                operation="files.upload",
                payload=payload,
                actor_id=actor_id,
                handler=import_content,
                sources=[source],
            )
        except BaseException as error:
            if publish_attempted:
                self._compensate(staged.sha256)
            if isinstance(error, StorageError):
                raise _storage_error(error) from None
            raise

    def _verify_existing(self, row: StoredFile, expected_size: int) -> None:
        """复用元数据前验证物理内容及大小，损坏不能被去重掩盖。"""
        if row.size_bytes != expected_size:
            raise AppError(503, "file_unavailable", "文件暂不可用，请检查存储")
        stream = self._open_content(row.sha256, row.size_bytes)
        stream.close()

    def _open_content(self, digest: str, expected_size: int) -> BinaryIO:
        """存储先流式校验摘要，再验证元数据长度，返回调用方负责关闭的句柄。"""
        try:
            stream = self.store.open(digest)
            try:
                if os.fstat(stream.fileno()).st_size != expected_size:
                    raise AppError(503, "file_unavailable", "文件暂不可用，请检查存储")
                return stream
            except BaseException:
                stream.close()
                raise
        except StorageError as error:
            raise _storage_error(error, reading=True) from None
        except OSError:
            raise AppError(503, "file_unavailable", "文件暂不可用，请检查存储") from None

    def metadata(self, file_id: UUID) -> dict[str, object]:
        """读取公开元数据并结束数据库事务，不打开原件。"""
        if not isinstance(file_id, UUID):
            raise AppError(422, "invalid_file_id", "文件标识不正确")
        with self._sessions()() as session:
            row = session.get(StoredFile, file_id)
            if row is None:
                raise AppError(404, "file_not_found", "文件不存在")
            return _metadata(row)

    def open_file(self, file_id: UUID) -> tuple[dict[str, object], BinaryIO]:
        """先关闭查询事务，再读取并校验原件；调用方须关闭返回句柄。"""
        metadata = self.metadata(file_id)
        return metadata, self._open_content(str(metadata["sha256"]), int(metadata["size_bytes"]))

    def _remove_if_unreferenced(self, digest: str) -> bool:
        """重新取得PG内容锁后确认无引用，绝不凭旧事务的结果删除文件。"""
        with self._sessions().begin() as session:
            lock_sources(
                session,
                [SourceKey("file-content", digest)],
                timeout_ms=self.executor.lock_timeout_ms,
            )
            referenced = session.scalar(
                select(StoredFile.file_id).where(StoredFile.sha256 == digest)
            )
            if referenced is not None:
                return False
            return self.store.remove(digest)

    def _compensate(self, digest: str) -> None:
        """事务后尽力回收无引用对象，失败只记录安全类型与错误码。"""
        try:
            self._remove_if_unreferenced(digest)
        except Exception as error:
            logger.warning(
                "file_cleanup_failed error_type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", "unavailable"),
            )

    def cleanup_orphans(self, min_age_seconds: float = 86400) -> int:
        """仅供显式维护命令调用，按年龄宽限和内容锁回收无引用对象。"""
        if (
            isinstance(min_age_seconds, bool)
            or not isinstance(min_age_seconds, (int, float))
            or not math.isfinite(min_age_seconds)
            or min_age_seconds < 0
        ):
            raise ValueError("孤儿文件宽限时间必须为非负有限秒数")
        self._sessions()
        removed = 0
        now = time.time()
        try:
            for digest, modified_at in self.store.iter_objects():
                if now - modified_at < min_age_seconds:
                    continue
                if self._remove_if_unreferenced(digest):
                    removed += 1
        except StorageError as error:
            raise _storage_error(error) from None
        return removed
