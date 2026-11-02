# procure-reconcile

本地采购管理与智能对账桌面应用。桌面端：Tauri 2 + React + TypeScript；服务端：FastAPI + PostgreSQL。开发时由 PyCharm 和 VS Code 分别启动服务与客户端，部署时服务进入本机 OrbStack Docker。

当前已建立连接设置、真实服务握手、单管理员登录，以及后端命令幂等、事务/来源锁、审计和原件上传下载公共代码，并提供Jenkins构建及受控Docker发布入口；实际验收范围见执行日志。供应商、订单、收退货、核对、文件解析和AI按规划逐步开发。

## 目录

- `client/`：桌面界面与Rust原生连接桥。
- `server/`：Python API、PostgreSQL迁移、管理员与会话。
- `deploy/`：独立开发/测试/部署容器配置，LAN TLS配置草案。
- `scripts/`：独立启动、检查及开发配置生成入口。
- `docs/implementation-plan.md`：已确认需求与58项实施任务。
- `docs/execution-log.md`：本轮实际修改、验收及下一步。
- `docs/backend-business-guide.md`：公共代码用法、业务命令接入和原件API说明。

原有根 `main.py` PyCharm示例保留，应用入口为 `server/app/main.py`。

## Python项目管理

后端统一由uv管理，项目根为 `server/`。依赖声明 `server/pyproject.toml`、锁文件 `server/uv.lock`、Python版本 `server/.python-version`，虚拟环境由uv放在 `server/.venv/`。前端包与Tauri命令继续由pnpm管理。

在 `server/` 中使用 `uv add` / `uv add --dev` / `uv remove` 管理依赖，使用 `uv sync --locked` 安装、`uv run --locked` 运行；`pyproject.toml`和`uv.lock`一起维护。根pnpm后端快捷入口也调用uv。详细命令见 [后端说明](server/README.md)。

## 本机启动

先准备Node24、pnpm11.25.0、uv/Python3.13、Rust1.99.1及可用的OrbStack引擎。普通终端首次可通过 `corepack install --global pnpm@11.25.0`、`corepack enable pnpm`启用项目版本，详情见本机开发说明。

```sh
pnpm install --frozen-lockfile
uv sync --directory server --locked
pnpm dev:configure
pnpm db:dev:up
pnpm db:migrate
pnpm admin:init
```

`admin:init`交互设置管理员密码，长度6至128个字符，支持6位纯数字，不存在默认密码。`dev:configure`只生成独立开发PG/API配置，不覆盖已有文件、不显示密码。

PyCharm选择 `server/.venv/bin/python`，工作目录 `server/`，运行模块 `uvicorn`，参数 `app.main:app --host 127.0.0.1 --port 18081 --reload`。打开根目录时可使用共享的 `.run/Procure API.run.xml` 配置。

VS Code在根目录执行 `pnpm client:dev`。客户端命令不会自动起停Python或Docker；关闭桌面窗口也不会停止服务。浏览器布局预览另用 `pnpm client:preview`，不能替代原生登录验收。

完整说明见 [本机开发](docs/local-development.md) 与 [容器基础](deploy/README.md)。正式其他设备连接服务Mac的局域网HTTPS地址，在桌面“服务连接”中配置，不能填写其他设备自身的127.0.0.1。

Jenkins日常发布见[构建部署指南](docs/deployment/jenkins.md)：可信main检查通过后构建SHA镜像，维护备份与显式迁移后再开放。开发启动与正式发布仍独立。

## 验证与状态

```sh
pnpm run doctor
pnpm check
pnpm test:domain
pnpm test:api
pnpm test:ui
```

测试、原生编译、数据库和容器的实际通过范围见 [执行日志](docs/execution-log.md)。未提供隔离测试PG时跳过的集成测试不算已通过。全部任务状态依据实际证据更新，规划文档不代表功能已经完成。
