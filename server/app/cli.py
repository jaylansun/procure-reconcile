import argparse
import getpass
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.persistence.database import Database
from app.security.admin import initialize_administrator
from app.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="受控后端维护入口")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-admin", help="交互初始化唯一管理员，不覆盖已有账号")
    initialize.add_argument("--username", help="管理员名称；密码只能交互输入")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("初始化必须在交互终端运行，密码不得通过命令行或管道传入")
    configured = Settings()
    if not configured.instance_id or not configured.database_url:
        parser.error("请配置稳定PROCURE_INSTANCE_ID和PROCURE_DATABASE_URL")
    database = Database(configured.database_url.get_secret_value())
    try:
        username = args.username or input("管理员名称: ")
        password = getpass.getpass("管理员密码（6至128字符，可用6位数字）: ")
        confirmation = getpass.getpass("再次输入密码: ")
        if password != confirmation:
            raise ValueError("两次密码不一致")
        initialize_administrator(database, username, password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("初始化失败：请检查数据库连接、权限及迁移状态", file=sys.stderr)
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
