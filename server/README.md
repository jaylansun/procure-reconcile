# Python后端与uv项目管理

`server/`是独立的uv项目，Python3.13，uv支持版本为`>=0.12.10,<0.13`，Docker目前固定uv0.12.10。Python依赖、锁定版本和虚拟环境均由uv管理。

| 文件 | 作用 |
| --- | --- |
| pyproject.toml | 运行依赖、dev组、Python及uv版本约束、工具配置 |
| uv.lock | uv生成的跨平台精确版本锁；和pyproject一起维护，不手工编辑 |
| .python-version | Python3.13版本选择 |
| .venv/ | uv维护的项目环境；不提交Git |

以下命令在`server/`执行。也可在仓库根执行`uv <子命令> --directory server ...`。首次准备Python可用`uv python install 3.13`，已有兼容解释器时由uv选择；不要另建IDE虚拟环境。

## 安装与运行

```sh
uv sync --locked
uv run --locked alembic upgrade head
uv run --locked python -m app.cli init-admin --username your-admin-name
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 18081 --reload
```

PyCharm解释器选择`server/.venv/bin/python`（Windows为`.venv/Scripts/python.exe`），工作目录为`server/`；依赖变更后先执行`uv sync --locked`。使用共享的Procure API运行配置时，依然调用同一uv环境，不启动第二套Python。

应用从工作目录读取`.env`，配置`PROCURE_ENVIRONMENT`、稳定的`PROCURE_INSTANCE_ID`、PostgreSQL `PROCURE_DATABASE_URL`、`PROCURE_FILES_DIR`及可选`PROCURE_SESSION_HOURS`（缺省12）。管理员密码长度6至128个字符，允许6位纯数字；初始化管理员必须在交互终端隐藏输入密码，不接受命令行密码，不覆盖已有管理员。应用不会自动迁移或创建默认账号。

## 依赖变更

| 操作 | 命令（替换实际依赖名） |
| --- | --- |
| 新增运行依赖 | `uv add <依赖名>` |
| 新增开发依赖 | `uv add --dev <依赖名>` |
| 移除依赖 | `uv remove <依赖名>`；dev组用`uv remove --dev <依赖名>` |
| 有意升级一个依赖 | `uv lock --upgrade-package <依赖名>`，然后`uv sync --locked`并验证 |
| 检查锁文件一致 | `uv lock --check` |

日常安装与运行采用`--locked`，配置和锁不一致时直接失败，避免静默重算版本。需要改依赖时显式使用uv的变更命令，同时保留声明和锁文件。不用pip、Poetry、Conda或手写requirements作为第二套项目依赖来源。

## 检查与测试

```sh
uv lock --check
uv run --locked ruff check app tests
uv run --locked pytest
```

真实PG集成测试需要显式`TEST_DATABASE_URL`，仅允许独立`test`、`test_*`或`*_test`数据库；会迁移并清理该测试库认证表，不能连接开发或正式库，也不要并行使用同一测试库。仓库根`pnpm test:api:pg`通过uv运行同一套测试，私有密码不输出。

## Docker与CI约定

构建通过`uv sync --locked --no-dev --no-install-project`安装同一锁文件的运行依赖，Python由基础镜像提供，禁止构建时隐式下载另一解释器。容器从uv创建的`.venv`直接启动uvicorn；迁移和管理员CLI同样用该环境。入口不执行uv同步，不在启动时安装dev包或写root拥有的环境。Docker和Jenkins实际构建/发布状态见执行日志。

## 当前API与范围

`GET /health`仅存活；`GET /api/v1/system`返回数据库/schema/管理员就绪，不暴露秘密。实例ID未配置时保持未就绪，不在启动时随机生成。

数据库期望版本由`app/persistence/schema.py`读取本项目`alembic.ini`和迁移脚本的head自动确定，并与数据库`alembic_version`中的完整版本集合比较。新增迁移后无需修改Python版本常量，但仍需显式执行`uv run --locked alembic upgrade head`；就绪检查不会自动迁移。管理员初始化复用同一版本校验。迁移文件缺失、损坏或没有head时保持未就绪，部署必须同时包含`alembic.ini`及`migrations/`。路径按模块位置定位，不依赖启动工作目录。

`POST /api/v1/auth/login`取得随机会话，其他身份/工作台/退出请求使用Bearer；数据库仅存token的SHA-256哈希。多设备会话独立过期/失效。当前单API进程按来源限制失败登录，计数重启会重置，LAN仍须可信HTTPS。

目前实现健康、配置、PG认证及真实连接身份；订单、收退货、完整幂等、文件、业务审计及工作流仍按规划推进。

uv约定依据：[项目结构](https://docs.astral.sh/uv/concepts/projects/layout/)、[锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[Docker集成](https://docs.astral.sh/uv/guides/integration/docker/)。
