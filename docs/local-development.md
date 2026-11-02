# 本机开发

工程位于 `/Users/sunjiale/Documents/procure-reconcile`，目录固定 `client/`、`server/`。根 `main.py` 是原有 PyCharm 示例，已保留；应用入口是 `server/app/main.py`。

## 准备

Node 24，pnpm 11.25.0，Python 3.13，uv 0.12.10，Rust 1.99.1，macOS 原生编译工具，以及可用的 OrbStack Docker 引擎。

```sh
pnpm install --frozen-lockfile
uv sync --directory server --locked
pnpm run doctor
pnpm dev:configure
pnpm db:dev:up
pnpm db:migrate
pnpm admin:init
```

`dev:configure` 只在开发配置不存在时生成随机PG密码与稳定实例ID，文件权限600，不显示密码。数据库初次初始化后修改env不会自动修改其密码。管理员密码长度6至128个字符，支持6位纯数字，通过交互输入，和PG账号无关；无默认管理员密码或注册入口。

## PyCharm 单独启动服务

- 打开 `server/`，解释器选择 `server/.venv/bin/python`。
- 新建 Python 运行配置，运行模块 `uvicorn`。
- 参数：`app.main:app --host 127.0.0.1 --port 18081 --reload`。
- 工作目录：项目的 `server/`，自动读取该目录 `.env`。
- 初始依赖同步用 `uv sync --directory server --locked`；PyCharm不依赖另一个全局Python环境。

等价终端命令（与PyCharm二选一，避免端口冲突）：

```sh
uv run --directory server --locked uvicorn app.main:app --host 127.0.0.1 --port 18081 --reload
```

`/health` 是存活探针；`/api/v1/system` 返回真实环境、实例、API版本及就绪状态。无数据库、未迁移或未初始化管理员时不能把存活当业务就绪。

## VS Code 单独启动客户端

在根目录执行 `pnpm client:dev`。该命令只启动 Vite/Tauri，不启动或关闭 Python、PG或Docker。开发客户端默认连接 `http://127.0.0.1:18081`，并采用独立应用身份和原生凭据命名空间。

`pnpm client:preview` 只供浏览器查看布局。浏览器缺少Tauri桥，不能实际登录；不会降级到浏览器直连或伪造成功。登录与服务连接需在真实桌面窗口验收。

本次工具链安装于忽略提交的 `.local/cargo`、`.local/rustup`，根客户端脚本会自动发现，不修改系统Shell配置。其他电脑按Tauri官方前置条件安装Rust即可。

## 检查入口

```sh
pnpm check:client
pnpm check:rust
pnpm check:server
pnpm test:domain
pnpm test:api
pnpm test:ui
```

PG集成测试只允许显式提供独立测试数据库，不能连接开发/部署库；未提供测试库时跳过项必须如实记录。具体测试条件见执行日志。

## 正式客户端与Docker

正式客户端在连接设置中保存服务Mac的局域网HTTPS地址、预期环境和实例。其他设备的127.0.0.1指向它自己。开发和正式数据库、文件、凭据不会自动同步。

局域网部署、三平台安装包、Jenkins备份迁移门禁及真实模型均需后续按规划验收，当前骨架不代表这些能力已经交付。

## 隔离PG集成测试

首次另行执行 `pnpm test:pg:configure` 生成随机测试密码（已有配置时保留）。然后：

```sh
pnpm db:test:up
pnpm test:api:pg
docker compose --env-file .local/test-db.env -p procure-test -f deploy/compose.test.yml stop
```

测试固定使用独立procure_test数据库/17432端口，密码从私有文件读取且不输出。测试会清理该测试库认证表；不能将业务库改名后当作测试库。停止tmpfs测试实例后数据消失，下次启动由初始化和迁移重新建立。当前本机临时PG17镜像来源见deploy/README.md。

## PyCharm终端找不到pnpm

前端命令需要普通终端也能访问的pnpm。Node24自带的Corepack可启用项目固定版本：

```sh
corepack install --global pnpm@11.25.0
corepack enable pnpm
```

启用方式见[Corepack官方说明](https://github.com/nodejs/corepack#corepack-enable--name)。启用后重新打开终端标签，在仓库根目录运行`pnpm --version`确认11.25.0，再执行`pnpm client:dev`。`Documents/`是项目父目录，客户端脚本在`procure-reconcile/`根package.json中。

如果Node也找不到，先加载本机已安装的nvm：

```sh
export NVM_DIR="$HOME/.nvm"
. "$NVM_DIR/nvm.sh"
nvm use 24.21.0
```

不把Codex工具缓存目录加到用户PATH；普通终端依赖本机Node/Corepack管理的pnpm。所有前端后端命令均可在PyCharm终端执行。

## uv管理约定

后端独立项目位于server/，uv维护pyproject.toml、uv.lock和.venv。Python选择由.python-version指定3.13，uv版本范围在pyproject中检查。依赖新增/移除分别用uv add/remove；开发工具用dev组。安装和运行使用--locked，CI先检查锁文件；Docker安装--no-dev。后端说明包含完整命令，PyCharm始终选择同一个uv虚拟环境。

项目doctor检查server/.venv实际Python及uv锁文件一致性，不以全局python3替代项目环境。首次同步在doctor前执行。
