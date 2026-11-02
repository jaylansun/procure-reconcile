"""用隔离PG验证文件引用、补偿回收与并发，不读取部署配置。"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.documents.service import FileService
from app.documents.storage import FileStore, StagedFile, StorageError
from app.errors import AppError
from app.persistence.database import Database
from app.persistence.models import AuditEvent, CommandRecord, StoredFile
from app.security.admin import initialize_administrator
from app.security.passwords import new_session_token
from app.services.command_executor import CommandContext, CommandExecutor

pytestmark = pytest.mark.postgres


@pytest.fixture
def file_service(postgres_database: Database, tmp_path: Path) -> FileService:
    """仅使用显式指定的测试库与本次临时文件目录。"""
    initialize_administrator(postgres_database, "files-admin", new_session_token())
    return FileService(
        postgres_database, FileStore(tmp_path / "files"), CommandExecutor(postgres_database)
    )


def upload(
    service: FileService,
    content: bytes = b"original file",
    filename: str = "采购凭证.pdf",
    command_id: UUID | None = None,
):
    """模拟API的finally清理责任，不让服务自动丢弃暂存内容。"""
    staged = service.store.stage([content])
    try:
        return service.import_staged(
            staged, command_id=command_id or uuid4(), filename=filename, actor_id=1
        )
    finally:
        service.store.discard(staged)


def assert_counts(database: Database, *, files: int, commands: int, audits: int) -> None:
    """检查引用、成功命令与审计的提交边界，拒绝半份结果。"""
    assert database.sessions is not None
    with database.sessions() as session:
        for model, expected in (
            (StoredFile, files),
            (CommandRecord, commands),
            (AuditEvent, audits),
        ):
            assert session.scalar(select(func.count()).select_from(model)) == expected


def test_success_and_same_command_replay_once(
    file_service: FileService, postgres_database: Database
) -> None:
    """重放返回首次201及完整响应，不重复引用或审计，响应不包含路径。"""
    command_id = uuid4()
    first = upload(file_service, command_id=command_id)
    replay = upload(file_service, command_id=command_id)
    assert first.status_code == 201 and not first.replayed
    assert replay.status_code == 201 and replay.replayed
    assert first.body == replay.body
    assert set(first.body) == {
        "file_id",
        "sha256",
        "size_bytes",
        "original_name",
        "media_type",
        "created_at",
    }
    assert str(file_service.store.root) not in str(first.body)
    assert_counts(postgres_database, files=1, commands=1, audits=2)
    with postgres_database.sessions() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.target_type == "file"))
        assert audit.details == {
            "sha256": first.body["sha256"],
            "size_bytes": len(b"original file"),
            "reused": False,
        }
    assert list((file_service.store.root / ".staging").iterdir()) == []


def test_different_filename_same_content_reuses_first_metadata(
    file_service: FileService, postgres_database: Database
) -> None:
    """不同命令上传同内容保留首次文件名与媒体类型，新增一次复用审计。"""
    first = upload(file_service, filename="first.pdf")
    reused = upload(file_service, filename="second.PNG")
    assert reused.status_code == 200 and not reused.replayed
    assert reused.body == first.body
    assert reused.body["original_name"] == "first.pdf"
    assert reused.body["media_type"] == "application/pdf"
    assert_counts(postgres_database, files=1, commands=2, audits=4)


@pytest.mark.parametrize("changed", ["filename", "content"])
def test_same_command_changed_filename_or_hash_is_conflict(
    file_service: FileService, postgres_database: Database, changed: str
) -> None:
    """同一命令ID的名称或内容变化都返回409，不能发布额外对象。"""
    command_id = uuid4()
    first = upload(file_service, command_id=command_id)
    with pytest.raises(AppError) as error:
        upload(
            file_service,
            content=b"changed" if changed == "content" else b"original file",
            filename="changed.pdf" if changed == "filename" else "采购凭证.pdf",
            command_id=command_id,
        )
    assert (error.value.status, error.value.code) == (409, "command_conflict")
    assert list(dict(file_service.store.iter_objects())) == [first.body["sha256"]]
    assert_counts(postgres_database, files=1, commands=1, audits=2)


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        (
            "中英文 English.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("old.XLS", "application/vnd.ms-excel"),
        ("original.PDF", "application/pdf"),
        ("photo.PNG", "image/png"),
        ("photo.jpg", "image/jpeg"),
        ("photo.JPEG", "image/jpeg"),
    ],
)
def test_confirmed_extensions_use_server_media_mapping(
    file_service: FileService, filename: str, media_type: str
) -> None:
    """六种已确认扩展名不依赖客户端Content-Type，大小写不影响映射。"""
    result = upload(file_service, filename=filename)
    assert result.body["media_type"] == media_type
    assert result.body["original_name"] == filename


@pytest.mark.parametrize(
    "filename",
    [
        "",
        " ",
        "a" * 252 + ".pdf",
        "../outside.pdf",
        "/tmp/file.pdf",
        "dir/file.pdf",
        "dir\\file.pdf",
        "C:\\secret.pdf",
        "C:secret.pdf",
        "\\\\host\\file.pdf",
        "line\nbreak.pdf",
        "nul\0.pdf",
        "tab\t.pdf",
        "delete\x7f.pdf",
        "bad\ud800.pdf",
        "unsupported.txt",
    ],
)
def test_unsafe_names_are_rejected_before_any_database_access(
    tmp_path: Path, filename: str
) -> None:
    """路径、控制字符、超长和未确认后缀在事务及发布前就拒绝。"""
    database = Database(None)
    service = FileService(database, FileStore(tmp_path / "files"), CommandExecutor(database))
    staged = service.store.stage([b"content"])
    try:
        with pytest.raises(AppError) as error:
            service.import_staged(staged, command_id=uuid4(), filename=filename, actor_id=1)
        assert error.value.status == 422
        assert staged.path.exists()
        assert list(service.store.iter_objects()) == []
    finally:
        service.store.discard(staged)


def test_empty_file_is_rejected_without_database_or_publication(tmp_path: Path) -> None:
    """空文件不是合法导入，服务报422且仍由调用方清理暂存。"""
    database = Database(None)
    service = FileService(database, FileStore(tmp_path / "files"), CommandExecutor(database))
    staged = service.store.stage([])
    with pytest.raises(AppError) as error:
        service.import_staged(staged, command_id=uuid4(), filename="empty.pdf", actor_id=1)
    assert (error.value.status, error.value.code) == (422, "empty_file")
    assert staged.path.exists()
    service.store.discard(staged)


def test_concurrent_different_commands_share_one_reference(
    file_service: FileService, postgres_database: Database
) -> None:
    """真实PG内容锁让并发同内容只有一条引用，不覆盖首次元数据。"""
    barrier = threading.Barrier(2)

    def submit(index: int):
        """两个连接同时提交不同命令与不同文件名。"""
        barrier.wait(timeout=5)
        return upload(file_service, filename=f"file-{index}.pdf")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sorted(result.status_code for result in results) == [200, 201]
    assert results[0].body == results[1].body
    assert_counts(postgres_database, files=1, commands=2, audits=4)
    metadata, source = file_service.open_file(UUID(results[0].body["file_id"]))
    with source:
        assert source.read() == b"original file"
    assert metadata == results[0].body


def test_concurrent_same_command_replays_without_duplicate_audit(
    file_service: FileService, postgres_database: Database
) -> None:
    """同一上传命令的并发重试由命令锁重放，审计仅有首次两条。"""
    barrier, command_id = threading.Barrier(2), uuid4()

    def submit():
        """同时发起同一个已暂存内容的命令。"""
        barrier.wait(timeout=5)
        return upload(file_service, command_id=command_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].body == results[1].body
    assert_counts(postgres_database, files=1, commands=1, audits=2)


def test_failure_after_physical_publication_compensates_without_partial_sql(
    file_service: FileService, postgres_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布后抛错也会重新加锁查引用，再回收已落盘但未提交的对象。"""
    original = file_service.store.publish

    def publish_then_fail(staged: StagedFile):
        """模拟文件已创建但发布返回阶段发生故障。"""
        original(staged)
        raise StorageError("io_error", "sensitive storage path")

    monkeypatch.setattr(file_service.store, "publish", publish_then_fail)
    with pytest.raises(AppError) as error:
        upload(file_service)
    assert (error.value.status, error.value.code) == (500, "file_storage_error")
    assert "sensitive" not in error.value.message
    assert list(file_service.store.iter_objects()) == []
    assert_counts(postgres_database, files=0, commands=0, audits=0)


def test_callback_failure_after_flush_rolls_back_and_reclaims_object(
    file_service: FileService, postgres_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """元数据flush后回调失败，SQL整体回滚且无引用原件被安全回收。"""

    def fail_audit(context: CommandContext, change) -> None:
        """模拟回调在物理发布与SQL写入后拒绝操作。"""
        raise AppError(409, "injected_callback_failure", "模拟回调失败")

    monkeypatch.setattr(CommandContext, "audit", fail_audit)
    with pytest.raises(AppError) as error:
        upload(file_service)
    assert error.value.code == "injected_callback_failure"
    assert list(file_service.store.iter_objects()) == []
    assert_counts(postgres_database, files=0, commands=0, audits=0)


def test_commit_failure_reclaims_only_after_transaction_exits(
    file_service: FileService, postgres_database: Database
) -> None:
    """提交失败后的新事务才能判断引用，不能在失败事务内直接删除对象。"""
    assert postgres_database.engine is not None
    failed = False

    def fail_first_commit(connection) -> None:
        """只阻止上传事务提交，允许后续补偿短事务正常结束。"""
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected commit failure")

    event.listen(postgres_database.engine, "commit", fail_first_commit)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            upload(file_service)
    finally:
        event.remove(postgres_database.engine, "commit", fail_first_commit)
    assert failed
    assert list(file_service.store.iter_objects()) == []
    assert_counts(postgres_database, files=0, commands=0, audits=0)


def test_ambiguous_postcommit_error_keeps_committed_reference(
    file_service: FileService, postgres_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """响应阶段出错但PG已提交时，补偿查到引用后绝不能删除原件。"""
    execute = file_service.executor.execute

    def commit_then_fail(**kwargs):
        """模拟提交成功后丢失响应，调用方无法确定提交结果。"""
        execute(**kwargs)
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(file_service.executor, "execute", commit_then_fail)
    with pytest.raises(RuntimeError, match="response lost"):
        upload(file_service)
    assert_counts(postgres_database, files=1, commands=1, audits=2)
    assert len(list(file_service.store.iter_objects())) == 1


def test_cleanup_failure_preserves_original_error_and_safe_orphan(
    file_service: FileService,
    postgres_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """补偿锁不可用时保留孤儿且不覆盖原异常，日志只含类型和安全码。"""
    import app.documents.service as module

    publish = file_service.store.publish

    def publish_then_fail(staged: StagedFile):
        """先落盘，再报告不应进入日志的底层错误细节。"""
        publish(staged)
        raise RuntimeError("original upload failure")

    def fail_cleanup_lock(*args, **kwargs):
        """只影响补偿导入的锁函数，不改命令执行器里的锁。"""
        raise OSError("credential /sensitive/path")

    monkeypatch.setattr(file_service.store, "publish", publish_then_fail)
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "lock_sources", fail_cleanup_lock)
        with pytest.raises(RuntimeError, match="original upload failure"):
            upload(file_service)
    assert "OSError" in caplog.text
    assert "sensitive" not in caplog.text and "credential" not in caplog.text
    assert_counts(postgres_database, files=0, commands=0, audits=0)
    assert len(list(file_service.store.iter_objects())) == 1
    assert file_service.cleanup_orphans(min_age_seconds=0) == 1


def test_failed_upload_cleanup_cannot_delete_later_committed_reference(
    file_service: FileService, postgres_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败事务释放锁后另一上传先提交，延迟补偿必须重新查引用并保留原件。"""
    failing_id = uuid4()
    cleanup_started, resume_cleanup = threading.Event(), threading.Event()
    original_audit, compensate = CommandContext.audit, file_service._compensate

    def fail_one_audit(context: CommandContext, change) -> None:
        """仅让第一个命令在发布后回滚。"""
        if context.command_id == failing_id:
            raise AppError(409, "first_failed", "模拟首个命令失败")
        original_audit(context, change)

    def delayed_compensate(digest: str) -> None:
        """暂停失败后的补偿，给另一个命令提交引用的机会。"""
        cleanup_started.set()
        assert resume_cleanup.wait(timeout=10)
        compensate(digest)

    monkeypatch.setattr(CommandContext, "audit", fail_one_audit)
    monkeypatch.setattr(file_service, "_compensate", delayed_compensate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(upload, file_service, command_id=failing_id)
        try:
            assert cleanup_started.wait(timeout=10)
            success = pool.submit(upload, file_service).result(timeout=10)
        finally:
            resume_cleanup.set()
        with pytest.raises(AppError) as error:
            failed.result(timeout=10)
    assert error.value.code == "first_failed"
    assert file_service.store.read(success.body["sha256"]) == b"original file"
    assert_counts(postgres_database, files=1, commands=1, audits=2)


def test_gc_only_removes_old_unreferenced_objects(file_service: FileService) -> None:
    """年龄宽限排除新对象，已有数据库引用的老对象和暂存也不能删除。"""
    referenced = upload(file_service)
    old = file_service.store.stage([b"old orphan"])
    recent = file_service.store.stage([b"recent orphan"])
    old_object, recent_object = file_service.store.publish(old), file_service.store.publish(recent)
    file_service.store.discard(old)
    file_service.store.discard(recent)
    pending = file_service.store.stage([b"unpublished"])
    old_time = time.time() - 172800
    os.utime(old_object.path, (old_time, old_time))
    referenced_path = file_service.store.root / "objects" / referenced.body["sha256"]
    os.utime(referenced_path, (old_time, old_time))
    assert file_service.cleanup_orphans() == 1
    assert not old_object.path.exists()
    assert recent_object.path.exists() and pending.path.exists()
    assert file_service.store.read(referenced.body["sha256"]) == b"original file"
    assert file_service.cleanup_orphans() == 0


def test_gc_waits_for_inflight_reference_and_then_keeps_object(
    file_service: FileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GC拿同一内容锁，不能在上传事务提交前删掉正在建立引用的对象。"""
    held, release = threading.Event(), threading.Event()
    audit = CommandContext.audit

    def wait_before_commit(context: CommandContext, change) -> None:
        """在上传已有内容的元数据flush后暂停，保持内容锁尚未释放。"""
        held.set()
        assert release.wait(timeout=10)
        audit(context, change)

    staged = file_service.store.stage([b"original file"])
    file_service.store.publish(staged)
    file_service.store.discard(staged)
    monkeypatch.setattr(CommandContext, "audit", wait_before_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        incoming = pool.submit(upload, file_service)
        try:
            assert held.wait(timeout=10)
            collected = pool.submit(file_service.cleanup_orphans, 0)
            with pytest.raises(TimeoutError):
                collected.result(timeout=0.05)
        finally:
            release.set()
        result = incoming.result(timeout=10)
        assert collected.result(timeout=10) == 0
    assert file_service.store.read(result.body["sha256"]) == b"original file"


@pytest.mark.parametrize("operation", ["metadata", "open_file"])
def test_missing_metadata_is_404(file_service: FileService, operation: str) -> None:
    """不存在的公开ID返回404，不推测物理对象或暴露目录。"""
    with pytest.raises(AppError) as error:
        getattr(file_service, operation)(uuid4())
    assert (error.value.status, error.value.code) == (404, "file_not_found")


@pytest.mark.parametrize("damage", ["missing", "hash", "size"])
def test_missing_corrupt_or_wrong_size_objects_are_503_and_never_deduped(
    file_service: FileService, postgres_database: Database, damage: str
) -> None:
    """有元数据但原件缺失、损坏或长度不符时，读取及复用都必须报503。"""
    first = upload(file_service)
    file_id = UUID(first.body["file_id"])
    digest = first.body["sha256"]
    if damage == "missing":
        file_service.store.remove(digest)
    elif damage == "hash":
        path = file_service.store.root / "objects" / digest
        path.chmod(0o600)
        path.write_bytes(b"wrong content")
    else:
        with postgres_database.sessions.begin() as session:
            session.execute(update(StoredFile).values(size_bytes=999))
    assert file_service.metadata(file_id) is not None
    for action in (lambda: file_service.open_file(file_id), lambda: upload(file_service)):
        with pytest.raises(AppError) as error:
            action()
        assert (error.value.status, error.value.code) == (503, "file_unavailable")
    assert_counts(postgres_database, files=1, commands=1, audits=2)


def test_open_file_ends_query_before_touching_object_storage(
    file_service: FileService, postgres_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载流读取期间不长期占用PG连接，文件句柄由调用方关闭。"""
    uploaded = upload(file_service)
    original_open = file_service.store.open

    def ensure_query_closed(digest: str):
        """在物理读开始时检查独立服务没有留下查询连接。"""
        assert postgres_database.engine.pool.checkedout() == 0
        return original_open(digest)

    monkeypatch.setattr(file_service.store, "open", ensure_query_closed)
    metadata, source = file_service.open_file(UUID(uploaded.body["file_id"]))
    with source:
        assert source.read() == b"original file"
    assert metadata == uploaded.body
    assert source.closed


def test_publish_limit_failure_maps_to_413(
    file_service: FileService, postgres_database: Database
) -> None:
    """暂存后配置缩小导致发布超限，服务保留413语义且没有半份引用。"""
    staged = file_service.store.stage([b"too large"])
    file_service.store.max_bytes = 2
    try:
        with pytest.raises(AppError) as error:
            file_service.import_staged(staged, command_id=uuid4(), filename="large.pdf", actor_id=1)
        assert (error.value.status, error.value.code) == (413, "file_too_large")
        assert staged.path.exists()
    finally:
        file_service.store.discard(staged)
    assert_counts(postgres_database, files=0, commands=0, audits=0)
