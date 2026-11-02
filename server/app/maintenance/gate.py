"""通过持久维护标记和共享文件锁协调API写入与一致备份。

普通写入持有共享flock；控制器先持久设置维护标记，再取得独占锁排空旧写入。
控制器必须另外持有发布目标锁，检查通过后才显式解除维护。两个容器须使用
同一个本项目命名卷以及相同运行UID；此模块不修改已有目录的归属或权限。
"""

import fcntl
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.errors import AppError

_STATE = "maintenance.json"
_LOCK = "writers.lock"
_MAX_STATE_BYTES = 16 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _maintenance_error() -> AppError:
    """对普通调用者只返回固定提示，不泄露本机路径或底层异常。"""
    return AppError(503, "maintenance", "服务维护中，请稍后重试")


def _timeout(value: float) -> float:
    """只允许非负有限等待时间，0表示只尝试一次。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("维护锁等待时间必须是非负有限秒数")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("维护锁等待时间必须是非负有限秒数")
    return float(value)


def _invalid_constant(value: str) -> None:
    """Python默认接受NaN/Infinity；维护标记只允许标准JSON。"""
    raise ValueError("维护状态文件不合法")


def _unique_fields(fields: list[tuple[str, object]]) -> dict[str, object]:
    """重复字段存在歧义，不能让后一个active=false静默覆盖前值。"""
    result: dict[str, object] = {}
    for key, value in fields:
        if key in result:
            raise ValueError("维护状态文件不合法")
        result[key] = value
    return result


class MaintenanceGate:
    """默认缺少标记为开放，坏标记或存储故障则拒绝新的普通写入。

    构造不触碰文件系统，因此附件卷故障也不会阻止独立存活探针启动。
    所有服务实例必须指向同一个维护目录；开发、测试和正式目录不得混用。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(os.path.abspath(directory))

    @property
    def marker_path(self) -> Path:
        """控制台可据此定位本项目状态文件，HTTP接口不返回此路径。"""
        return self.directory / _STATE

    def _directory(self, *, create: bool) -> int:
        if create:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        return os.open(self.directory, _DIRECTORY_FLAGS)

    @staticmethod
    def _read_active(directory_fd: int) -> bool:
        """只读取有界普通文件，缺失开放，其余错误留给调用方fail-closed。"""
        try:
            descriptor = os.open(
                _STATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        except FileNotFoundError:
            return False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE_BYTES:
                raise ValueError("维护状态文件不合法")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(_MAX_STATE_BYTES + 1)
            if len(content) > _MAX_STATE_BYTES:
                raise ValueError("维护状态文件不合法")
            body = json.loads(
                content.decode("utf-8"),
                parse_constant=_invalid_constant,
                object_pairs_hook=_unique_fields,
            )
            if (
                not isinstance(body, dict)
                or type(body.get("active")) is not bool
                or set(body) - {"active", "release_id", "updated_at"}
                or (body.get("release_id") is not None and not isinstance(body["release_id"], str))
                or ("updated_at" in body and not isinstance(body["updated_at"], str))
            ):
                raise ValueError("维护状态文件不合法")
            return body["active"]
        finally:
            os.close(descriptor)

    def is_maintenance(self) -> bool:
        """查询持久状态；缺失目录开放，不可读、坏JSON或不安全路径一律维护。"""
        try:
            directory_fd = self._directory(create=False)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True
        try:
            return self._read_active(directory_fd)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return True
        finally:
            os.close(directory_fd)

    @staticmethod
    def _lock(directory_fd: int) -> int:
        descriptor = os.open(
            _LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("维护锁文件不合法")
        return descriptor

    @staticmethod
    def _release(lock_fd: int | None, directory_fd: int | None) -> None:
        """关闭描述符释放flock，清理异常不覆盖业务回调原来的异常。"""
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in (lock_fd, directory_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _wait(
        self, lock_fd: int, mode: int, timeout_seconds: float, *, directory_fd: int, writing: bool
    ) -> None:
        """非阻塞轮询使新写者能及时观察维护标记，避免卡在排空窗口内。"""
        deadline = time.monotonic() + timeout_seconds
        while True:
            if writing and self._read_active(directory_fd):
                raise _maintenance_error()
            try:
                fcntl.flock(lock_fd, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待维护锁超时") from None
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    @contextmanager
    def writer(self, *, timeout_seconds: float = 5) -> Iterator[None]:
        """普通写入口持有共享锁；进入后业务异常原样传播，finally释放锁。

        先检查标记，再取得共享锁并复查。维护标记设置前已进入的写者可以结束，
        控制器的独占锁等待它们排空；标记设置后的新写者立即被拒绝。
        """
        timeout_seconds = _timeout(timeout_seconds)
        directory_fd = lock_fd = None
        try:
            directory_fd = self._directory(create=True)
            if self._read_active(directory_fd):
                raise _maintenance_error()
            lock_fd = self._lock(directory_fd)
            self._wait(
                lock_fd, fcntl.LOCK_SH, timeout_seconds, directory_fd=directory_fd, writing=True
            )
            if self._read_active(directory_fd):
                raise _maintenance_error()
        except BaseException as error:
            self._release(lock_fd, directory_fd)
            if isinstance(error, (OSError, ValueError, UnicodeError, RecursionError, AppError)):
                raise _maintenance_error() from None
            raise
        try:
            yield
        finally:
            self._release(lock_fd, directory_fd)

    @contextmanager
    def exclusive(self, *, timeout_seconds: float = 60) -> Iterator[None]:
        """受控发布入口先设维护，再独占等待所有旧写者结束。

        此锁不替代发布控制器的目标锁。异常或超时不会自动清除维护标记，
        因而候选失败、Jenkins中断或进程重启后仍保持停写。
        """
        timeout_seconds = _timeout(timeout_seconds)
        directory_fd = lock_fd = None
        try:
            directory_fd = self._directory(create=True)
            if not self._read_active(directory_fd):
                raise ValueError("取得维护独占锁前必须先启用维护")
            lock_fd = self._lock(directory_fd)
            self._wait(
                lock_fd, fcntl.LOCK_EX, timeout_seconds, directory_fd=directory_fd, writing=False
            )
            if not self._read_active(directory_fd):
                raise ValueError("维护状态已改变，不能继续维护操作")
        except BaseException:
            self._release(lock_fd, directory_fd)
            raise
        try:
            yield
        finally:
            self._release(lock_fd, directory_fd)

    def set_maintenance(self, active: bool, *, release_id: str | None = None) -> None:
        """原子保存维护状态并同步文件和目录，不自动解除维护或记录秘密。

        调用者须持有统一发布目标锁。启用在排空前执行；解除应在独占维护锁中、
        检查通过后执行。与API使用相同UID，避免0600标记变为其他账号所有。
        """
        if type(active) is not bool:
            raise ValueError("维护状态必须是布尔值")
        if release_id is not None:
            if (
                not isinstance(release_id, str)
                or not release_id.strip()
                or len(release_id) > 128
                or "\0" in release_id
            ):
                raise ValueError("发布编号必须是1至128个字符的非空白文本")
            try:
                release_id.encode("utf-8")
            except UnicodeError:
                raise ValueError("发布编号必须是有效UTF-8文本") from None
        content = json.dumps(
            {
                "active": active,
                "release_id": release_id,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        directory_fd = self._directory(create=True)
        temporary = f".{secrets.token_hex(16)}.tmp"
        descriptor = None
        try:
            try:
                existing = os.stat(_STATE, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ValueError("维护状态文件类型不安全")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            remaining = memoryview(content)
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise OSError("维护状态写入失败")
                remaining = remaining[count:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, _STATE, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
