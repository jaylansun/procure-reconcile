"""维护入口只操作明确配置的环境，清理失败不输出连接或文件细节。"""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from app import cli


@pytest.fixture
def maintenance(monkeypatch, tmp_path):
    """替换外部资源，验证CLI编排而不读取本机配置。"""
    configured = SimpleNamespace(
        instance_id="test-cli",
        database_url=SecretStr("postgresql://test-only"),
        files_dir=tmp_path,
        max_file_bytes=4096,
        command_lock_timeout_ms=250,
    )
    settings = Mock(return_value=configured)
    database = Mock()
    database_factory = Mock(return_value=database)
    store = Mock()
    store_factory = Mock(return_value=store)
    executor = Mock()
    executor_factory = Mock(return_value=executor)
    service = Mock()
    service.cleanup_orphans.return_value = 2
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(cli, "Settings", settings)
    monkeypatch.setattr(cli, "Database", database_factory)
    monkeypatch.setattr(cli, "FileStore", store_factory)
    monkeypatch.setattr(cli, "CommandExecutor", executor_factory)
    monkeypatch.setattr(cli, "FileService", service_factory)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return SimpleNamespace(
        configured=configured,
        settings=settings,
        database=database,
        database_factory=database_factory,
        store=store,
        store_factory=store_factory,
        executor=executor,
        executor_factory=executor_factory,
        service=service,
        service_factory=service_factory,
    )


@pytest.mark.parametrize("arguments,seconds", [([], 86400), (["--min-age-hours", "0"], 0)])
def test_cleanup_uses_explicit_environment_and_grace(
    monkeypatch, capsys, maintenance, arguments, seconds
):
    """默认留24小时宽限；显式0可立即检查，始终交给带内容锁的服务清理。"""
    monkeypatch.setattr(sys, "argv", ["app.cli", "cleanup-files", *arguments])
    assert cli.main() == 0
    maintenance.database_factory.assert_called_once_with("postgresql://test-only")
    maintenance.store_factory.assert_called_once_with(
        maintenance.configured.files_dir, max_bytes=4096
    )
    maintenance.executor_factory.assert_called_once_with(maintenance.database, lock_timeout_ms=250)
    maintenance.service_factory.assert_called_once_with(
        maintenance.database, maintenance.store, maintenance.executor
    )
    maintenance.service.cleanup_orphans.assert_called_once_with(min_age_seconds=seconds)
    maintenance.database.close.assert_called_once()
    assert "2份" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["-1", "1.5", "nan"])
def test_invalid_grace_does_not_open_database(monkeypatch, maintenance, value):
    """非法宽限值在参数解析阶段拒绝，不创建连接或触碰附件。"""
    monkeypatch.setattr(sys, "argv", ["app.cli", "cleanup-files", "--min-age-hours", value])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    maintenance.settings.assert_not_called()
    maintenance.database_factory.assert_not_called()


def test_cleanup_database_failure_is_safe_and_closes(monkeypatch, capsys, maintenance):
    """PG故障保留失败退出码并关闭资源，不将驱动原始异常打印出去。"""
    monkeypatch.setattr(sys, "argv", ["app.cli", "cleanup-files"])
    maintenance.service.cleanup_orphans.side_effect = OperationalError(
        "private SQL", {"password": "private-password"}, RuntimeError("private-path")
    )
    assert cli.main() == 1
    maintenance.database.close.assert_called_once()
    output = capsys.readouterr()
    assert "维护失败" in output.err
    assert "private" not in output.err + output.out


def test_initialize_still_requires_interactive_terminal(monkeypatch, maintenance):
    """新增可自动运行的清理命令不会放宽管理员密码的交互输入约定。"""
    monkeypatch.setattr(sys, "argv", ["app.cli", "init-admin"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    maintenance.settings.assert_not_called()


def test_cleanup_requires_stable_environment(monkeypatch, maintenance):
    """环境未配置时明确失败，不猜测开发或正式数据库。"""
    monkeypatch.setattr(sys, "argv", ["app.cli", "cleanup-files"])
    maintenance.configured.instance_id = None
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    maintenance.database_factory.assert_not_called()
