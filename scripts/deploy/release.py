"""采购项目受控发布：维护、配套备份、隔离恢复验证、迁移与认证冒烟。

秘密只从stdin读取，不回显子进程输出。失败保留维护和journal，绝不自动恢复旧库。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
TERMINAL_STATUS = "succeeded"
MAX_ENV_BYTES = 64 * 1024


class ReleaseError(Exception):
    """仅携带预先定义的安全错误码，不保存stderr或连接内容。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, document: dict) -> None:
    """同目录0600临时文件、fsync和原子替换，避免中断留下半份journal。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def parse_dotenv(raw: bytes) -> dict[str, str]:
    """读取单行dotenv字面量，不执行shell、变量展开、export或重复键覆盖。"""
    if len(raw) > MAX_ENV_BYTES:
        raise ReleaseError("invalid_configuration")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ReleaseError("invalid_configuration") from None
    values: dict[str, str] = {}
    for source in text.splitlines():
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"PROCURE_[A-Z0-9_]+", key) or key in values:
            raise ReleaseError("invalid_configuration")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                raise ReleaseError("invalid_configuration") from None
        elif value.startswith("'"):
            if not value.endswith("'") or len(value) < 2:
                raise ReleaseError("invalid_configuration")
            value = value[1:-1].replace("\\'", "'")
        if not isinstance(value, str) or any(c in value for c in "\0\r\n"):
            raise ReleaseError("invalid_configuration")
        values[key] = value
    return values


@dataclass(frozen=True)
class Configuration:
    """内存中的部署配置不参与repr，bootstrap凭据不进入长期服务环境。"""

    values: dict[str, str] = field(repr=False)

    @classmethod
    def read(cls, raw: bytes) -> Configuration:
        values = parse_dotenv(raw)
        required = (
            "PROCURE_INSTANCE_ID",
            "PROCURE_DATABASE_URL",
            "PROCURE_PG_ADMIN_PASSWORD",
            "PROCURE_PG_APP_PASSWORD",
            "PROCURE_BOOTSTRAP_USERNAME",
            "PROCURE_BOOTSTRAP_PASSWORD",
            "PROCURE_LAN_BIND",
            "PROCURE_HTTPS_PORT",
        )
        if any(not values.get(key) for key in required):
            raise ReleaseError("missing_configuration")
        try:
            url = urlsplit(values["PROCURE_DATABASE_URL"])
            valid_url = (
                url.scheme == "postgresql+psycopg"
                and url.hostname == "db"
                and url.port == 5432
                and url.path == "/procure"
                and unquote(url.username or "") == "procure_app"
                and unquote(url.password or "") == values["PROCURE_PG_APP_PASSWORD"]
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ReleaseError("wrong_database_target")
        if values["PROCURE_PG_ADMIN_PASSWORD"] == values["PROCURE_PG_APP_PASSWORD"]:
            raise ReleaseError("credentials_not_independent")
        if not 6 <= len(values["PROCURE_BOOTSTRAP_PASSWORD"]) <= 128:
            raise ReleaseError("invalid_bootstrap_configuration")
        username = values["PROCURE_BOOTSTRAP_USERNAME"]
        if (
            username != username.strip()
            or not 1 <= len(username) <= 64
            or any(ord(character) < 32 for character in username)
        ):
            raise ReleaseError("invalid_bootstrap_configuration")
        if (
            values["PROCURE_INSTANCE_ID"] != values["PROCURE_INSTANCE_ID"].strip()
            or len(values["PROCURE_INSTANCE_ID"]) > 128
        ):
            raise ReleaseError("invalid_configuration")
        port = values["PROCURE_HTTPS_PORT"]
        if not port.isascii() or not port.isdecimal() or not 1024 <= int(port) <= 65535:
            raise ReleaseError("invalid_configuration")
        config_version = values.get("PROCURE_CONFIG_VERSION", "unversioned")
        if (
            not config_version.strip()
            or len(config_version) > 128
            or any(unicodedata.category(character) in {"Cc", "Cf"} for character in config_version)
        ):
            raise ReleaseError("invalid_configuration_version")
        try:
            config_version.encode("utf-8")
        except UnicodeError:
            raise ReleaseError("invalid_configuration_version") from None
        return cls(values)

    def compose_values(self, image_tag: str) -> dict[str, str]:
        return {
            **{key: value for key, value in self.values.items() if "BOOTSTRAP" not in key},
            "PROCURE_IMAGE_TAG": image_tag,
        }

    def bootstrap(self) -> bytes:
        return json.dumps(
            {
                "username": self.values["PROCURE_BOOTSTRAP_USERNAME"],
                "password": self.values["PROCURE_BOOTSTRAP_PASSWORD"],
            }
        ).encode("utf-8")


def write_env(path: Path, values: dict[str, str], *, compose: bool = True) -> None:
    """Compose单引号阻止美元变量展开，文件仅当前用户可读。"""
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        for key, value in sorted(values.items()):
            if any(character in value for character in "\0\r\n") or value.endswith("\\"):
                raise ReleaseError("unsupported_configuration_literal")
            if compose:
                escaped = value.replace("'", "\\'")
                stream.write(f"{key}='{escaped}'\n")
            else:
                # docker run --env-file按字面读取，不采用Compose的引号语法。
                stream.write(f"{key}={value}\n")
        stream.flush()
        os.fsync(stream.fileno())


class Runner:
    """所有Docker调用使用参数数组，捕获输出并只向上抛固定安全错误码。"""

    def run(
        self,
        arguments: list[str],
        *,
        input_data: bytes | BinaryIO | None = None,
        output: BinaryIO | None = None,
        timeout: int = 180,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        options: dict = {
            "stdout": output if output is not None else subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": timeout,
            "check": False,
        }
        if isinstance(input_data, bytes):
            options["input"] = input_data
        elif input_data is not None:
            options["stdin"] = input_data
        try:
            result = subprocess.run(arguments, **options)
        except (OSError, subprocess.SubprocessError):
            raise ReleaseError("docker_command_failed") from None
        if check and result.returncode:
            raise ReleaseError("docker_command_failed")
        return result


@contextmanager
def target_lock(state: Path, timeout_seconds: float = 60):
    """独立于Jenkins job的目标flock；中断时由内核释放锁。"""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(state / "deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReleaseError("deployment_busy") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_files(source: Path, destination: Path) -> list[dict]:
    """在维护独占期间备份附件；拒绝符号链接、设备和目录外跳转。"""
    source.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink():
        raise ReleaseError("unsafe_attachment_path")
    manifest = []
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for directory, children, names in os.walk(source, followlinks=False):
                for name in children:
                    if (Path(directory) / name).is_symlink():
                        raise ReleaseError("unsafe_attachment_path")
                for name in sorted(names):
                    path = Path(directory) / name
                    relative = Path("files") / path.relative_to(source)
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise ReleaseError("unsafe_attachment_path")
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise ReleaseError("unsafe_attachment_path")
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        stream.seek(0)
                        member = tarfile.TarInfo(relative.as_posix())
                        member.size, member.mode, member.mtime = info.st_size, 0o600, info.st_mtime
                        archive.addfile(member, stream)
                        manifest.append(
                            {"path": relative.as_posix(), "size": info.st_size, "sha256": digest}
                        )
        output.flush()
        os.fsync(output.fileno())
    return manifest


def verify_backup(directory: Path, manifest: dict) -> None:
    """读取实际dump/tar验证SHA256及完整附件集合，禁止仅生成清单不校验。"""
    for filename in ("database.dump", "files.tar.gz"):
        if file_sha256(directory / filename) != manifest["sha256"][filename]:
            raise ReleaseError("backup_hash_mismatch")
    expected = {item["path"]: item for item in manifest["files"]}
    seen: set[str] = set()
    with tarfile.open(directory / "files.tar.gz", "r:gz") as archive:
        for member in archive:
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise ReleaseError("backup_manifest_mismatch")
            stream = archive.extractfile(member)
            if stream is None:
                raise ReleaseError("backup_manifest_mismatch")
            with stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entry = expected[member.name]
            if member.size != entry["size"] or digest != entry["sha256"]:
                raise ReleaseError("backup_manifest_mismatch")
            seen.add(member.name)
    if seen != set(expected):
        raise ReleaseError("backup_manifest_mismatch")


BOOTSTRAP_SCRIPT = r"""
import json, sys
from app.persistence.database import Database
from app.persistence.models import Administrator
from app.security.admin import initialize_administrator
from app.security.passwords import verify_password
from app.settings import Settings
data = json.load(sys.stdin)
settings = Settings()
database = Database(settings.database_url.get_secret_value())
try:
    with database.sessions() as session:
        existing = session.get(Administrator, 1)
        if existing is not None:
            matches = verify_password(data['password'], existing.password_hash)
            if existing.username != data['username'] or not matches:
                raise RuntimeError('existing administrator differs')
    if existing is None:
        initialize_administrator(database, data['username'], data['password'])
    print(json.dumps({'ok': True}))
finally:
    database.close()
"""

CANDIDATE_SCRIPT = r"""
import json, secrets
from pathlib import Path
from app.documents.storage import FileStore
from app.maintenance.gate import MaintenanceGate
from app.persistence.database import Database
from app.persistence.schema import expected_schema_heads
from app.readiness import check_readiness
from app.settings import Settings
settings = Settings()
database = Database(settings.database_url.get_secret_value())
staged = None
published = None
store = FileStore(settings.files_dir, max_bytes=settings.max_file_bytes)
try:
    if not MaintenanceGate(Path('/app/data/maintenance')).is_maintenance():
        raise RuntimeError('maintenance missing')
    if not check_readiness(settings, database).ready:
        raise RuntimeError('underlying readiness failed')
    content = secrets.token_bytes(min(settings.max_file_bytes, 32))
    staged = store.stage([content])
    published = store.publish(staged)
    with store.open(published.sha256) as stream:
        if stream.read() != content:
            raise RuntimeError('file probe failed')
    print(json.dumps({'ok': True, 'schema': sorted(expected_schema_heads())}))
finally:
    if published is not None and published.created:
        store.remove(published.sha256)
    if staged is not None:
        store.discard(staged)
    database.close()
"""

MAINTENANCE_HTTP_SCRIPT = r"""
import json, urllib.request
with urllib.request.urlopen('http://api:8000/health', timeout=10) as response:
    if json.load(response).get('status') != 'alive':
        raise RuntimeError('liveness failed')
with urllib.request.urlopen('http://api:8000/api/v1/system', timeout=10) as response:
    result = json.load(response)
    if result.get('ready') is not False or result.get('reason') != 'maintenance':
        raise RuntimeError('maintenance readiness failed')
print(json.dumps({'ok': True}))
"""

CANDIDATE_TLS_SCRIPT = r"""
import http.client, json, socket, ssl, sys
configuration = json.load(sys.stdin)
hostname = configuration['server_hostname']
context = ssl.create_default_context(cadata=configuration['ca_certificate'])
for path in ('/health', '/api/v1/system'):
    connection = http.client.HTTPConnection('gateway', 443, timeout=10)
    raw = socket.create_connection(('gateway', 443), timeout=10)
    try:
        # TCP目标是内部服务名，证书验证始终使用真实LAN地址及受信任CA。
        connection.sock = context.wrap_socket(raw, server_hostname=hostname)
        connection.request('GET', path, headers={'Host': hostname, 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('TLS endpoint status failed')
        document = json.loads(response.read())
        if path == '/health' and document.get('status') != 'alive':
            raise RuntimeError('TLS liveness failed')
        if path == '/api/v1/system' and (
            document.get('ready') is not False or document.get('reason') != 'maintenance'
        ):
            raise RuntimeError('TLS maintenance readiness failed')
    finally:
        connection.close()
        raw.close()
print(json.dumps({'ok': True}))
"""

AUTH_SMOKE_SCRIPT = r"""
import json, sys, urllib.request
from app.settings import Settings
settings = Settings()
credentials = json.load(sys.stdin)
base = 'http://api:8000'
def call(path, method='GET', payload=None, token=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        content = response.read()
        return response.status, json.loads(content) if content else None
status, system = call('/api/v1/system')
if not system.get('ready') or system.get('instance_id') != settings.instance_id:
    raise RuntimeError('public readiness failed')
status, login = call('/api/v1/auth/login', 'POST', {**credentials, 'device_name': 'release-smoke'})
token = login['token']
try:
    status, identity = call('/api/v1/auth/session', token=token)
    status, workspace = call('/api/v1/workspace', token=token)
    if (
        identity.get('username') != credentials['username']
        or workspace.get('instance_id') != settings.instance_id
    ):
        raise RuntimeError('authenticated read failed')
finally:
    status, _ = call('/api/v1/auth/logout', 'POST', token=token)
    if status != 204:
        raise RuntimeError('logout failed')
print(json.dumps({'ok': True}))
"""


class Controller:
    """可注入Docker runner和维护门禁，方便验证失败时仍保持停写。"""

    def __init__(
        self,
        configuration: Configuration,
        image_tag: str,
        commit_sha: str,
        build_number: str,
        *,
        state: Path = Path("/state"),
        backups: Path = Path("/backups"),
        data: Path = Path("/app/data"),
        tls: Path = Path("/tls"),
        root: Path = ROOT,
        runner: Runner | None = None,
        gate=None,
    ):
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", image_tag)
            or not re.fullmatch(r"[0-9a-f]{40}", commit_sha)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", build_number)
        ):
            raise ReleaseError("invalid_release_identity")
        self.configuration, self.tag, self.sha, self.build = (
            configuration,
            image_tag,
            commit_sha,
            build_number,
        )
        self.release_id = f"{build_number}-{commit_sha[:12]}"
        self.state, self.backups, self.data, self.root = state, backups, data, root
        self.tls = tls
        self.runner, self.gate = runner or Runner(), gate
        self.journal: dict = {}
        self.compose_command: list[str] = []

    def event(self, phase: str) -> None:
        self.journal.update(phase=phase, updated_at=timestamp())
        atomic_json(self.state / "current.json", self.journal)
        print(
            json.dumps({"release_id": self.release_id, "phase": phase, "status": "running"}),
            flush=True,
        )

    def compose(self, *arguments: str, **options):
        return self.runner.run([*self.compose_command, *arguments], **options)

    def api_script(self, script: str, *, input_data: bytes | None = None) -> dict:
        result = self.compose(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "python",
            "api",
            "-c",
            script,
            input_data=input_data,
        )
        try:
            document = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ReleaseError("invalid_probe_response") from None
        if not isinstance(document, dict) or document.get("ok") is not True:
            raise ReleaseError("probe_failed")
        return document

    def _images(self) -> dict[str, str]:
        images = {}
        for service in ("api", "db", "gateway"):
            result = self.runner.run(
                ["docker", "image", "inspect", f"procure-{service}:{self.tag}"]
            )
            try:
                inspected = json.loads(result.stdout)[0]
                image_id = inspected["Id"]
                labels = inspected.get("Config", {}).get("Labels") or {}
            except (ValueError, TypeError, KeyError, IndexError):
                raise ReleaseError("invalid_image_metadata") from None
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ReleaseError("invalid_image_metadata")
            if service != "db" and labels.get("org.opencontainers.image.revision") != self.sha:
                raise ReleaseError("image_revision_mismatch")
            images[service] = image_id
        return images

    def _psql(self, container: str, sql: str) -> str:
        result = self.runner.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-X",
                "-At",
                "-U",
                "postgres",
                "-d",
                "procure",
                "-c",
                sql,
            ]
        )
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeError:
            raise ReleaseError("invalid_database_probe") from None

    def _snapshot(self, container: str) -> dict:
        schema = []
        if self._psql(
            container, "SELECT COALESCE(to_regclass('public.alembic_version')::text, '')"
        ):
            schema = self._psql(
                container, "SELECT version_num FROM public.alembic_version ORDER BY version_num"
            ).splitlines()
        admins = 0
        if self._psql(container, "SELECT COALESCE(to_regclass('public.administrators')::text, '')"):
            try:
                admins = int(self._psql(container, "SELECT count(*) FROM public.administrators"))
            except ValueError:
                raise ReleaseError("invalid_database_probe") from None
        return {"schema": schema, "administrators": admins}

    def _backup(self) -> tuple[Path, dict]:
        directory = self.backups / self.release_id
        directory.mkdir(parents=True, mode=0o700)
        snapshot = self._snapshot("procure-db-1")
        temporary = directory / "database.dump.part"
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            self.runner.run(
                [
                    "docker",
                    "exec",
                    "procure-db-1",
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    "procure",
                    "-Fc",
                ],
                output=stream,
                timeout=600,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "database.dump")
        if not (directory / "database.dump").stat().st_size:
            raise ReleaseError("empty_database_backup")
        files = archive_files(self.data / "files", directory / "files.tar.gz.part")
        os.replace(directory / "files.tar.gz.part", directory / "files.tar.gz")
        manifest = {
            "release_id": self.release_id,
            "commit_sha": self.sha,
            "created_at": timestamp(),
            "schema": snapshot["schema"],
            "files": files,
            "sha256": {
                name: file_sha256(directory / name) for name in ("database.dump", "files.tar.gz")
            },
        }
        atomic_json(directory / "manifest.json", manifest)
        verify_backup(directory, manifest)
        self.journal["backup"] = {
            "reference": self.release_id,
            "manifest_sha256": file_sha256(directory / "manifest.json"),
        }
        self.journal["schema_before"] = snapshot["schema"]
        return directory, snapshot

    def _restore_verify(self, directory: Path, expected: dict, temporary: Path) -> None:
        """恢复到独立无发布端口的PG，验证schema/管理员数量后只清理临时资源。"""
        name = f"procure-restore-{self.build}"
        network, volume = f"{name}-net", f"{name}-data"
        made_network = made_volume = made_container = False
        failure = False
        credentials = temporary / "restore.env"
        write_env(
            credentials,
            {
                "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
                "POSTGRES_DB": "procure",
                "PROCURE_APP_PASSWORD": secrets.token_urlsafe(32),
            },
            compose=False,
        )
        try:
            self.runner.run(["docker", "network", "create", "--internal", network])
            made_network = True
            existing_volume = self.runner.run(["docker", "volume", "inspect", volume], check=False)
            if existing_volume.returncode == 0:
                raise ReleaseError("restore_resource_exists")
            self.runner.run(["docker", "volume", "create", volume])
            made_volume = True
            self.runner.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    network,
                    "--env-file",
                    str(credentials),
                    "--mount",
                    f"type=volume,source={volume},target=/var/lib/postgresql/data",
                    f"procure-db:{self.tag}",
                ]
            )
            made_container = True
            for attempt in range(60):
                result = self.runner.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "postgres",
                        "-d",
                        "procure",
                    ],
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    break
                if attempt == 59:
                    raise ReleaseError("restore_database_not_ready")
                time.sleep(1)
            with (directory / "database.dump").open("rb") as stream:
                self.runner.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "pg_restore",
                        "--exit-on-error",
                        "--clean",
                        "--if-exists",
                        "--no-owner",
                        "--no-privileges",
                        "-U",
                        "postgres",
                        "-d",
                        "procure",
                    ],
                    input_data=stream,
                    timeout=600,
                )
            if self._snapshot(name) != expected:
                raise ReleaseError("restore_content_mismatch")
        except BaseException:
            failure = True
            raise
        finally:
            cleanup_failed = False
            cleanup = []
            if made_container:
                cleanup.append(["docker", "rm", "-f", name])
            if made_volume:
                cleanup.append(["docker", "volume", "rm", volume])
            if made_network:
                cleanup.append(["docker", "network", "rm", network])
            for arguments in cleanup:
                try:
                    self.runner.run(arguments)
                except ReleaseError:
                    cleanup_failed = True
            credentials.unlink(missing_ok=True)
            if cleanup_failed and not failure:
                raise ReleaseError("restore_cleanup_failed")

    def _previous(self) -> None:
        path = self.state / "current.json"
        if not path.exists():
            return
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ReleaseError("release_state_invalid") from None
        if not isinstance(previous, dict) or previous.get("status") != TERMINAL_STATUS:
            raise ReleaseError("incomplete_release_requires_inspection")

    def _assert_running_images(self) -> None:
        for service in ("api", "gateway"):
            result = self.runner.run(
                [
                    "docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    "{{.Image}}",
                    f"procure-{service}-1",
                ]
            )
            if result.stdout.decode().strip() != self.journal["images"][service]:
                raise ReleaseError("running_image_mismatch")

    def _candidate_tls(self) -> None:
        """只读公共CA经stdin交探针；证书或网关故障统一阻止开放。"""
        try:
            with (self.tls / "ca.crt").open("rb") as stream:
                certificate = stream.read(64 * 1024 + 1)
            if not certificate or len(certificate) > 64 * 1024:
                raise ValueError("invalid CA size")
            ca_certificate = certificate.decode("ascii")
            # 在ops侧先拒绝无效CA，API探针还会独立验证网关证书和主机名。
            ssl.create_default_context(cadata=ca_certificate)
            payload = json.dumps(
                {
                    "server_hostname": self.configuration.values["PROCURE_LAN_BIND"],
                    "ca_certificate": ca_certificate,
                }
            ).encode("utf-8")
            self.api_script(CANDIDATE_TLS_SCRIPT, input_data=payload)
        except Exception:
            raise ReleaseError("candidate_tls_failed") from None

    def run(self) -> dict:
        with target_lock(self.state):
            self._previous()
            self.journal = {
                "format_version": 1,
                "release_id": self.release_id,
                "commit_sha": self.sha,
                "image_tag": self.tag,
                "config_version": self.configuration.values.get(
                    "PROCURE_CONFIG_VERSION", "unversioned"
                ),
                "status": "running",
                "started_at": timestamp(),
            }
            frozen = False
            with tempfile.TemporaryDirectory(prefix="procure-release-") as temporary_directory:
                temporary = Path(temporary_directory)
                write_env(temporary / "deploy.env", self.configuration.compose_values(self.tag))
                self.compose_command = [
                    "docker",
                    "compose",
                    "--env-file",
                    str(temporary / "deploy.env"),
                    "-p",
                    "procure",
                    "-f",
                    str(self.root / "deploy/compose.yml"),
                    "-f",
                    str(self.root / "deploy/compose.lan.yml"),
                ]
                if self.gate is None:
                    from app.maintenance.gate import MaintenanceGate

                    self.gate = MaintenanceGate(self.data / "maintenance")
                try:
                    self.event("preflight")
                    self.journal["images"] = self._images()
                    self.compose(
                        "up", "-d", "--no-recreate", "--wait", "--wait-timeout", "90", "db"
                    )
                    self.event("maintenance")
                    self.gate.set_maintenance(True, release_id=self.release_id)
                    frozen = True
                    self.compose("stop", "--timeout", "60", "gateway", "api")
                    with self.gate.exclusive(timeout_seconds=60):
                        self.event("drained")
                        active = self._psql(
                            "procure-db-1",
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = 'procure' "
                            "AND pid <> pg_backend_pid() AND backend_type = 'client backend'",
                        )
                        if active != "0":
                            raise ReleaseError("database_writers_not_drained")
                        self.event("backup")
                        directory, expected = self._backup()
                        self.event("restore_verification")
                        self._restore_verify(directory, expected, temporary)
                        self.event("migration")
                        self.compose(
                            "run",
                            "--rm",
                            "-T",
                            "--no-deps",
                            "api",
                            "alembic",
                            "upgrade",
                            "head",
                            timeout=600,
                        )
                        self.api_script(BOOTSTRAP_SCRIPT, input_data=self.configuration.bootstrap())
                        self.journal["schema_after"] = self.api_script(CANDIDATE_SCRIPT)["schema"]
                        self.event("candidate")
                        self.compose(
                            "up",
                            "-d",
                            "--no-deps",
                            "--wait",
                            "--wait-timeout",
                            "90",
                            "api",
                            "gateway",
                        )
                        self._assert_running_images()
                        self.api_script(MAINTENANCE_HTTP_SCRIPT)
                        self._candidate_tls()
                        self.event("opening")
                        self.gate.set_maintenance(False, release_id=self.release_id)
                    self.event("authenticated_smoke")
                    self.api_script(AUTH_SMOKE_SCRIPT, input_data=self.configuration.bootstrap())
                    self.journal.update(
                        status="succeeded", phase="succeeded", completed_at=timestamp()
                    )
                    atomic_json(self.state / "current.json", self.journal)
                    atomic_json(self.state / "history" / f"{self.release_id}.json", self.journal)
                    print(
                        json.dumps(
                            {
                                "release_id": self.release_id,
                                "phase": "succeeded",
                                "status": "succeeded",
                                "report": self.journal,
                            }
                        ),
                        flush=True,
                    )
                    return self.journal
                except BaseException as error:
                    # 发布已开放后的冒烟失败也重新关闭门禁；绝不恢复旧数据库。
                    if frozen:
                        try:
                            self.gate.set_maintenance(True, release_id=self.release_id)
                        except Exception:
                            pass
                        try:
                            self.compose("stop", "--timeout", "60", "gateway", "api")
                        except Exception:
                            pass
                    code = error.code if isinstance(error, ReleaseError) else "release_failed"
                    self.journal.update(status="failed", error_code=code, updated_at=timestamp())
                    atomic_json(self.state / "current.json", self.journal)
                    atomic_json(self.state / "history" / f"{self.release_id}.json", self.journal)
                    print(
                        json.dumps(
                            {
                                "status": "failed",
                                "phase": self.journal["phase"],
                                "report": self.journal,
                            }
                        ),
                        flush=True,
                    )
                    raise ReleaseError(code) from None


def inspect_state(state: Path = Path("/state")) -> dict:
    """只读查看安全字段，不更改状态、解除维护或自动恢复任何数据。"""
    try:
        document = json.loads((state / "current.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "not_deployed"}
    except (ValueError, OSError):
        raise ReleaseError("release_state_invalid") from None
    allowed = {
        "release_id",
        "commit_sha",
        "image_tag",
        "config_version",
        "status",
        "phase",
        "images",
        "backup",
        "schema_before",
        "schema_after",
        "started_at",
        "updated_at",
        "completed_at",
        "error_code",
    }
    if not isinstance(document, dict):
        raise ReleaseError("release_state_invalid")
    return {key: value for key, value in document.items() if key in allowed}


def main() -> int:
    parser = argparse.ArgumentParser(description="受控采购发布，不自动回滚数据库")
    parser.add_argument("image_tag", nargs="?")
    parser.add_argument("commit_sha", nargs="?")
    parser.add_argument("build_number", nargs="?")
    parser.add_argument("--resume-inspect", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.resume_inspect:
            print(json.dumps(inspect_state(), ensure_ascii=False))
            return 0
        if not all((arguments.image_tag, arguments.commit_sha, arguments.build_number)):
            raise ReleaseError("missing_release_identity")
        configuration = Configuration.read(sys.stdin.buffer.read(MAX_ENV_BYTES + 1))
        controller = Controller(
            configuration, arguments.image_tag, arguments.commit_sha, arguments.build_number
        )

        def interrupted(signum, frame):
            raise ReleaseError("deployment_interrupted")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        controller.run()
        return 0
    except Exception as error:
        code = error.code if isinstance(error, ReleaseError) else "release_failed"
        print(json.dumps({"status": "failed", "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
