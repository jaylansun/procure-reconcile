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

应用从工作目录读取`.env`，配置`PROCURE_ENVIRONMENT`、稳定的`PROCURE_INSTANCE_ID`、PostgreSQL `PROCURE_DATABASE_URL`、`PROCURE_FILES_DIR`及可选`PROCURE_SESSION_HOURS`（缺省12）、`PROCURE_MAX_FILE_BYTES`（缺省25MiB，可配置技术上限）、`PROCURE_COMMAND_LOCK_TIMEOUT_MS`（缺省3000毫秒）。管理员密码长度6至128个字符，允许6位纯数字；初始化管理员必须在交互终端隐藏输入密码，不接受命令行密码，不覆盖已有管理员。应用不会自动迁移或创建默认账号。

## 后端目录与阅读顺序

后端采用已确认的分层目录，入口为`app/main.py`。以供应商模块为例，沿下面的文件顺序阅读。

| 层 | 入口 | 职责 |
| --- | --- | --- |
| Controller | `app/controller/supplier_controller.py` | 接收DTO、QO，注入依赖，调用Service，声明响应VO |
| Service | `app/service/supplier_service.py`中的`SupplierService` | 业务校验、版本检查、业务修改和审计内容 |
| Repository | `app/repository/supplier_repository.py`中的`SupplierRepository` | 在调用方Session中查询、保存和刷新DO |
| Entity | `app/entity/supplier_do.py`中的`SupplierDO` | 表、字段与数据库约束 |
| Domain | `app/domain/dto/`、`qo/`、`vo/` | 写入命令、查询条件及展示数据 |
| Config | `app/config/settings.py` | 运行配置 |
| Common | `app/common/` | 认证依赖、命令执行、来源锁、审计、原件存储和维护门禁 |
| Persistence | `app/persistence/` | 数据库连接、迁移版本检查及完整模型注册 |

每个DO、DTO、QO、VO独立建文件并保留对应的`_do.py`、`_dto.py`、`_qo.py`、`_vo.py`后缀。业务服务使用`*_service.py`与`*Service`类，数据库访问使用`*_repository.py`与`*Repository`类。新增类配中文文档字符串，说明用途。

新增DO需要同步注册到`app/persistence/model_registry.py`，所有映射共享`app/entity/base_do.py`中的`BaseDO`。业务Service及Repository复用`CommandContext.session`，由`CommandExecutor`统一提交业务变更、命令记录和审计。金额与数量计算入口分别为`MoneyService`和`QuantityService`。业务手写与助手负责基础设施、迁移和测试的分工见[后端业务开发指南](../docs/backend-business-guide.md)。

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

真实PG集成测试需要显式`TEST_DATABASE_URL`，仅允许独立`test`、`test_*`或`*_test`数据库；会迁移并清理该测试库认证、命令、审计、原件及基础资料表，不能连接开发或正式库，也不要并行使用同一测试库。仓库根`pnpm test:api:pg`通过uv运行同一套测试，私有密码不输出。

## Docker与CI约定

构建通过`uv sync --locked --no-dev --no-install-project`安装同一锁文件的运行依赖，Python由基础镜像提供，禁止构建时隐式下载另一解释器。容器从uv创建的`.venv`直接启动uvicorn；迁移和管理员CLI同样用该环境。入口不执行uv同步，不在启动时安装dev包或写root拥有的环境。Docker和Jenkins实际构建/发布状态见执行日志。

## 当前API与范围

`GET /health`仅存活；`GET /api/v1/system`返回数据库/schema/管理员就绪，不暴露秘密。实例ID未配置时保持未就绪，不在启动时随机生成。

数据库期望版本由`app/persistence/schema.py`读取本项目`alembic.ini`和迁移脚本的head自动确定，并与数据库`alembic_version`中的完整版本集合比较。新增迁移后无需修改Python版本常量，但仍需显式执行`uv run --locked alembic upgrade head`；就绪检查不会自动迁移。管理员初始化复用同一版本校验。迁移文件缺失、损坏或没有head时保持未就绪，部署必须同时包含`alembic.ini`及`migrations/`。路径按模块位置定位，不依赖启动工作目录。

`POST /api/v1/auth/login`取得随机会话，其他身份/工作台/退出请求使用Bearer；数据库仅存token的SHA-256哈希。多设备会话独立过期/失效。当前单API进程按来源限制失败登录，计数重启会重置，LAN仍须可信HTTPS。

目前实现健康、配置、PG认证、命令幂等与事务、来源锁、审计、原件上传/下载及去重存储，以及供应商新增、修改、详情和分页接口。计量单位手写代码已补齐模型注册、迁移和四个HTTP接口，真实PG集成及本轮585项后端回归全部通过。物料、订单、收退货、对账业务和文件解析/OCR/工作流继续按规划推进。业务接入方式见[后端业务开发指南](../docs/backend-business-guide.md)。

`PROCURE_MAINTENANCE_DIR`缺省为附件根的同级maintenance目录；正式API和维护工具共同挂载独立procure_files卷并使用UID10001。普通写入持有共享flock，持久维护标记开启后拒绝新写入，发布控制器独占排空后备份。`/health`保持存活，`/api/v1/system`在维护中返回ready=false及maintenance原因。坏标记或目录故障保持停写；后台任务以后必须接入同一门禁。

## 业务命令记录（D05子步骤）

`CommandRecordDO`映射到`command_records`表，由`0002_commands`迁移创建：

| 字段 | 用途 |
| --- | --- |
| command_id | 调用方提供的UUID主键，重试时保持相同，不由数据库自动生成 |
| operation | 最长128字符、非空白的操作名称，保留原值 |
| request_fingerprint | 64位小写十六进制请求指纹；不同命令允许指纹相同 |
| response_status | 已成功执行的响应状态码，范围200至299，无默认值 |
| response_body | JSONB响应内容；Python None存为SQL NULL，JSON.NULL可表示JSON null |
| created_at | PostgreSQL生成的带时区创建时间 |

`CommandExecutor`已接入同ID重放、载荷冲突、并发串行与有限事务重试；业务变更、成功记录及审计在同一事务提交。接口继承`CommandDTO`或`VersionedCommandDTO`，注入`ActorDep`和`CommandExecutorDep`，使用`command_response()`返回结果。业务回调不自行提交事务或执行外部副作用。

`0003_infrastructure`新增`audit_events`和`stored_files`。审计对象外键延迟至事务提交检查；原件按摘要唯一存储，使用UUID文件标识关联业务。`POST /api/v1/files`使用原始字节请求体、`filename`查询参数及`X-Command-ID`请求头；元数据和下载接口均要求Bearer认证。上传不代表已完成内容解析。

显式回收无元数据引用且超过宽限时间的物理对象：`uv run --locked python -m app.cli cleanup-files --min-age-hours 24`。受内容锁保护，不自动清理已有元数据的原件，也不在服务启动时回收。

新增迁移后，开发库仍需在`server/`显式执行`uv run --locked alembic upgrade head`。本轮开发库已显式升级到0005_units。正式库通过受控发布流程迁移，实际版本由发布记录提供。就绪检查会同时核对迁移版本和命令表，旧结构或缺表保持未就绪。

uv约定依据：[项目结构](https://docs.astral.sh/uv/concepts/projects/layout/)、[锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[Docker集成](https://docs.astral.sh/uv/guides/integration/docker/)。


### 供应商后端

用户手写的供应商新增、修改、详情和列表业务整理在`app/service/supplier_service.py`的`SupplierService`中，通过`app/controller/supplier_controller.py`接入，数据库访问由`app/repository/supplier_repository.py`负责。`0004_suppliers`建立suppliers表，与`app/entity/supplier_do.py`中的SupplierDO对应。POST/PUT写入复用公共命令执行器、认证、来源锁、版本检查和审计，GET详情与分页使用已认证数据库会话。请求、查询、响应分别使用`app/domain/dto/`、`qo/`、`vo/`中的独立类型。联调路径与输入规则见[后端业务开发指南](../docs/backend-business-guide.md)。


### 计量单位后端

单位业务入口为`app/service/unit_service.py`的`UnitService`，数据库访问由`app/repository/unit_repository.py`的`UnitRepository`负责。`0005_units`建立与`app/entity/unit_do.py`一致的单位表。`app/controller/unit_controller.py`接入新增、修改、详情及分页，写入使用公共执行器及`unit`来源锁。UnitDTO、CreateUnitDTO和UpdateUnitDTO沿用用户手写内容，UnitListQO、UnitVO及UnitListVO位于对应domain目录。

本轮226项真实PG集成和585项完整后端回归已通过，发布结果见执行日志及Jenkins记录。模型注册、迁移和接口完成属于D09b后端节点，客户端管理与选择组件继续在D09e验收。单位名称最多50字符沿用此前代码的技术默认值。
