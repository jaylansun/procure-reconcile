# 后端业务开发接入指南

更新于2026-11-17。当前分工为用户手写采购业务，助手负责公共基础设施、迁移衔接和全部测试。2026-11-16已授权将现有后端整理为分层目录及Service类。后续业务手写继续沿用这一分工，已确认的业务规则以 implementation-plan.md 为准。

## 类名与用途

| 后缀 | 用途 | 项目示例 |
| --- | --- | --- |
| QO | 查询、筛选、分页和排序条件 | SupplierListQO |
| DTO | 新增/修改请求以及内部数据传输 | CreateSupplierDTO、UpdateSupplierDTO、CommandResultDTO |
| VO | 接口返回的展示数据 | LoginVO、SystemVO、FileMetadataVO |
| DO | SQLAlchemy数据库映射 | SupplierDO、AdministratorDO、StoredFileDO |

SupplierDTO承载新增和修改复用的供应商资料。CommandDTO提供命令编号，VersionedCommandDTO增加预期版本。服务、执行器、存储、配置、上下文和异常使用对应的职责后缀，例如FileService、CommandExecutor、FileStore、Settings、CommandContext、AppError。每个新增类配中文文档字符串，说明职责和所在层。供应商列表通过SupplierListQO接收分页参数，通过SupplierListVO返回当前页。

## 目录与阅读顺序

| 层 | 供应商模块示例 | 职责 |
| --- | --- | --- |
| Controller | `app/controller/supplier_controller.py` | HTTP参数、认证、服务注入与响应VO |
| Service | `app/service/supplier_service.py`中的`SupplierService` | 业务校验、版本检查、资料修改与审计 |
| Repository | `app/repository/supplier_repository.py`中的`SupplierRepository` | 复用调用方Session执行查询、保存和刷新 |
| Entity | `app/entity/supplier_do.py`中的`SupplierDO` | SQLAlchemy表映射与数据库约束 |
| DTO | `app/domain/dto/create_supplier_dto.py`等 | 写入命令和内部传输数据 |
| QO | `app/domain/qo/supplier_list_qo.py` | 查询条件 |
| VO | `app/domain/vo/supplier_vo.py`等 | 接口展示数据 |

先从Controller进入，跟随调用阅读Service和Repository，再核对DO、DTO、QO和VO。每个DO、DTO、QO、VO独立建文件，文件名使用对应的`_do.py`、`_dto.py`、`_qo.py`、`_vo.py`后缀。每个业务Service类独立放在对应的`*_service.py`中。

`app/config/`承载配置，`app/common/`承载公共组件，`app/persistence/`承载数据库连接、迁移检查和模型注册。新增DO加入`app/persistence/model_registry.py`，并复用`app/entity/base_do.py`中的`BaseDO`，使Alembic可以读取完整表集合。金额与数量规则分别调用`app/service/money_service.py`的`MoneyService`和`app/service/quantity_service.py`的`QuantityService`。

## 已提供的公共能力

| 入口 | 负责的事情 |
| --- | --- |
| `app/common/dependencies.py` 的 `ActorDep` | 校验Bearer会话，返回管理员身份快照，随即关闭鉴权数据库会话 |
| `app/domain/dto/command_dto.py`及`versioned_command_dto.py` | 通用命令输入及预期版本字段 |
| `app/common/commands/http.py` | 执行器注入、成功响应转换 |
| `app/common/commands/command_executor.py` | 独立事务、同命令重放/冲突、有限重试、统一提交和自动审计 |
| `app/common/commands/locks.py` | PostgreSQL事务级命令锁和稳定来源锁 |
| `app/common/audit/audit.py` | 显式对象审计追加 |
| `app/domain/dto/audit_change_dto.py` | 审计目标、版本与原因、JSON快照及已知敏感键遮盖 |
| `app/common/storage/file_store.py` | 分块暂存、摘要验证、原子发布、不可变对象和安全路径 |
| `app/service/file_service.py` | 文件标识、内容去重、数据库引用、失败补偿和显式孤儿回收 |
| `app/controller/file_controller.py` | 已鉴权的上传、元数据与下载接口 |

业务Service将`context.session`传给Repository读写业务表，返回`CommandResultDTO`。Repository保存操作执行`add/flush`，业务变更、命令记录及审计由公共执行器统一提交。

## 先准备数据库

先在仓库根执行迁移命令。

```sh
pnpm db:migrate
```

也可以在`server/`执行。

```sh
uv run --locked alembic upgrade head
```

当前迁移头为`0005_units`，依次增加认证、命令、审计、原件元数据、供应商及单位资料表。显式迁移后，服务通过`ReadinessService.check()`核对数据库版本及就绪状态。

## 一次业务写入怎样接入

1. 输入模型继承 `CommandDTO`；修改已有记录时继承 `VersionedCommandDTO`，包含 `expected_version`。
2. 路由声明`actor: ActorDep`、`executor: CommandExecutorDep`和对应的Service依赖。
3. 调用 `executor.execute()`，把完整业务输入交给 `payload=data.command_payload()`。
4. 在同步Service方法中经Repository读取、检查和修改业务记录，记录对象审计，返回成功结果。
5. 路由用 `command_response(result)` 返回结果。

下面使用已编写的`SupplierService`和`SupplierRepository`演示路由接入方式，实际接口位于`app/controller/supplier_controller.py`。

```python
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.common.commands.http import CommandExecutorDep, command_response
from app.common.dependencies import ActorDep
from app.common.errors import AppError
from app.domain.dto.source_key_dto import SourceKeyDTO
from app.domain.dto.update_supplier_dto import UpdateSupplierDTO
from app.domain.vo.supplier_vo import SupplierVO
from app.repository.supplier_repository import SupplierRepository
from app.service.supplier_service import SupplierService

router = APIRouter(prefix="/api/v1/suppliers", tags=["供应商"])


def get_supplier_service() -> SupplierService:
    """为当前请求提供供应商服务及其数据库访问组件。"""
    return SupplierService(SupplierRepository())


SupplierServiceDep = Annotated[SupplierService, Depends(get_supplier_service)]


@router.put("/{supplier_id}", response_model=SupplierVO)
def update_supplier_endpoint(
    supplier_id: UUID,
    command: UpdateSupplierDTO,
    actor: ActorDep,
    executor: CommandExecutorDep,
    service: SupplierServiceDep,
) -> Response:
    """把已校验的业务输入交给统一命令执行器。"""
    if supplier_id != command.supplier_id:
        raise AppError(422, "supplier_id_mismatch", "路径与请求中的供应商ID必须一致")

    result = executor.execute(
        command_id=command.command_id,
        operation="suppliers.update",
        payload=command.command_payload(),
        actor_id=actor.id,
        sources=[SourceKeyDTO("supplier", str(supplier_id))],
        # 执行器将当前事务交给Service，Repository复用context.session。
        handler=lambda context: service.update_supplier(context, command),
    )
    return command_response(result)
```

Service通过构造函数接收Repository。现有`SupplierService.update_supplier()`调用`self.repository.get_for_update(context.session, command.supplier_id)`读取并锁定记录，调用`self.repository.save(context.session, supplier)`保存资料和刷新字段。事务由公共执行器管理，Repository使用同一个Session。

在`SupplierService.update_supplier()`内，你负责记录缺失、允许修改的字段和生效条件等业务规则。下面是方法内部的版本与审计调用片段。

```python
from app.domain.dto.audit_change_dto import AuditChangeDTO
from app.domain.dto.command_result_dto import CommandResultDTO

# 此时执行器已经持有supplier来源锁，row由Repository查询取得。
context.expect_version(row.version, data.expected_version)
previous_version = row.version

# 在这里写业务字段校验和修改。
row.version += 1

context.audit(
    AuditChangeDTO(
        target_type="supplier",
        target_id=str(row.id),
        before_version=previous_version,
        after_version=row.version,
        details={"changed_fields": ["name"]},
    )
)
result = CommandResultDTO(200, {"id": str(row.id), "version": row.version})
```

完整的保存、审计及返回过程可直接阅读`app/service/supplier_service.py`。版本字段及递增属于业务模型，公共层负责版本比较。

## 命令、锁和事务约定

- 客户端为每一次新操作生成UUID `command_id`。网络中断后重试原操作沿用原ID；业务输入改变或版本冲突后重新提交，生成新ID。
- 同ID、相同操作和完整输入：返回第一次成功的状态与JSON，`X-Command-Replayed: true`，不再执行业务和追加审计。
- 同ID改变操作、输入或执行原因：返回409 `command_conflict`。指纹内容是操作名称和内部封装的 `payload`、`reason`；`command_id` 独立保存。调用方无需自己再次计算指纹。
- 数量、金额、单价、税率在命令JSON中使用规范十进制字符串；不要传 `float`。UUID、日期经输入模型转成JSON字符串。嵌套字段仍全部参与指纹，只有根级 `command_id` 被输入基类移除。
- 修改已有记录时，先声明 `sources`，再由回调查询、检查版本并修改。同一业务来源在所有入口使用同一命名空间和稳定ID。一次操作涉及多条来源时一次性声明完整集合，公共层排序去重后加锁。
- 新建操作按真正需要保护的唯一业务键选择来源，数据库同时保留唯一/外键/检查约束。不同ID即使输入相同也属于不同命令，不能用指纹代替业务唯一约束。
- 版本不符返回409 `version_conflict`；锁等待超时返回409 `resource_busy`，失败不会保存成功命令。锁超时缺省3000毫秒，可按环境配置。
- 执行器仅对PG的序列化失败和死锁（40001/40P01）重试整个事务，最多3次。因此回调可能重新运行。回调不能发送请求、写文件、发消息或操作别的连接，也不能执行 `COMMIT` 等原始事务SQL。
- 回调不能调用 `session.commit/rollback/close/begin` 或自行取得外部连接。执行器对常见提前提交/回滚误用有保护；这不是运行不可信任代码的沙箱。正常使用 `add/get/scalar/execute/flush`。
- 响应只接受2xx与精确JSON值。204/205必须无响应体；200的 `None` 表示JSON null。错误抛 `AppError(status, code, message)`，不要把异常包装成成功返回。

自动命令审计与显式对象审计、业务变更、成功记录在同一PG事务提交。审计只传必要业务字段；公共层不会自动抓取请求体，已知密码/token键遮盖也不能替代调用方主动排除秘密。审计模块只提供追加接口；数据库级不可篡改留痕不在当前实现承诺内。

## 原件接口

所有接口均需 `Authorization: Bearer <会话令牌>`。

| 请求 | 参数和结果 |
| --- | --- |
| `POST /api/v1/files?filename=采购单.pdf` | `X-Command-ID: <UUID>`；请求体直接是文件字节，使用 `application/octet-stream`，不使用multipart |
| `GET /api/v1/files/{file_id}` | 返回标识、SHA-256、字节数、首次导入文件名、媒体类型、创建时间 |
| `GET /api/v1/files/{file_id}/content` | 流式下载已校验对象，中文文件名使用RFC5987响应头 |

首次内容导入返回201。不同命令导入相同内容返回200并复用同一 `file_id`，保留首次名称；同命令重试返回原状态和原结果。相同内容的其他原始显示名称，后续可由业务附件关联记录自行保存，不能覆盖原件元数据。

文件名仅用于显示，不能包含路径。支持 `.xlsx/.xls/.pdf/.png/.jpg/.jpeg`，大小写不敏感；当前按后缀分配媒体类型，上传成功不代表内容已经被解析或确认格式有效。空文件拒绝。单文件默认25MiB是可调整的技术默认值，通过 `PROCURE_MAX_FILE_BYTES` 配置，尚不是已确认的业务额度。扫描、OCR、加密文件检测与候选提取继续在后续解析步骤实现。

接收网络流时不持有业务SQL事务。暂存后，服务在内容锁内原子发布，再保存数据库引用。PG和文件系统没有跨资源原子事务：失败时重新加同一内容锁查引用后补偿；补偿失败宁可留下可回收孤儿，后续受控维护回收，避免删除已被成功引用的内容。下载缺失或损坏对象返回503，不返回磁盘路径。

业务表通过外键引用 `stored_files.file_id`，不要保存客户端路径或物理对象路径。别在业务回调内调用上传服务，因为它有自己的命令事务；先上传取得文件标识，再在业务命令中检查并建立附件关联。业务确认失败时，原件仍可保留以便重试和追溯。

## 显式维护

在`server/`执行维护命令。

```sh
uv run --locked python -m app.cli cleanup-files --min-age-hours 24
```

仅检查超过宽限时间且没有任何 `stored_files` 元数据引用的对象。每个对象重新加内容锁并查引用，正在提交或已引用的对象不会被删；有元数据但业务未引用的原件也不会被自动删除。服务启动不自动清理。正常上传在finally丢弃暂存文件；进程硬退出遗留的 `.staging` 文件暂不自动回收，后续维护流程需结合停机/租约处理。

## 验证和继续开发

全部测试交给助手。仓库根 `pnpm test:api:pg` 使用独立测试PG并运行整套后端测试；未配置测试PG导致的跳过不算验证通过。不要在开发/正式库运行会清表的测试。验证证据见 execution-log.md。

供应商后端已有新增、修改、详情和列表接口，单位手写业务本轮接入相同公共机制。单位接口真实PG集成已通过，D09接着完成资料页面、物料规格及经确认的单位换算，完整回归及发布结果见执行日志。D09验收基础资料管理和独立选择组件，订单页面集成随D10验证。

新增业务沿用Controller、Service、Repository、Entity和Domain的分层入口。助手负责迁移、导入注册和测试衔接，业务实现由用户在对应Service类中手写。

## 供应商接口联调

开发库启动后，在仓库根目录执行`pnpm db:migrate`，将开发库显式升级到当前迁移头。随后由PyCharm启动后端，在`http://127.0.0.1:18081/docs`打开接口文档，登录取得Bearer会话后使用供应商接口。

| 方法 | 路径 | 输入与结果 |
| --- | --- | --- |
| POST | `/api/v1/suppliers` | CreateSupplierDTO包含command_id及data，返回201和SupplierVO |
| PUT | `/api/v1/suppliers/{supplier_id}` | UpdateSupplierDTO包含相同supplier_id、command_id、expected_version及完整data，返回200和SupplierVO |
| GET | `/api/v1/suppliers/{supplier_id}` | 返回SupplierVO，记录缺失返回404 |
| GET | `/api/v1/suppliers` | SupplierListQO包含offset和limit，返回SupplierListVO的items、offset、limit |

新增及修改请求的data包含name和可选contact_info。修改采用完整资料提交，省略或传null会清空联系信息。每次新操作生成新的command_id，重试原操作复用原ID与请求内容。修改路径和DTO中的supplier_id须一致。列表默认offset=0、limit=20，limit范围1至100，offset范围0至PostgreSQL的有符号64位整数上限。

SupplierListQO位于`app/domain/qo/supplier_list_qo.py`，SupplierVO和SupplierListVO位于`app/domain/vo/`，SupplierDO位于`app/entity/supplier_do.py`。CreateSupplierDTO和UpdateSupplierDTO位于`app/domain/dto/`，由Controller校验输入后交给用户手写的SupplierService业务方法。


## 单位接口联调

当前代码通过`app/controller/unit_controller.py`接入四个单位接口，迁移为`0005_units`。开发库显式迁移后，由PyCharm启动同一个后端入口并在Swagger登录。正式环境使用受控发布流程迁移，执行结果以发布记录为准。

| 方法 | 路径 | 输入与结果 |
| --- | --- | --- |
| POST | `/api/v1/units` | CreateUnitDTO包含command_id和data.name，返回201和UnitVO |
| PUT | `/api/v1/units/{unit_id}` | UpdateUnitDTO包含同一unit_id、command_id、expected_version和data.name，返回200和UnitVO |
| GET | `/api/v1/units/{unit_id}` | 返回UnitVO，记录缺失返回404 |
| GET | `/api/v1/units` | UnitListQO接收offset和limit，返回UnitListVO |

单位名称去除两端空格后为1至50字符，50字符沿用此前技术默认值。写入操作名为`units.create`与`units.update`，修改使用`SourceKeyDTO("unit", str(unit_id))`。成功响应和审计保存稳定ID、版本及ISO时间，重试复用原command_id和完整请求。分页offset范围为0至PG BIGINT最大值，limit为1至100。

本轮226项真实PG集成及585项完整后端回归已通过，其中包含单位接口、迁移及既有功能回归。开发库已显式升级0005_units，正式发布结果由执行日志及发布记录提供。手写业务学习继续沿用中文注释和一类一文件规范。
