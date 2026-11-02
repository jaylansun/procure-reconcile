"""验证文件存储的流式边界、原子发布与路径安全，不连接数据库。"""

import errno
import hashlib
import os
import stat
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from app.documents.storage import FileStore, FileTooLarge, StagedFile, StorageError


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    """每个测试使用全新的存储目录，不读取应用运行配置。"""
    return FileStore(tmp_path / "files")


def test_stage_publish_read_and_discard_are_independent(store: FileStore) -> None:
    """原件按内容摘要发布，重复清理暂存不影响正式文件。"""
    content = "中文原件".encode()
    staged = store.stage([content[:4], b"", content[4:]])
    assert staged.sha256 == hashlib.sha256(content).hexdigest()
    assert staged.size_bytes == len(content)
    assert staged.path.read_bytes() == content

    stored = store.publish(staged)
    assert stored.created
    assert stored.path.name == staged.sha256
    assert stored.path.stat().st_dev == staged.path.stat().st_dev
    assert stored.path.stat().st_ino != staged.path.stat().st_ino
    assert stored.size_bytes == len(content)
    assert staged.path.exists()
    assert store.read(stored.sha256) == content
    with store.open(stored.sha256) as source:
        assert source.read(3) == content[:3]

    store.discard(staged)
    store.discard(staged)
    assert not staged.path.exists()
    assert store.read(stored.sha256) == content


def test_empty_file_has_valid_sha256(store: FileStore) -> None:
    """空文件仍是合法内容对象，大小和摘要可验证。"""
    staged = store.stage([])
    stored = store.publish(staged)
    store.discard(staged)
    assert stored.size_bytes == 0
    assert store.read(stored.sha256) == b""


def test_duplicate_content_never_replaces_the_existing_object(store: FileStore) -> None:
    """重复上传返回created=False，既有正确文件的inode和修改时间不变。"""
    first = store.stage([b"same"])
    published = store.publish(first)
    original = published.path.stat()
    second = store.stage([b"s", b"ame"])
    repeated = store.publish(second)
    assert not repeated.created
    assert repeated.path.stat().st_ino == original.st_ino
    assert repeated.path.stat().st_mtime_ns == original.st_mtime_ns
    assert first.path.exists() and second.path.exists()


def test_editing_staged_path_cannot_change_published_object(store: FileStore) -> None:
    """保留给调用方清理的暂存文件不会与正式对象共享可写内容。"""
    staged = store.stage([b"immutable"])
    stored = store.publish(staged)
    staged.path.write_bytes(b"changed")
    store.discard(staged)
    assert store.read(stored.sha256) == b"immutable"
    assert stored.path.stat().st_mode & 0o222 == 0


def test_two_instances_publish_same_content_concurrently(tmp_path: Path) -> None:
    """不同实例并发发布同内容只有一个新建者，其余不覆盖获胜文件。"""
    stores = [FileStore(tmp_path / "shared") for _ in range(8)]
    staged = [item.stage([b"shared-content"]) for item in stores]
    barrier = threading.Barrier(len(stores))

    def publish(index: int):
        """同时竞争同一个内容名，并各自清理自己的暂存。"""
        barrier.wait(timeout=10)
        try:
            return stores[index].publish(staged[index])
        finally:
            stores[index].discard(staged[index])

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(publish, range(len(stores))))
    assert sum(result.created for result in results) == 1
    assert len({result.path.stat().st_ino for result in results}) == 1
    assert stores[0].read(results[0].sha256) == b"shared-content"
    assert list((tmp_path / "shared" / ".staging").iterdir()) == []


def test_object_is_invisible_until_atomic_publication(
    store: FileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布准备期间外部看不到半个对象，原子占名后才读到完整内容。"""
    content = b"atomic" * 100_000
    staged = store.stage([content])
    entered, proceed = threading.Event(), threading.Event()
    original_link = os.link

    def delayed_link(*args, **kwargs):
        """暂停在原子目录项创建前，模拟慢磁盘。"""
        entered.set()
        assert proceed.wait(timeout=10)
        return original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", delayed_link)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.publish, staged)
        try:
            assert entered.wait(timeout=10)
            assert list(store.iter_objects()) == []
            with pytest.raises(StorageError, match="文件不存在") as missing:
                store.read(staged.sha256)
            assert missing.value.code == "not_found"
        finally:
            proceed.set()
        stored = pending.result(timeout=10)
    assert store.read(stored.sha256) == content


def test_stage_is_lazy_and_has_bounded_memory(store: FileStore) -> None:
    """大量字节块边产生边落盘，不先收集全部上传内容。"""
    count, size = 128, 64 * 1024

    def chunks():
        """下一块产生前，上一块必须已经写入临时文件。"""
        for index in range(count):
            if index:
                temporary = list((store.root / ".staging").iterdir())
                assert len(temporary) == 1
                assert temporary[0].stat().st_size == index * size
            yield bytes([index % 256]) * size

    tracemalloc.start()
    try:
        staged = store.stage(chunks())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert staged.size_bytes == count * size
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("bad_chunk", ["text", bytearray(b"x"), memoryview(b"x"), 1, None])
def test_nonbytes_chunks_are_rejected_and_cleaned(store: FileStore, bad_chunk: object) -> None:
    """类型错误发生在部分写入之后，也不能遗留临时文件。"""
    with pytest.raises(StorageError) as error:
        store.stage([b"valid", bad_chunk])
    assert error.value.code == "invalid_chunk"
    assert list((store.root / ".staging").iterdir()) == []


@pytest.mark.parametrize("chunks", [None, 1, b"not-an-iterable-of-bytes"])
def test_invalid_chunk_iterable_is_rejected(store: FileStore, chunks: object) -> None:
    """非迭代对象和把单个bytes误当字节块集合均得到安全异常。"""
    with pytest.raises(StorageError):
        store.stage(chunks)
    assert list((store.root / ".staging").iterdir()) == []


def test_limit_is_enforced_during_stream_and_boundary_is_allowed(tmp_path: Path) -> None:
    """恰好达到上限可写入，下一字节直接拒绝且清理临时数据。"""
    store = FileStore(tmp_path / "limited", max_bytes=4)
    staged = store.stage([b"ab", b"cd"])
    assert staged.size_bytes == 4
    store.discard(staged)
    with pytest.raises(FileTooLarge) as error:
        store.stage([b"ab", b"cde"])
    assert error.value.code == "file_too_large"
    assert list((store.root / ".staging").iterdir()) == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_invalid_limits_are_rejected(tmp_path: Path, limit: object) -> None:
    """限额必须是正整数，布尔值不能冒充字节数。"""
    with pytest.raises(StorageError) as error:
        FileStore(tmp_path / "invalid", max_bytes=limit)
    assert error.value.code == "invalid_limit"


def test_generator_failure_cleans_partial_file(store: FileStore) -> None:
    """上传源在中途异常时，已写的部分原件不留在暂存目录。"""

    def chunks():
        """模拟请求源在第一块后中断。"""
        yield b"first"
        raise RuntimeError("sensitive external message")

    with pytest.raises(StorageError) as error:
        store.stage(chunks())
    assert error.value.code == "invalid_chunks"
    assert "sensitive" not in str(error.value)
    assert list((store.root / ".staging").iterdir()) == []


def test_stage_fsync_failure_cleans_and_hides_paths(
    store: FileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘同步失败时仍清理文件，公开异常不包含底层路径。"""

    def fail_sync(descriptor: int) -> None:
        """模拟底层错误带有敏感目录信息。"""
        raise OSError(errno.EIO, "/sensitive/customer/path")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(StorageError) as error:
        store.stage([b"partial"])
    assert error.value.code == "io_error"
    assert "sensitive" not in str(error.value)
    assert list((store.root / ".staging").iterdir()) == []


def test_file_and_parent_are_synced_before_publication_returns(
    store: FileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功返回前既同步内容文件，也同步对象目录，确保目录项持久化。"""
    staged = store.stage([b"durable"])
    original_sync, original_link = os.fsync, os.link
    events: list[str] = []
    object_inode = (store.root / "objects").stat().st_ino

    def record_sync(descriptor: int) -> None:
        """按文件类型记录实际同步步骤，仍执行系统调用。"""
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            events.append("file")
        elif info.st_ino == object_inode:
            events.append("objects")
        else:
            events.append("directory")
        original_sync(descriptor)

    def record_link(*args, **kwargs):
        """记录可见对象出现的时刻。"""
        events.append("link")
        return original_link(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_sync)
    monkeypatch.setattr(os, "link", record_link)
    store.publish(staged)
    assert events.index("file") < events.index("link") < events.index("objects")


def test_failed_link_keeps_original_staged_and_cleans_publish_copy(
    store: FileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子发布失败只清理内部副本，原staged仍由调用方finally处理。"""
    staged = store.stage([b"original"])

    def fail_link(*args, **kwargs):
        """模拟文件系统无法完成原子发布。"""
        raise OSError(errno.EIO, "private storage path")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(StorageError) as error:
        store.publish(staged)
    assert error.value.code == "io_error"
    assert set((store.root / ".staging").iterdir()) == {staged.path}
    assert list(store.iter_objects()) == []


@pytest.mark.parametrize("tamper", ["content", "size", "digest"])
def test_tampered_staged_metadata_or_content_cannot_publish(store: FileStore, tamper: str) -> None:
    """发布重新计算摘要及大小，不信任调用方持有的暂存描述。"""
    staged = store.stage([b"original"])
    if tamper == "content":
        staged.path.write_bytes(b"replaced")
    elif tamper == "size":
        staged = replace(staged, size_bytes=1)
    else:
        staged = replace(staged, sha256="a" * 64)
    with pytest.raises(StorageError) as error:
        store.publish(staged)
    assert error.value.code == "corrupt_staged"
    assert list(store.iter_objects()) == []
    assert list((store.root / ".staging").iterdir()) == [staged.path]


def test_corrupt_existing_object_is_never_overwritten(store: FileStore) -> None:
    """损坏的既有对象必须报错，不能被重复上传悄悄覆盖或当成成功。"""
    staged = store.stage([b"correct"])
    stored = store.publish(staged)
    stored.path.chmod(0o600)
    stored.path.write_bytes(b"damaged")
    for operation in (lambda: store.read(stored.sha256), lambda: store.publish(staged)):
        with pytest.raises(StorageError) as error:
            operation()
        assert error.value.code == "corrupt_object"
    assert stored.path.read_bytes() == b"damaged"
    assert list((store.root / ".staging").iterdir()) == [staged.path]


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "../outside",
        "/tmp/customer",
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "a" * 64 + "\n",
        "中" * 64,
        None,
        b"a" * 64,
    ],
)
def test_client_paths_and_malformed_digests_are_rejected(store: FileStore, digest: object) -> None:
    """读取和删除都只接受严格摘要，不拼接客户提供的任意路径。"""
    for operation in (store.read, store.open, store.remove):
        with pytest.raises(StorageError) as error:
            operation(digest)
        assert error.value.code == "invalid_digest"


def test_foreign_and_traversal_staged_paths_cannot_be_deleted(
    store: FileStore, tmp_path: Path
) -> None:
    """伪造暂存描述不能删除其他目录的文件，也不能跨存储发布。"""
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    other = FileStore(tmp_path / "other").stage([b"keep"])
    traversal = store.root / ".staging" / ".." / "outside"
    for path in (outside, other.path, traversal):
        staged = StagedFile(path, hashlib.sha256(b"keep").hexdigest(), 4)
        for operation in (store.discard, store.publish):
            with pytest.raises(StorageError) as error:
                operation(staged)
            assert error.value.code == "invalid_staged"
    assert outside.read_bytes() == b"keep"
    assert other.path.exists()


@pytest.mark.parametrize("directory", ["root", ".staging", "objects"])
def test_symbolic_link_directories_are_rejected_at_creation(tmp_path: Path, directory: str) -> None:
    """根目录与受管理子目录不能通过符号链接跳到其他位置。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "files"
    if directory == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / directory).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        FileStore(root)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("directory", [".staging", "objects"])
def test_replaced_subdirectory_symlink_is_rejected_on_every_operation(
    store: FileStore, tmp_path: Path, directory: str
) -> None:
    """实例创建后被替换成链接的子目录也不能绕过逐次路径校验。"""
    staged = store.stage([b"safe"])
    stored = store.publish(staged)
    target = store.root / directory
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    for operation in (
        lambda: store.stage([b"x"]),
        lambda: store.publish(staged),
        lambda: store.discard(staged),
        lambda: store.read(stored.sha256),
        lambda: store.remove(stored.sha256),
        lambda: list(store.iter_objects()),
    ):
        with pytest.raises(StorageError):
            operation()
    assert stored.sha256 in {item.name for item in moved.iterdir()} or directory == ".staging"


def test_symbolic_link_object_is_rejected_without_touching_target(
    store: FileStore, tmp_path: Path
) -> None:
    """同摘要名的符号链接不能被读取、删除或复用为已发布对象。"""
    staged = store.stage([b"original"])
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    (store.root / "objects" / staged.sha256).symlink_to(outside)
    for operation in (
        lambda: store.read(staged.sha256),
        lambda: store.publish(staged),
        lambda: store.remove(staged.sha256),
        lambda: list(store.iter_objects()),
    ):
        with pytest.raises(StorageError) as error:
            operation()
        assert error.value.code == "unsafe_path"
    assert outside.read_bytes() == b"original"


def test_staged_symbolic_link_is_rejected_without_touching_target(
    store: FileStore, tmp_path: Path
) -> None:
    """暂存文件被替换成链接后，发布与清理均拒绝跟随。"""
    staged = store.stage([b"original"])
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    staged.path.unlink()
    staged.path.symlink_to(outside)
    for operation in (store.publish, store.discard):
        with pytest.raises(StorageError) as error:
            operation(staged)
        assert error.value.code == "unsafe_path"
    assert outside.read_bytes() == b"original"
    assert list(store.iter_objects()) == []


def test_special_file_is_rejected_without_blocking(store: FileStore) -> None:
    """管道不能伪装成内容文件，打开不会等待写入方而挂起。"""
    digest = "a" * 64
    os.mkfifo(store.root / "objects" / digest)
    with pytest.raises(StorageError) as error:
        store.read(digest)
    assert error.value.code == "unsafe_path"


def test_iteration_and_explicit_remove_only_target_content(store: FileStore) -> None:
    """回收接口仅枚举对象摘要与时间，删除不波及其他对象或暂存。"""
    first, second = store.stage([b"first"]), store.stage([b"second"])
    stored_first, stored_second = store.publish(first), store.publish(second)
    found = dict(store.iter_objects())
    assert found == {
        first.sha256: stored_first.path.stat().st_mtime,
        second.sha256: stored_second.path.stat().st_mtime,
    }
    assert store.remove(first.sha256)
    assert not store.remove(first.sha256)
    assert store.read(second.sha256) == b"second"
    assert first.path.exists() and second.path.exists()
