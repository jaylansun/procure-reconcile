"""真实文件锁验证持久门禁，使用独立临时目录，不读取运行环境。"""

import json
import os
import select
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.errors import AppError
from app.maintenance.gate import MaintenanceGate


def _assert_maintenance(error: pytest.ExceptionInfo[AppError]) -> None:
    assert (error.value.status, error.value.code) == (503, "maintenance")


def test_missing_state_is_open_without_constructor_side_effects(tmp_path: Path) -> None:
    """未首次写入的环境没有公共全局目录，也不因查询状态而创建目录。"""
    directory = tmp_path / "instance" / "maintenance"
    gate = MaintenanceGate(directory)
    assert not directory.exists()
    assert not gate.is_maintenance()
    assert not directory.exists()
    with gate.writer():
        assert directory.is_dir()
    assert not gate.marker_path.exists()
    assert stat.S_IMODE((directory / "writers.lock").stat().st_mode) == 0o600


def test_state_is_persistent_private_and_explicitly_cleared(tmp_path: Path) -> None:
    """进程重新创建gate仍看到维护；只有受控显式操作才解除。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.set_maintenance(True, release_id="release-中文")
    assert MaintenanceGate(gate.directory).is_maintenance()
    body = json.loads(gate.marker_path.read_text(encoding="utf-8"))
    assert body["active"] is True
    assert body["release_id"] == "release-中文"
    assert stat.S_IMODE(gate.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(gate.marker_path.stat().st_mode) == 0o600
    with gate.exclusive():
        gate.set_maintenance(False, release_id="release-中文")
    assert not MaintenanceGate(gate.directory).is_maintenance()
    with gate.writer():
        pass
    assert sorted(path.name for path in gate.directory.iterdir()) == [
        "maintenance.json",
        "writers.lock",
    ]


def test_maintenance_rejects_new_writer_before_body(tmp_path: Path) -> None:
    """维护中的请求不能进入业务回调，也不创建无意义的锁文件。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.set_maintenance(True)
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("维护中的写入不应执行")
    _assert_maintenance(error)
    assert not (gate.directory / "writers.lock").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"[]",
        b"{}",
        b'{"active": 1}',
        b'{"active": "false"}',
        b'{"active": false, "extra": NaN}',
        b'{"active": true, "active": false}',
        b'{"active": false, "extra": "\xff"}',
        b'{"active": false, "extra": "' + b"x" * 17000 + b'"}',
        b'{"active": false, "extra": ' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    ],
    ids=[
        "empty",
        "truncated",
        "array",
        "missing-active",
        "numeric-active",
        "string-active",
        "nonstandard-constant",
        "duplicate-active",
        "invalid-utf8",
        "oversized",
        "deep-extra",
    ],
)
def test_bad_markers_fail_closed(tmp_path: Path, content: bytes) -> None:
    """损坏、歧义、超大或不合法状态不能被当作正常开放。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.directory.mkdir()
    gate.marker_path.write_bytes(content)
    assert gate.is_maintenance()
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("坏标记不能开放写入")
    _assert_maintenance(error)


def test_bad_regular_marker_can_be_repaired_into_maintenance(tmp_path: Path) -> None:
    """持有发布目标锁的控制器可把坏JSON原子修复为明确停写状态。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.directory.mkdir()
    gate.marker_path.write_bytes(b"broken")
    gate.set_maintenance(True, release_id="repair")
    assert gate.is_maintenance()
    with gate.exclusive():
        pass


def test_marker_symbolic_link_is_never_followed_or_replaced(tmp_path: Path) -> None:
    """拒绝符号链接，不读取或改写链接指向的其他文件。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"active":false}', encoding="utf-8")
    gate.marker_path.symlink_to(outside)
    assert gate.is_maintenance()
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("符号链接标记不能开放写入")
    _assert_maintenance(error)
    with pytest.raises(ValueError, match="类型不安全"):
        gate.set_maintenance(True)
    assert outside.read_text(encoding="utf-8") == '{"active":false}'
    assert gate.marker_path.is_symlink()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_unsafe_directory_is_closed(tmp_path: Path, kind: str) -> None:
    """维护根目录必须是真实目录，不能是普通文件或符号链接。"""
    directory = tmp_path / "maintenance"
    if kind == "file":
        directory.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "outside"
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
    gate = MaintenanceGate(directory)
    assert gate.is_maintenance()
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("不安全的目录不能开放写入")
    _assert_maintenance(error)


def test_unsafe_lock_is_closed(tmp_path: Path) -> None:
    """不能通过锁文件符号链接把不同实例的锁混在一起。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.directory.mkdir()
    target = tmp_path / "outside.lock"
    target.write_bytes(b"unchanged")
    (gate.directory / "writers.lock").symlink_to(target)
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("不安全的锁不能开放写入")
    _assert_maintenance(error)
    assert target.read_bytes() == b"unchanged"


def test_fifo_marker_is_rejected_without_blocking(tmp_path: Path) -> None:
    """状态文件被替换成FIFO时也不能使API读取无限阻塞。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.directory.mkdir()
    os.mkfifo(gate.marker_path)
    assert gate.is_maintenance()


def test_unreadable_marker_is_closed_without_leaking_path(tmp_path: Path, monkeypatch) -> None:
    """底层权限故障只变成固定维护提示，不返回私有目录或异常内容。"""
    gate = MaintenanceGate(tmp_path / "private-maintenance")

    def unreadable(*args, **kwargs):
        raise PermissionError("private-path private-password")

    monkeypatch.setattr(gate, "_read_active", unreadable)
    gate.directory.mkdir()
    assert gate.is_maintenance()
    with pytest.raises(AppError) as error, gate.writer():
        pass
    _assert_maintenance(error)
    assert "private" not in error.value.message


def test_existing_writers_drain_before_exclusive_and_new_writers_fail(tmp_path: Path) -> None:
    """先标记再排空：旧写入完成前不能开始备份，新写入已被拒绝。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    writer_entered, finish_writer, maintenance_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    failures = []

    def write() -> None:
        try:
            with gate.writer():
                writer_entered.set()
                assert finish_writer.wait(5)
        except BaseException as error:
            failures.append(error)

    def maintain() -> None:
        try:
            with gate.exclusive(timeout_seconds=3):
                maintenance_entered.set()
        except BaseException as error:
            failures.append(error)

    writer = threading.Thread(target=write)
    writer.start()
    assert writer_entered.wait(3)
    gate.set_maintenance(True)
    controller = threading.Thread(target=maintain)
    controller.start()
    try:
        assert not maintenance_entered.wait(0.1)
        with pytest.raises(AppError) as error, gate.writer():
            pytest.fail("新写入必须在排空期间被拒绝")
        _assert_maintenance(error)
    finally:
        finish_writer.set()
        writer.join(timeout=4)
        controller.join(timeout=4)
    assert maintenance_entered.is_set()
    assert not writer.is_alive() and not controller.is_alive()
    assert failures == []


def test_multiple_writers_can_hold_shared_locks(tmp_path: Path) -> None:
    """正常业务写入可并发，共享门禁不能把所有请求无谓串行化。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    with gate.writer(), MaintenanceGate(gate.directory).writer(timeout_seconds=0):
        assert not gate.is_maintenance()


def test_business_exception_is_preserved_and_releases_lock(tmp_path: Path) -> None:
    """业务OSError原样传播；门禁清理不能误改成维护错误或遗留锁。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    failure = OSError("business failure")
    with pytest.raises(OSError) as error, gate.writer():
        gate.set_maintenance(True)
        raise failure
    assert error.value is failure
    with gate.exclusive(timeout_seconds=0):
        pass


def test_exclusive_exception_keeps_maintenance_and_releases_lock(tmp_path: Path) -> None:
    """备份或迁移失败不能自动开放服务，锁则可由受控恢复流程重新取得。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.set_maintenance(True)
    with pytest.raises(RuntimeError), gate.exclusive():
        raise RuntimeError("candidate failed")
    assert gate.is_maintenance()
    with gate.exclusive(timeout_seconds=0):
        pass


def test_exclusive_requires_durable_maintenance_first(tmp_path: Path) -> None:
    """控制器不能先等独占再阻止新写入，否则无法可靠排空。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    with pytest.raises(ValueError, match="先启用维护"), gate.exclusive():
        pytest.fail("没有维护标记不得开始备份")


def test_writer_rechecks_marker_after_lock_acquisition(tmp_path: Path, monkeypatch) -> None:
    """覆盖预检查和加锁之间刚进入维护的竞态。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    original_wait = gate._wait

    def enter_maintenance(*args, **kwargs):
        original_wait(*args, **kwargs)
        gate.set_maintenance(True)

    monkeypatch.setattr(gate, "_wait", enter_maintenance)
    with pytest.raises(AppError) as error, gate.writer():
        pytest.fail("加锁后已进入维护必须拒绝")
    _assert_maintenance(error)
    monkeypatch.setattr(gate, "_wait", original_wait)
    with gate.exclusive(timeout_seconds=0):
        pass


def test_failed_atomic_update_keeps_old_state_and_cleans_temporary(
    tmp_path: Path, monkeypatch
) -> None:
    """状态替换失败时仍保留旧维护标记，没有部分JSON或临时文件泄漏。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    gate.set_maintenance(True, release_id="old")
    original = gate.marker_path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("atomic replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        gate.set_maintenance(False)
    assert gate.marker_path.read_bytes() == original
    assert gate.is_maintenance()
    assert [path.name for path in gate.directory.iterdir()] == ["maintenance.json"]


@pytest.mark.parametrize("kill", [False, True])
def test_other_process_writer_is_drained_and_process_exit_releases_lock(
    tmp_path: Path, kill: bool
) -> None:
    """跨进程锁阻止过早快照；正常结束或进程被终止后锁都会释放。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    # 管道协调不依赖跨进程条件锁，强制退出子进程也不会遗留IPC信号量死锁。
    source = (
        "import sys\nfrom pathlib import Path\n"
        "from app.maintenance import MaintenanceGate\n"
        "with MaintenanceGate(Path(sys.argv[1])).writer():\n"
        "    print('entered', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", source, str(gate.directory)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "entered"
        gate.set_maintenance(True)
        with pytest.raises(TimeoutError), gate.exclusive(timeout_seconds=0.05):
            pytest.fail("其他进程仍在写入，不能开始备份")
        assert gate.is_maintenance()
        if kill:
            process.terminate()
        else:
            process.stdin.write("finish\n")
            process.stdin.flush()
        assert process.wait(timeout=5) == (-15 if kill else 0)
        with gate.exclusive(timeout_seconds=1):
            pass
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("timeout", [True, -1, float("nan"), float("inf"), "1", None])
@pytest.mark.parametrize("method", ["writer", "exclusive"])
def test_invalid_timeout_is_rejected_without_filesystem_changes(
    tmp_path: Path, timeout, method: str
) -> None:
    """非法等待配置不能悄悄变成无限等待。"""
    gate = MaintenanceGate(tmp_path / "maintenance")
    with pytest.raises(ValueError), getattr(gate, method)(timeout_seconds=timeout):
        pytest.fail("非法锁等待值不应执行")
    assert not gate.directory.exists()
