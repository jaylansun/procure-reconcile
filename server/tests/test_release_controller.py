"""不启动Docker，以故障注入验证发布停写、恢复隔离、秘密与备份完整性。"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/deploy/release.py"
SPEC = importlib.util.spec_from_file_location("_procure_release_test_module", SOURCE)
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
TEST_PASSWORD = "synthetic-app-password"


def configuration_bytes(**overrides):
    """所有值为合成测试数据，不读取本机dotenv。"""
    values = {
        "PROCURE_INSTANCE_ID": "prod-test-controller",
        "PROCURE_DATABASE_URL": f"postgresql+psycopg://procure_app:{quote(TEST_PASSWORD)}@db:5432/procure",
        "PROCURE_PG_ADMIN_PASSWORD": "synthetic-admin-password",
        "PROCURE_PG_APP_PASSWORD": TEST_PASSWORD,
        "PROCURE_BOOTSTRAP_USERNAME": "admin",
        "PROCURE_BOOTSTRAP_PASSWORD": "012345",
        "PROCURE_LAN_BIND": "192.0.2.10",
        "PROCURE_HTTPS_PORT": "18443",
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()).encode()


class Gate:
    """记录维护状态和独占范围，以断言开放只发生在排空保护内。"""

    def __init__(self):
        self.active = False
        self.exclusive_active = False
        self.events = []

    def set_maintenance(self, active, *, release_id):
        if not active:
            assert self.exclusive_active
        self.active = active
        self.events.append(active)

    @contextmanager
    def exclusive(self, *, timeout_seconds):
        assert self.active
        assert timeout_seconds == 60
        self.exclusive_active = True
        try:
            yield
        finally:
            self.exclusive_active = False


class Docker:
    """模拟Docker边界；文件备份、校验、journal和锁仍运行真实实现。"""

    def __init__(self, gate, failure=None, *, initial_schema=True):
        self.gate, self.failure = gate, failure
        self.calls = []
        self.inputs = []
        self.initial_schema = initial_schema
        self.compose_environments = []
        self.restore_environments = []

    def run(self, arguments, *, input_data=None, output=None, timeout=180, check=True):
        self.calls.append(arguments)
        self.inputs.append(input_data if isinstance(input_data, bytes) else None)
        stdout = b""
        if arguments[:3] == ["docker", "volume", "inspect"]:
            code = 0 if self.failure == "stale_volume" else 1
            return subprocess.CompletedProcess(arguments, code, stdout=b"", stderr=b"")
        if arguments[:3] == ["docker", "image", "inspect"]:
            if self.failure == "image":
                raise RuntimeError("private driver credential must stay hidden")
            stdout = json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT}},
                    }
                ]
            ).encode()
        elif arguments[:2] == ["docker", "compose"]:
            env = Path(arguments[arguments.index("--env-file") + 1])
            self.compose_environments.append(env.read_text())
            if "alembic" in arguments:
                assert self.gate.exclusive_active
                if self.failure == "migration":
                    raise RuntimeError("private migration SQL")
            if "-c" in arguments:
                script = arguments[-1]
                if script in (release.CANDIDATE_SCRIPT, release.MAINTENANCE_HTTP_SCRIPT):
                    assert self.gate.active and self.gate.exclusive_active
                if script == release.AUTH_SMOKE_SCRIPT:
                    assert not self.gate.active and not self.gate.exclusive_active
                if (
                    self.failure == "candidate"
                    and script == release.MAINTENANCE_HTTP_SCRIPT
                    or self.failure == "smoke"
                    and script == release.AUTH_SMOKE_SCRIPT
                ):
                    raise RuntimeError("private token from failed HTTP smoke")
                stdout = json.dumps({"ok": True, "schema": ["0003_infrastructure"]}).encode()
        elif arguments[:3] == ["docker", "inspect", "--type"]:
            stdout = IMAGE_ID.encode()
        elif "psql" in arguments:
            sql = arguments[-1]
            if "pg_stat_activity" in sql:
                stdout = b"1" if self.failure == "drain" else b"0"
            elif "to_regclass" in sql:
                stdout = b"existing" if self.initial_schema else b""
            elif "version_num" in sql:
                stdout = b"0003_infrastructure"
            elif "count(*)" in sql:
                stdout = b"1"
        elif "pg_dump" in arguments:
            assert self.gate.exclusive_active
            if self.failure == "backup":
                raise RuntimeError("private backup driver output")
            output.write(b"PGDMP-synthetic-test-dump")
        elif arguments[:2] == ["docker", "run"]:
            env = Path(arguments[arguments.index("--env-file") + 1])
            self.restore_environments.append(env.read_text())
        elif "pg_restore" in arguments:
            assert self.gate.exclusive_active
            assert input_data.read() == b"PGDMP-synthetic-test-dump"
            if self.failure == "restore":
                raise RuntimeError("private restore driver output")
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")


def make_controller(tmp_path, failure=None, *, initial_schema=True):
    gate = Gate()
    docker = Docker(gate, failure, initial_schema=initial_schema)
    data = tmp_path / "data"
    (data / "files/objects").mkdir(parents=True)
    (data / "files/objects/example").write_bytes(b"immutable attachment")
    controller = release.Controller(
        release.Configuration.read(configuration_bytes()),
        "test-1",
        COMMIT,
        "1",
        state=tmp_path / "state",
        backups=tmp_path / "backups",
        data=data,
        root=tmp_path / "checkout",
        runner=docker,
        gate=gate,
    )
    return controller, gate, docker


def test_dotenv_is_literal_and_duplicate_keys_are_rejected():
    """看似shell的内容作为字面量保存；重复键不能悄悄覆盖目标。"""
    assert (
        release.parse_dotenv(b"PROCURE_SAMPLE=$(printf secret)\n")["PROCURE_SAMPLE"]
        == "$(printf secret)"
    )
    with pytest.raises(release.ReleaseError, match="invalid_configuration"):
        release.parse_dotenv(b"PROCURE_SAMPLE=one\nPROCURE_SAMPLE=two")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://procure_app:synthetic-app-password@devpilot:5432/procure",
        "postgresql+psycopg://postgres:synthetic-app-password@db:5432/procure",
        "postgresql+psycopg://procure_app:synthetic-app-password@db:5432/another",
        "postgresql+psycopg://procure_app:wrong@db:5432/procure",
        "postgresql+psycopg://procure_app:synthetic-app-password@db:5432/procure?options=other",
    ],
)
def test_configuration_rejects_wrong_database_target(url):
    """正式控制器只允许本项目内网库、角色和已配置密码。"""
    with pytest.raises(release.ReleaseError, match="wrong_database_target"):
        release.Configuration.read(configuration_bytes(PROCURE_DATABASE_URL=url))


def test_secrets_are_not_in_repr_or_long_running_environment(tmp_path):
    """bootstrap凭据仅作一次性stdin输入，不加入Compose配置。"""
    configuration = release.Configuration.read(configuration_bytes())
    assert TEST_PASSWORD not in repr(configuration)
    values = configuration.compose_values("test-1")
    assert not any("BOOTSTRAP" in key for key in values)
    path = tmp_path / "private.env"
    release.write_env(path, values)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "012345" not in path.read_text()
    assert json.loads(configuration.bootstrap())["password"] == "012345"


def test_runner_hides_raw_driver_output_and_exceptions(monkeypatch, capsys):
    """子进程失败只带固定错误码，第三方stderr不进入发布日志。"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout=b"private-password", stderr=b"private-database-url"
        ),
    )
    with pytest.raises(release.ReleaseError) as caught:
        release.Runner().run(["docker", "synthetic-test"])
    assert str(caught.value) == "docker_command_failed"
    assert capsys.readouterr().out == ""


def test_target_lock_serializes_different_controller_instances(tmp_path):
    """跨实例目标锁忙时失败，不依赖Jenkins同一job串行。"""
    with release.target_lock(tmp_path):
        with pytest.raises(release.ReleaseError, match="deployment_busy"):
            with release.target_lock(tmp_path, timeout_seconds=0):
                pytest.fail("第二个控制器不应取得目标锁")


def test_incomplete_release_refuses_retry_without_touching_state(tmp_path):
    """中断现场只能先检查，不自动重跑迁移、开放或恢复旧库。"""
    controller, gate, docker = make_controller(tmp_path)
    state = controller.state / "current.json"
    release.atomic_json(state, {"status": "running", "phase": "migration"})
    original = state.read_bytes()
    with pytest.raises(release.ReleaseError, match="incomplete_release_requires_inspection"):
        controller.run()
    assert docker.calls == []
    assert gate.events == []
    assert state.read_bytes() == original


@pytest.mark.parametrize("initial_schema", [False, True])
def test_success_backs_up_verifies_restores_and_opens_last(tmp_path, capsys, initial_schema):
    """空库首次发布和已有库发布都先验证备份，再迁移并完成开放后的认证冒烟。"""
    controller, gate, docker = make_controller(tmp_path, initial_schema=initial_schema)
    result = controller.run()
    assert result["status"] == "succeeded"
    assert gate.events == [True, False]
    assert not gate.active
    journal = json.loads((controller.state / "current.json").read_text())
    assert journal["schema_after"] == ["0003_infrastructure"]
    backup = controller.backups / controller.release_id
    manifest = json.loads((backup / "manifest.json").read_text())
    release.verify_backup(backup, manifest)
    assert manifest["files"][0]["path"] == "files/objects/example"
    commands = [" ".join(arguments) for arguments in docker.calls]
    assert any("--no-recreate" in command and command.endswith(" db") for command in commands)
    restore_commands = [command for command in commands if "pg_restore" in command]
    assert len(restore_commands) == 1
    assert "procure-restore-1" in restore_commands[0]
    assert "procure-db-1" not in restore_commands[0]
    assert any("network create --internal procure-restore-1-net" in command for command in commands)
    assert any("volume rm procure-restore-1-data" in command for command in commands)
    assert not any("BOOTSTRAP" in env for env in docker.compose_environments)
    assert all("PROCURE_APP_PASSWORD=" in env for env in docker.restore_environments)
    # 凭据只存在stdin/0600临时文件，不出现在argv、journal或结构日志中。
    output = capsys.readouterr().out
    combined = "\n".join(commands) + json.dumps(journal) + output
    for secret in (TEST_PASSWORD, "synthetic-admin-password", "012345"):
        assert secret not in combined
    assert all(
        json.loads(line)["status"] in {"running", "succeeded"} for line in output.splitlines()
    )


@pytest.mark.parametrize(
    "failure,phase",
    [
        ("drain", "drained"),
        ("backup", "backup"),
        ("restore", "restore_verification"),
        ("migration", "migration"),
        ("candidate", "candidate"),
        ("smoke", "authenticated_smoke"),
    ],
)
def test_failures_keep_maintenance_and_diagnostic_phase(tmp_path, capsys, failure, phase):
    """关键阶段失败均停写；开放后冒烟失败也重新维护，绝不恢复生产库。"""
    controller, gate, docker = make_controller(tmp_path, failure)
    with pytest.raises(release.ReleaseError):
        controller.run()
    assert gate.active
    assert gate.events[-1] is True
    journal = json.loads((controller.state / "current.json").read_text())
    assert journal["status"] == "failed"
    assert journal["phase"] == phase
    commands = [" ".join(arguments) for arguments in docker.calls]
    assert "stop --timeout 60 gateway api" in commands[-1]
    assert not any("pg_restore" in command and "procure-db-1" in command for command in commands)
    assert not any(" down" in command or "prune" in command for command in commands)
    assert "private" not in capsys.readouterr().out + json.dumps(journal)
    if failure in {"drain", "backup", "restore"}:
        assert not any("alembic upgrade" in command for command in commands)


def test_preflight_failure_does_not_stop_existing_service(tmp_path):
    """镜像未准备好时，仍在维护前退出，不影响旧版本。"""
    controller, gate, docker = make_controller(tmp_path, "image")
    with pytest.raises(release.ReleaseError):
        controller.run()
    assert gate.events == []
    assert not any("stop" in command for command in docker.calls)


def test_backup_detects_corruption_before_migration(tmp_path):
    """备份后实际读取并核对摘要，不能仅凭清单存在认定可恢复。"""
    controller, gate, _ = make_controller(tmp_path)
    gate.set_maintenance(True, release_id=controller.release_id)
    with gate.exclusive(timeout_seconds=60):
        directory, _ = controller._backup()
    manifest = json.loads((directory / "manifest.json").read_text())
    (directory / "files.tar.gz").write_bytes(b"corrupted archive")
    with pytest.raises(release.ReleaseError, match="backup_hash_mismatch"):
        release.verify_backup(directory, manifest)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "fifo"])
def test_attachment_backup_rejects_unsafe_files(tmp_path, kind):
    """附件备份拒绝链接及特殊文件，FIFO不得阻塞整个发布。"""
    source = tmp_path / "files"
    source.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "private").write_text("synthetic private sample")
    if kind == "file_symlink":
        (source / "bad").symlink_to(target / "private")
    elif kind == "directory_symlink":
        (source / "bad").symlink_to(target, target_is_directory=True)
    else:
        os.mkfifo(source / "bad")
    with pytest.raises(release.ReleaseError, match="unsafe_attachment_path"):
        release.archive_files(source, tmp_path / "backup.tar.gz")


def test_inspection_is_read_only_and_filters_unknown_fields(tmp_path):
    """检查中断状态不会清除门禁、覆盖journal或输出未知私有字段。"""
    path = tmp_path / "current.json"
    release.atomic_json(path, {"status": "failed", "phase": "migration", "password": "hidden"})
    original = path.read_bytes()
    assert release.inspect_state(tmp_path) == {"status": "failed", "phase": "migration"}
    assert path.read_bytes() == original


def test_stale_restore_volume_is_neither_reused_nor_deleted(tmp_path):
    """旧恢复现场需要人工检查，不能因同名就当成新建临时卷删除。"""
    controller, gate, docker = make_controller(tmp_path, "stale_volume")
    with pytest.raises(release.ReleaseError, match="restore_resource_exists"):
        controller.run()
    assert gate.active
    commands = [" ".join(arguments) for arguments in docker.calls]
    assert not any("volume rm" in command for command in commands)
    assert not any("pg_restore" in command or "alembic upgrade" in command for command in commands)


def test_configuration_version_is_saved_in_journal_and_inspection(tmp_path):
    """发布记录关联显式配置版本，不用秘密内容充当配置标识。"""
    controller, _, _ = make_controller(tmp_path)
    controller.configuration = release.Configuration.read(
        configuration_bytes(PROCURE_CONFIG_VERSION="production-config-20261102")
    )
    controller.run()
    inspected = release.inspect_state(controller.state)
    assert inspected["config_version"] == "production-config-20261102"


@pytest.mark.parametrize("version", [" ", "x" * 129, '"bad\\tversion"', '"\\ud800"'])
def test_invalid_configuration_version_is_rejected(version):
    """配置版本禁止空白、超长、控制字符与无效UTF8。"""
    with pytest.raises(release.ReleaseError, match="invalid_configuration_version"):
        release.Configuration.read(configuration_bytes(PROCURE_CONFIG_VERSION=version))
