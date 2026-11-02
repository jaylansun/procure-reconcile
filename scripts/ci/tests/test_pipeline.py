"""用假Docker验证发布前CI门禁，不启动容器、不读取本机配置。"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
TAG = f"{REVISION}-7"
SPEC = importlib.util.spec_from_file_location("ci_backend", ROOT / "scripts/ci/run_backend.py")
BACKEND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKEND)

FAKE_DOCKER = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[0] == "build" and os.environ.get("FAKE_FAIL_BUILD"):
    sys.exit(9)
if args[0] == "run":
    print("1" * 48 + ":" + "2" * 48)
elif args[0] == "compose" and "ps" in args:
    print(args[-1] + "-id")
elif args[0] == "wait":
    failed = args[-1] == "backend-id" and os.environ.get("FAKE_FAIL_BACKEND")
    print(1 if failed else 0)
elif args[0] == "logs":
    print("safe test output")
elif args[0] == "cp" and ":/tmp/procure-ci-reports/" in args[1]:
    if os.environ.get("FAKE_MISSING_REPORT"):
        sys.exit(1)
    Path(args[2]).write_text('<testsuites><testsuite tests="1" skipped="0"/></testsuites>')
elif args[:2] == ["image", "inspect"]:
    print("sha256:ci-test")
"""


class ShellPipelineTests(unittest.TestCase):
    """CI必须先通过检查，再允许部署，清理始终限于本次项目。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "scripts/ci", self.root / "scripts/ci")
        (self.root / "bin").mkdir()
        docker = self.root / "bin/docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        docker.chmod(0o700)
        self.log = self.root / "docker.jsonl"
        self.environment = {
            **os.environ,
            "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_LOG": str(self.log),
            "PROCURE_BUILD_PROXY": "http://ci-proxy.invalid:3128",
        }
        for key in ("FAKE_FAIL_BUILD", "FAKE_FAIL_BACKEND", "FAKE_MISSING_REPORT"):
            self.environment.pop(key, None)

    def run_script(self, script, arguments, **extra):
        return subprocess.run(
            ["bash", str(self.root / "scripts/ci" / script), *arguments],
            env={**self.environment, **extra},
            capture_output=True,
            text=True,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_success_uses_only_isolated_project_and_copies_init_script(self):
        result = self.run_script("check.sh", [TAG, REVISION, "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        compose = [call for call in calls if call[0] == "compose"]
        self.assertTrue(compose)
        self.assertTrue(
            all(call[call.index("--project-name") + 1] == "procure-ci-7" for call in compose)
        )
        copy_init = next(
            i
            for i, call in enumerate(calls)
            if call[:2] == ["cp", "deploy/postgres/01-app-role.sh"]
        )
        start_db = next(i for i, call in enumerate(calls) if call[0] == "compose" and "up" in call)
        self.assertLess(copy_init, start_db)
        self.assertIn("down", compose[-1])
        self.assertTrue((self.root / ".local/ci-reports/backend-junit.xml").exists())
        self.assertTrue((self.root / ".local/ci-reports/client-junit.xml").exists())
        self.assertNotIn("1" * 48, result.stdout + result.stderr)
        self.assertNotIn("2" * 48, result.stdout + result.stderr)

    def test_backend_failure_stops_client_and_still_cleans(self):
        result = self.run_script("check.sh", [TAG, REVISION, "7"], FAKE_FAIL_BACKEND="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertNotIn(["start", "client-id"], calls)
        self.assertIn("down", [call for call in calls if call[0] == "compose"][-1])

    def test_missing_report_cannot_be_treated_as_success(self):
        result = self.run_script("check.sh", [TAG, REVISION, "7"], FAKE_MISSING_REPORT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(["start", "client-id"], self.calls())

    def test_build_failure_never_creates_database(self):
        result = self.run_script("check.sh", [TAG, REVISION, "7"], FAKE_FAIL_BUILD="1")
        self.assertEqual(result.returncode, 9)
        self.assertFalse(any(call[0] == "compose" for call in self.calls()))

    def test_bad_revision_and_mismatched_build_stop_before_docker(self):
        for arguments in ([TAG, "main", "7"], [TAG, REVISION, "8"], ["latest", REVISION, "7"]):
            with self.subTest(arguments=arguments):
                result = self.run_script("check.sh", arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.calls(), [])

    def test_release_images_are_tagged_with_exact_revision(self):
        result = self.run_script("build-images.sh", [TAG, REVISION])
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [call for call in self.calls() if call[0] == "build"]
        self.assertEqual(len(builds), 4)
        for call, image in zip(builds, ("api", "ops", "db", "gateway"), strict=True):
            self.assertIn(f"procure-{image}:{TAG}", call)
            self.assertIn(f"org.opencontainers.image.revision={REVISION}", call)
        self.assertIn(f"RUNTIME_IMAGE=procure-api:{TAG}", builds[1])
        self.assertTrue((self.root / ".local/ci-reports/images.txt").exists())
        self.assertFalse(any(call[0] == "compose" for call in self.calls()))

    def test_empty_proxy_is_supported_by_macos_bash(self):
        result = self.run_script(
            "build-images.sh",
            [TAG, REVISION],
            PROCURE_BUILD_PROXY="",
            HTTP_PROXY="",
            HTTPS_PROXY="",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [call for call in self.calls() if call[0] == "build"]
        self.assertEqual(len(builds), 4)
        self.assertTrue(all("HTTP_PROXY=" not in call for call in builds))


class BackendRunnerTests(unittest.TestCase):
    """即使数据库驱动失败，密码也不能进入控制台或可归档JUnit。"""

    def test_redacts_raw_and_encoded_password(self):
        self.assertEqual(BACKEND.redact("a/b a%2Fb", ["a/b"]), "[REDACTED] [REDACTED]")

    def test_signal_hides_incomplete_output(self):
        result = subprocess.CompletedProcess([], -9, stdout="private-fragment", stderr="private")
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(BACKEND.subprocess, "run", return_value=result):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(BACKEND.run_check(["pytest"], ["private-password"]), 1)
        self.assertNotIn("private", output.getvalue() + errors.getvalue())

    def test_startup_error_never_prints_original_exception(self):
        errors = io.StringIO()
        with patch.object(BACKEND.subprocess, "run", side_effect=OSError("private-password")):
            with contextlib.redirect_stderr(errors):
                self.assertEqual(BACKEND.run_check(["pytest"], []), 1)
        self.assertNotIn("private-password", errors.getvalue())

    def test_rejects_nonisolated_url_before_running_checks(self):
        for supplied in (
            "",
            "postgresql+psycopg://app:private@db/procure",
            "postgresql+psycopg://app:private@host/procure_test",
        ):
            with (
                self.subTest(supplied=supplied),
                patch.dict(os.environ, {"TEST_DATABASE_URL": supplied}),
            ):
                with patch.object(BACKEND, "run_check") as check:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(BACKEND.main(), 2)
                    check.assert_not_called()

    def test_junit_is_sanitized_and_skip_fails_gate(self):
        for skipped, expected in ((0, 0), (1, 1)):
            with self.subTest(skipped=skipped), tempfile.TemporaryDirectory() as directory:
                report_dir = Path(directory) / "reports"
                raw = Path(directory) / "raw.xml"

                def check(command, secrets):
                    if command[0] == "pytest":
                        raw.write_text(
                            f'<testsuites><testsuite skipped="{skipped}">'
                            "<system-out>private-password</system-out></testsuite></testsuites>"
                        )
                    return 0

                environment = {
                    "TEST_DATABASE_URL": "postgresql+psycopg://app:private-password@db/procure_test"
                }
                with (
                    patch.dict(os.environ, environment),
                    patch.object(BACKEND, "REPORTS", report_dir),
                    patch.object(BACKEND, "RAW_REPORT", raw),
                    patch.object(BACKEND, "run_check", side_effect=check),
                ):
                    with (
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(BACKEND.main(), expected)
                safe = (report_dir / "backend-junit.xml").read_text()
                self.assertNotIn("private-password", safe)
                self.assertIn("[REDACTED]", safe)
                self.assertFalse(raw.exists())


if __name__ == "__main__":
    unittest.main()
