"""本地不可变内容存储；数据库引用和清理由上层事务协调。"""

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_DIGEST = re.compile(r"[0-9a-f]{64}")
_STAGED_NAME = re.compile(r"[0-9a-f]{32}\.stage")
_CHUNK_SIZE = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class StorageError(ValueError):
    """带稳定错误码与安全提示的存储异常，不暴露本机路径。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class FileTooLarge(StorageError):
    """流式上传超过配置上限，供接口映射为413。"""

    def __init__(self) -> None:
        super().__init__("file_too_large", "文件超过允许的大小")


@dataclass(frozen=True)
class StagedFile:
    """已完整写入并同步的暂存文件，只能交回同一存储目录。"""

    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StoredObject:
    """已持久化的内容对象；created表示本次是否首次发布。"""

    path: Path
    sha256: str
    size_bytes: int
    created: bool


class FileStore:
    """以SHA256命名内容文件，不接受客户提供的对象路径。"""

    def __init__(self, root: Path, max_bytes: int = 25 * 1024 * 1024) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise StorageError("invalid_limit", "文件大小上限必须为正整数")
        self.root = Path(os.path.abspath(root))
        self.max_bytes = max_bytes
        self._staging = self.root / ".staging"
        self._objects = self.root / "objects"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._directories(create=True):
                pass
        except OSError:
            raise StorageError("io_error", "无法准备文件存储") from None

    @contextmanager
    def _directories(self, *, create: bool = False) -> Iterator[tuple[int, int]]:
        """逐层打开目录，后续操作通过目录描述符拒绝符号链接。"""
        descriptors: list[int] = []
        try:
            root_fd = self._open_directory(self.root)
            descriptors.append(root_fd)
            for name in (".staging", "objects"):
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=root_fd)
                    except FileExistsError:
                        pass
                child_fd = self._open_directory(name, dir_fd=root_fd)
                descriptors.append(child_fd)
            if create:
                os.fsync(root_fd)
            yield descriptors[1], descriptors[2]
        except OSError:
            raise StorageError("io_error", "文件存储操作失败") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _open_directory(path: Path | str, *, dir_fd: int | None = None) -> int:
        """拒绝目录本身为符号链接或普通文件。"""
        try:
            return os.open(path, _DIRECTORY_FLAGS, dir_fd=dir_fd)
        except OSError:
            # lstat不跟随链接，只用于将不安全目录与其他IO故障区分。
            try:
                info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                raise
            if not stat.S_ISDIR(info.st_mode):
                raise StorageError("unsafe_path", "存储目录类型不安全") from None
            raise

    @staticmethod
    def _validate_digest(digest: str) -> None:
        """对象名只允许64位小写十六进制摘要。"""
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise StorageError("invalid_digest", "文件标识不合法")

    def _staged_name(self, staged: StagedFile) -> str:
        """暂存路径必须是本存储生成的直接子文件，不能越级或跳目录。"""
        if (
            not isinstance(staged, StagedFile)
            or not isinstance(staged.path, Path)
            or staged.path.parent != self._staging
            or _STAGED_NAME.fullmatch(staged.path.name) is None
        ):
            raise StorageError("invalid_staged", "暂存文件标识不合法")
        return staged.path.name

    @staticmethod
    def _create_temporary(staging_fd: int) -> tuple[str, BinaryIO]:
        """独占创建同文件系统的随机暂存文件。"""
        while True:
            name = f"{secrets.token_hex(16)}.stage"
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=staging_fd,
                )
                return name, os.fdopen(descriptor, "wb", buffering=0)
            except FileExistsError:
                continue

    @staticmethod
    def _delete_temporary(staging_fd: int, name: str) -> None:
        """删除临时目录项；不存在时重复清理仍成功。"""
        try:
            os.unlink(name, dir_fd=staging_fd)
        except FileNotFoundError:
            return
        os.fsync(staging_fd)

    @staticmethod
    def _write_all(file: BinaryIO, chunk: bytes) -> None:
        """处理短写，不把整次上传收集到内存。"""
        remaining = memoryview(chunk)
        while remaining:
            written = file.write(remaining)
            if not written:
                raise StorageError("io_error", "文件写入失败")
            remaining = remaining[written:]

    @staticmethod
    def _open_regular(directory_fd: int, name: str) -> BinaryIO:
        """仅打开普通文件，符号链接和管道等特殊文件均拒绝。"""
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            raise StorageError("not_found", "文件不存在") from None
        except OSError:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("unsafe_path", "文件类型不安全") from None
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise StorageError("unsafe_path", "文件类型不安全")
        return os.fdopen(descriptor, "rb")

    def stage(self, chunks: Iterable[bytes]) -> StagedFile:
        """逐块限额写入并计算摘要；任何失败均清理本次临时文件。"""
        with self._directories() as (staging_fd, _):
            name, output = self._create_temporary(staging_fd)
            try:
                digest = hashlib.sha256()
                size = 0
                with output:
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise StorageError("invalid_chunk", "文件内容必须为字节块")
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FileTooLarge()
                        self._write_all(output, chunk)
                        digest.update(chunk)
                    os.fsync(output.fileno())
                os.fsync(staging_fd)
                return StagedFile(self._staging / name, digest.hexdigest(), size)
            except BaseException as error:
                output.close()
                self._delete_temporary(staging_fd, name)
                if isinstance(error, (StorageError, OSError, KeyboardInterrupt, SystemExit)):
                    raise
                raise StorageError("invalid_chunks", "文件内容读取失败") from None

    def publish(self, staged: StagedFile) -> StoredObject:
        """校验后原子发布；保留staged，调用方须在finally中discard。"""
        name = self._staged_name(staged)
        self._validate_digest(staged.sha256)
        if (
            not isinstance(staged.size_bytes, int)
            or isinstance(staged.size_bytes, bool)
            or staged.size_bytes < 0
        ):
            raise StorageError("invalid_staged", "暂存文件大小不合法")
        if staged.size_bytes > self.max_bytes:
            raise FileTooLarge()
        with self._directories() as (staging_fd, objects_fd):
            # 另建发布副本，避免原staged路径在发布后被修改而改变内容对象。
            temporary_name, output = self._create_temporary(staging_fd)
            try:
                digest = hashlib.sha256()
                size = 0
                with output, self._open_regular(staging_fd, name) as source:
                    while chunk := source.read(_CHUNK_SIZE):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FileTooLarge()
                        self._write_all(output, chunk)
                        digest.update(chunk)
                    if digest.hexdigest() != staged.sha256 or size != staged.size_bytes:
                        raise StorageError("corrupt_staged", "暂存文件校验失败")
                    os.fchmod(output.fileno(), 0o400)
                    os.fsync(output.fileno())
                try:
                    # link是同文件系统的原子、不覆盖操作；并发同内容仅一方成功。
                    os.link(
                        temporary_name,
                        staged.sha256,
                        src_dir_fd=staging_fd,
                        dst_dir_fd=objects_fd,
                        follow_symlinks=False,
                    )
                    created = True
                except FileExistsError:
                    created = False
                with self._open_verified(objects_fd, staged.sha256) as stored:
                    if os.fstat(stored.fileno()).st_size != size:
                        raise StorageError("corrupt_object", "已存文件校验失败")
                    os.fsync(stored.fileno())
                os.fsync(objects_fd)
                return StoredObject(self._objects / staged.sha256, staged.sha256, size, created)
            finally:
                output.close()
                self._delete_temporary(staging_fd, temporary_name)

    def _open_verified(self, objects_fd: int, digest: str) -> BinaryIO:
        """流式校验对象，返回已回到文件开头的只读句柄。"""
        source = self._open_regular(objects_fd, digest)
        try:
            actual = hashlib.sha256()
            while chunk := source.read(_CHUNK_SIZE):
                actual.update(chunk)
            if actual.hexdigest() != digest:
                raise StorageError("corrupt_object", "已存文件校验失败")
            source.seek(0)
            return source
        except BaseException:
            source.close()
            raise

    def open(self, sha256: str) -> BinaryIO:
        """校验摘要后打开只读文件，调用方用with关闭句柄。"""
        self._validate_digest(sha256)
        with self._directories() as (_, objects_fd):
            return self._open_verified(objects_fd, sha256)

    def read(self, sha256: str) -> bytes:
        """读取已校验的完整内容；大文件可用open避免一次加载到内存。"""
        with self.open(sha256) as source:
            try:
                return source.read()
            except OSError:
                raise StorageError("io_error", "文件读取失败") from None

    def discard(self, staged: StagedFile) -> None:
        """幂等删除合法暂存文件，不影响已经发布的对象。"""
        name = self._staged_name(staged)
        with self._directories() as (staging_fd, _):
            try:
                with self._open_regular(staging_fd, name):
                    pass
            except StorageError as error:
                if error.code == "not_found":
                    return
                raise
            self._delete_temporary(staging_fd, name)

    def remove(self, sha256: str) -> bool:
        """仅删对象；上层必须先在PG内容锁下确认没有引用。"""
        self._validate_digest(sha256)
        with self._directories() as (_, objects_fd):
            try:
                with self._open_regular(objects_fd, sha256):
                    pass
            except StorageError as error:
                if error.code == "not_found":
                    return False
                raise
            try:
                os.unlink(sha256, dir_fd=objects_fd)
            except FileNotFoundError:
                return False
            os.fsync(objects_fd)
            return True

    def iter_objects(self) -> Iterator[tuple[str, float]]:
        """返回摘要与修改时间的快照供孤儿回收，不扫描暂存文件。"""
        objects: list[tuple[str, float]] = []
        with self._directories() as (_, objects_fd), os.scandir(objects_fd) as entries:
            for entry in entries:
                self._validate_digest(entry.name)
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise StorageError("unsafe_path", "文件类型不安全")
                objects.append((entry.name, info.st_mtime))
        return iter(objects)
