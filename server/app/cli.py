import argparse
import getpass
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.documents.service import FileService
from app.documents.storage import FileStore, StorageError
from app.errors import AppError
from app.maintenance import MaintenanceGate
from app.persistence.database import Database
from app.security.admin import initialize_administrator
from app.services.command_executor import CommandExecutor
from app.settings import Settings


def _nonnegative_hours(value: str) -> int:
    """维护宽限时间只接收非负整数小时，防止误写负数立即回收。"""
    try:
        hours = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("宽限小时数必须为非负整数") from None
    if hours < 0:
        raise argparse.ArgumentTypeError("宽限小时数必须为非负整数")
    return hours


def main() -> int:
    parser = argparse.ArgumentParser(description="受控后端维护入口")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-admin", help="交互初始化唯一管理员，不覆盖已有账号")
    initialize.add_argument("--username", help="管理员名称；密码只能交互输入")
    cleanup = commands.add_parser(
        "cleanup-files", help="显式回收超过宽限时间且没有数据库引用的原件"
    )
    cleanup.add_argument("--min-age-hours", type=_nonnegative_hours, default=24)
    args = parser.parse_args()
    if args.command == "init-admin" and not sys.stdin.isatty():
        parser.error("初始化必须在交互终端运行，密码不得通过命令行或管道传入")
    configured = Settings()
    if not configured.instance_id or not configured.database_url:
        parser.error("请配置稳定PROCURE_INSTANCE_ID和PROCURE_DATABASE_URL")
    database = Database(configured.database_url.get_secret_value())
    gate = MaintenanceGate(
        getattr(configured, "maintenance_dir", None) or configured.files_dir.parent / "maintenance"
    )
    try:
        if args.command == "cleanup-files":
            with gate.writer():
                store = FileStore(configured.files_dir, max_bytes=configured.max_file_bytes)
                executor = CommandExecutor(
                    database, lock_timeout_ms=configured.command_lock_timeout_ms
                )
                removed = FileService(database, store, executor).cleanup_orphans(
                    min_age_seconds=args.min_age_hours * 3600
                )
            print(f"已回收{removed}份无数据库引用的原件。")
            return 0
        username = args.username or input("管理员名称: ")
        password = getpass.getpass("管理员密码（6至128字符，可用6位数字）: ")
        confirmation = getpass.getpass("再次输入密码: ")
        if password != confirmation:
            raise ValueError("两次密码不一致")
        with gate.writer():
            initialize_administrator(database, username, password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("维护失败：请检查数据库连接、权限及迁移状态", file=sys.stderr)
        return 1
    except (AppError, StorageError):
        print("维护失败：请检查附件存储、迁移状态及锁等待情况", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("初始化已取消", file=sys.stderr)
        return 1
    finally:
        database.close()
    print("管理员初始化完成；请从客户端登录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
