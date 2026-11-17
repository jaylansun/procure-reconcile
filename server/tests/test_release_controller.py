"""不启动Docker，以故障注入验证发布停写、恢复隔离、秘密与备份完整性。"""

import ast
import importlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
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
# 合成测试公共证书；生成时的私钥已销毁，不使用部署证书或用户私有配置。
TEST_CA = """-----BEGIN CERTIFICATE-----
MIIDNTCCAh2gAwIBAgIUEqJWvt5LDLQg9E9HrYU6dKocZuEwDQYJKoZIhvcNAQEL
BQAwKjEoMCYGA1UEAwwfUHJvY3VyZSBDb250cm9sbGVyIFVuaXQgVGVzdCBDQTAe
Fw0yNjExMDIxNTQzMjZaFw0zNjEwMzAxNTQzMjZaMCoxKDAmBgNVBAMMH1Byb2N1
cmUgQ29udHJvbGxlciBVbml0IFRlc3QgQ0EwggEiMA0GCSqGSIb3DQEBAQUAA4IB
DwAwggEKAoIBAQC6i8Wj59F7WM/GDGUVH9AH1J0c/H0s78szqQFylVAi/rygq7NR
oB49jzVcZebDLsGRxn538LwnvOSlvanenCgOZPGIvLUN9h1AHSnJcvOkITn9xx0W
hs9LlKRaRRWkA9L99+hCER5vDwRDNieY+zpxbLlbR7Qd0qL7lie1UjOPJD9yRTNg
FJKCo54Bny6bfi8ZXZK2HSMlLm/RCeaFvuPgnqLCnFSS1zO7WuxGRmF8DnfWab8N
Kjqt3CHX8VkvCx2cjl9HRfsZq4t/7RtTSrt7044HUB/Q220rQYNL7s24Q/usJIMd
PwVn54zi2ZQ+XXOkEZS0GU0wLL/6igtCm6i5AgMBAAGjUzBRMB0GA1UdDgQWBBSi
LGHmLHl0JzGT/nBMQ9CTdhw50DAfBgNVHSMEGDAWgBSiLGHmLHl0JzGT/nBMQ9CT
dhw50DAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAYTtj0HXqM
pJAvO+KHjvxCIF8NKO+fIgkgYeBQarJw7jD8FcIiu0bPCrj4yrDEvDkD8FYxdvz5
j4G5Ffmm54HKq+a04dsg+9q2RpeZp/qXZuJwLMEickyu75d54Er8OWvUnSBEGk9X
qWrwE3hdfo42xZzZUECRYc6G6fG3KbQ5BHCMVE7dLjzlmPbvFH8sbUweV73mDQ2W
NR/yBQf/YCW0Kx0iG/Mq5lhedxfrpXBHp4ySxw8jnvTQT5WDVMC2X4A6JHQurRoO
uH7GY5RAjh2sPwb0TlN82OOP7yd7WPc7IU4170Mv9yRWVsAYe0tJC5Sc05G9/kCG
jS2oTknIvNqe
-----END CERTIFICATE-----
"""


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
                if script in (
                    release.CANDIDATE_SCRIPT,
                    release.MAINTENANCE_HTTP_SCRIPT,
                    release.CANDIDATE_TLS_SCRIPT,
                ):
                    assert self.gate.active and self.gate.exclusive_active
                if script == release.AUTH_SMOKE_SCRIPT:
                    assert not self.gate.active and not self.gate.exclusive_active
                if (
                    self.failure == "candidate"
                    and script == release.MAINTENANCE_HTTP_SCRIPT
                    or self.failure == "smoke"
                    and script == release.AUTH_SMOKE_SCRIPT
                    or self.failure == "tls"
                    and script == release.CANDIDATE_TLS_SCRIPT
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
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "ca.crt").write_text(TEST_CA)
    controller = release.ReleaseController(
        release.ReleaseSettings.read(configuration_bytes()),
        "test-1",
        COMMIT,
        "1",
        state=tmp_path / "state",
        backups=tmp_path / "backups",
        data=data,
        tls=tls,
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
        release.ReleaseSettings.read(configuration_bytes(PROCURE_DATABASE_URL=url))


def test_secrets_are_not_in_repr_or_long_running_environment(tmp_path):
    """bootstrap凭据仅作一次性stdin输入，不加入Compose配置。"""
    configuration = release.ReleaseSettings.read(configuration_bytes())
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
        release.DockerRunner().run(["docker", "synthetic-test"])
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
    probes = [arguments[-1] for arguments in docker.calls if "-c" in arguments]
    assert probes.index(release.CANDIDATE_TLS_SCRIPT) < probes.index(release.AUTH_SMOKE_SCRIPT)
    tls_index = next(
        index
        for index, arguments in enumerate(docker.calls)
        if arguments[-1] == release.CANDIDATE_TLS_SCRIPT
    )
    tls_payload = json.loads(docker.inputs[tls_index])
    assert tls_payload == {
        "server_hostname": "192.0.2.10",
        "https_port": 18443,
        "ca_certificate": TEST_CA,
    }
    readiness = [arguments for arguments in docker.calls if "pg_isready" in arguments]
    assert readiness and all(
        "-h" in arguments and "127.0.0.1" in arguments for arguments in readiness
    )
    # 凭据只存在stdin/0600临时文件，不出现在argv、journal或结构日志中。
    output = capsys.readouterr().out
    combined = "\n".join(commands) + json.dumps(journal) + output
    for secret in (TEST_PASSWORD, "synthetic-admin-password", "012345"):
        assert secret not in combined
    assert TEST_CA not in combined
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
    controller.configuration = release.ReleaseSettings.read(
        configuration_bytes(PROCURE_CONFIG_VERSION="production-config-20261102")
    )
    controller.run()
    inspected = release.inspect_state(controller.state)
    assert inspected["config_version"] == "production-config-20261102"


@pytest.mark.parametrize("version", [" ", "x" * 129, '"bad\\tversion"', '"\\ud800"'])
def test_invalid_configuration_version_is_rejected(version):
    """配置版本禁止空白、超长、控制字符与无效UTF8。"""
    with pytest.raises(release.ReleaseError, match="invalid_configuration_version"):
        release.ReleaseSettings.read(configuration_bytes(PROCURE_CONFIG_VERSION=version))


@pytest.mark.parametrize("failure", ["missing_ca", "invalid_ca", "oversized_ca", "tls"])
def test_candidate_tls_failure_never_opens_maintenance(tmp_path, capsys, failure):
    """CA和TLS故障均在开门前中止，不把CA内容或原始TLS错误写入报告。"""
    controller, gate, _ = make_controller(tmp_path, failure)
    certificate = controller.tls / "ca.crt"
    if failure == "missing_ca":
        certificate.unlink()
    elif failure == "invalid_ca":
        certificate.write_text("synthetic invalid certificate")
    elif failure == "oversized_ca":
        certificate.write_bytes(b"A" * (64 * 1024 + 1))
    with pytest.raises(release.ReleaseError, match="candidate_tls_failed"):
        controller.run()
    assert gate.active
    assert False not in gate.events
    journal = json.loads((controller.state / "current.json").read_text())
    assert journal["phase"] == "candidate"
    assert journal["error_code"] == "candidate_tls_failed"
    assert TEST_CA not in capsys.readouterr().out + json.dumps(journal)


@pytest.mark.parametrize("hostname", ["192.0.2.10", "2001:db8::10", "procure-server.local"])
def test_legacy_configuration_uses_lan_bind_as_certificate_hostname(hostname):
    """旧部署配置继续使用原 LAN 地址校验证书。"""
    configuration = release.ReleaseSettings.read(configuration_bytes(PROCURE_LAN_BIND=hostname))
    assert configuration.server_hostname == hostname


@pytest.mark.parametrize("binding", ["0.0.0.0", "::", "192.0.2.10"])
def test_explicit_server_hostname_and_custom_port_are_passed_to_probe(tmp_path, binding):
    """显式主机名与发布端口独立于网卡绑定，并进入候选 HTTPS 探针。"""
    controller, _, docker = make_controller(tmp_path)
    controller.configuration = release.ReleaseSettings.read(
        configuration_bytes(
            PROCURE_LAN_BIND=binding,
            PROCURE_SERVER_HOST="procure-server.local",
            PROCURE_HTTPS_PORT="19443",
        )
    )
    assert controller.configuration.server_hostname == "procure-server.local"
    controller.run()
    tls_index = next(
        index
        for index, arguments in enumerate(docker.calls)
        if arguments[-1] == release.CANDIDATE_TLS_SCRIPT
    )
    assert json.loads(docker.inputs[tls_index]) == {
        "server_hostname": "procure-server.local",
        "https_port": 19443,
        "ca_certificate": TEST_CA,
    }


@pytest.mark.parametrize(
    "hostname",
    [
        "",
        '" "',
        '" procure-server.local"',
        '"procure-server.local "',
        "https://procure-server.local",
        "procure-server.local/path",
        "procure-server.local:18443",
        "procure-server.local?query",
        "procure-server.local#fragment",
        "admin@procure-server.local",
        "*.local",
        "*",
        "0.0.0.0",
        "::",
        "[::]",
        "[2001:db8::10]:18443",
        "procure_server.local",
        "-server.local",
        "server-.local",
        "x" * 64 + ".local",
        "x." * 127 + "local",
    ],
)
def test_invalid_certificate_hostname_is_rejected(hostname):
    """证书目标只接受纯 IP 或主机名，拒绝空白、URL 和监听通配地址。"""
    with pytest.raises(release.ReleaseError, match="invalid_configuration"):
        release.ReleaseSettings.read(configuration_bytes(PROCURE_SERVER_HOST=hostname))


@pytest.mark.parametrize("binding", ["0.0.0.0", "::"])
def test_wildcard_binding_requires_explicit_certificate_hostname(binding):
    """所有网卡监听需要独立配置可验证的证书主机名。"""
    with pytest.raises(release.ReleaseError, match="invalid_configuration"):
        release.ReleaseSettings.read(configuration_bytes(PROCURE_LAN_BIND=binding))


@pytest.fixture
def tls_probe_boundary(monkeypatch):
    """替换网络与 TLS 边界，实际执行同一份发布探针代码。"""
    import http.client
    import socket
    import ssl

    state = SimpleNamespace(
        hostname="192.0.2.10",
        https_port=18443,
        fail_host_mapping=False,
        raw=Mock(),
        requests=[],
    )
    context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)

    def wrap_socket(stream, *, server_hostname):
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert server_hostname == state.hostname
        assert stream is state.raw
        return stream

    def connect_socket(target, *, timeout):
        assert timeout == 10
        if state.fail_host_mapping and target[0] == "host.docker.internal":
            raise ConnectionRefusedError("synthetic private host mapping error")
        return state.raw

    context.wrap_socket.side_effect = wrap_socket
    create_context = Mock(return_value=context)
    connect = Mock(side_effect=connect_socket)

    class Connection:
        def __init__(self, host, port, *, timeout):
            assert timeout == 10
            assert (host, port) in {
                ("gateway", 443),
                ("host.docker.internal", state.https_port),
            }
            self.host, self.port = host, port

        def request(self, method, path, *, headers):
            assert method == "GET" and headers["Host"] == state.hostname
            self.path = path
            state.requests.append((self.host, self.port, path))

        def getresponse(self):
            body = (
                {"status": "alive"}
                if self.path == "/health"
                else {"ready": False, "reason": "maintenance"}
            )
            return SimpleNamespace(status=200, read=lambda: json.dumps(body).encode())

        def close(self):
            pass

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)

    def execute(payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        exec(release.CANDIDATE_TLS_SCRIPT, {})

    state.context = context
    state.create_context = create_context
    state.connect = connect
    state.execute = execute
    return state


@pytest.mark.parametrize(
    ("hostname", "port"), [("192.0.2.10", 18443), ("procure-server.local", 19443)]
)
def test_tls_probe_checks_gateway_and_host_port_with_same_ca_and_sni(
    tls_probe_boundary, capsys, hostname, port
):
    """内部网关与宿主映射均校验 CA、SNI、健康和维护状态。"""
    boundary = tls_probe_boundary
    boundary.hostname = hostname
    boundary.https_port = port
    boundary.execute(
        {"server_hostname": hostname, "https_port": port, "ca_certificate": TEST_CA}
    )
    boundary.create_context.assert_called_once_with(cadata=TEST_CA)
    assert boundary.context.wrap_socket.call_count == 4
    assert [call.args[0] for call in boundary.connect.call_args_list] == [
        ("gateway", 443),
        ("gateway", 443),
        ("host.docker.internal", port),
        ("host.docker.internal", port),
    ]
    assert boundary.requests == [
        ("gateway", 443, "/health"),
        ("gateway", 443, "/api/v1/system"),
        ("host.docker.internal", port, "/health"),
        ("host.docker.internal", port, "/api/v1/system"),
    ]
    assert boundary.raw.close.call_count == 4
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_host_port_mapping_failure_keeps_maintenance_and_safe_diagnostics(
    tmp_path, monkeypatch, capsys, tls_probe_boundary
):
    """内部网关正常而宿主发布端口不可达时，实际探针阻止发布开放。"""
    controller, gate, _ = make_controller(tmp_path)
    original = controller.api_script
    boundary = tls_probe_boundary
    boundary.fail_host_mapping = True

    def api_script(script, *, input_data=None):
        if script == release.CANDIDATE_TLS_SCRIPT:
            boundary.execute(json.loads(input_data))
            return {"ok": True}
        return original(script, input_data=input_data)

    monkeypatch.setattr(controller, "api_script", api_script)
    with pytest.raises(release.ReleaseError, match="candidate_tls_failed"):
        controller.run()
    assert boundary.requests == [
        ("gateway", 443, "/health"),
        ("gateway", 443, "/api/v1/system"),
    ]
    assert boundary.connect.call_args.args[0] == ("host.docker.internal", 18443)
    assert gate.active and False not in gate.events
    journal = json.loads((controller.state / "current.json").read_text())
    assert journal["phase"] == "candidate"
    assert journal["error_code"] == "candidate_tls_failed"
    output = capsys.readouterr().out + json.dumps(journal)
    assert "synthetic private host mapping error" not in output
    assert TEST_CA not in output


@pytest.mark.parametrize(
    "script",
    [
        value
        for name, value in vars(release).items()
        if name.endswith("_SCRIPT") and isinstance(value, str)
    ],
)
def test_embedded_application_imports_resolve(script):
    """验证维护脚本字符串中的应用类名确实可以导入。"""
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), (node.module, alias.name)
