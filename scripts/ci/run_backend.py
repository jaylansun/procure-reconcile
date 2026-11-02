"""容器内检查后端；测试输出和JUnit先脱敏，再交给Jenkins。"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

REPORTS = Path("/tmp/procure-ci-reports")
RAW_REPORT = Path("/tmp/procure-ci-backend-raw.xml")


def redact(value: str, secrets: list[str]) -> str:
    """同时遮盖原值及URL编码值，不回显数据库连接中的密码。"""
    variants = {item for secret in secrets for item in (secret, quote(secret, safe="")) if item}
    for secret in sorted(variants, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def run_check(command: list[str], secrets: list[str]) -> int:
    """等子进程完成后输出脱敏日志，启动失败不打印可能截断的结果。"""
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    except OSError:
        print("CI检查进程启动失败。", file=sys.stderr)
        return 1
    if result.returncode < 0:
        print("CI检查进程被信号终止，未输出不完整日志。", file=sys.stderr)
        return 1
    sys.stdout.write(redact(result.stdout, secrets))
    sys.stderr.write(redact(result.stderr, secrets))
    return result.returncode


def main() -> int:
    """必须连接显式的独立测试PG，不能以跳过PG测试冒充CI成功。"""
    supplied = os.environ.get("TEST_DATABASE_URL", "")
    connection = urlsplit(supplied)
    if (
        connection.scheme != "postgresql+psycopg"
        or connection.hostname != "db"
        or connection.path != "/procure_test"
        or not connection.password
    ):
        print("CI仅接受隔离网络内db服务的procure_test数据库。", file=sys.stderr)
        return 2
    secrets = [unquote(connection.password)]
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / "backend-junit.xml"
    commands = [
        ["uv", "lock", "--check", "--offline"],
        ["ruff", "check", "app", "tests", "migrations"],
        ["pytest", "-q", "--tb=short", f"--junitxml={RAW_REPORT}"],
    ]
    status = 0
    try:
        for command in commands:
            print(f"后端CI：{command[0]}检查", flush=True)
            status = run_check(command, secrets)
            if status:
                break
        if status == 0:
            try:
                root = ElementTree.parse(RAW_REPORT).getroot()
                skipped = sum(
                    int(suite.attrib.get("skipped", "0")) for suite in root.iter("testsuite")
                )
            except (OSError, ValueError, ElementTree.ParseError):
                print("CI没有产生有效JUnit结果。", file=sys.stderr)
                status = 1
            else:
                if skipped:
                    print("CI存在跳过的用例，不能宣告完整验证通过。", file=sys.stderr)
                    status = 1
        # 原始JUnit与可归档文件使用不同路径，清理失败也不会复制未脱敏原件。
        if RAW_REPORT.is_file():
            safe = redact(RAW_REPORT.read_text(encoding="utf-8"), secrets)
            report.write_text(safe, encoding="utf-8")
        return status
    except (OSError, UnicodeError):
        print("CI报告无法安全生成，停止发布。", file=sys.stderr)
        return 1
    finally:
        RAW_REPORT.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
