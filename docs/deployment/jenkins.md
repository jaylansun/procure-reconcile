# Jenkins构建与OrbStack发布

采购项目使用共享Jenkins中的独立任务`procure-reconcile`，源码固定为`https://github.com/jaylansun/procure-reconcile.git`的可信`main`，脚本入口为根`Jenkinsfile`。不配置PR发布或自动触发；日常点击“立即构建”。DevPilot任务及业务资源不修改。

## 操作入口

- Jenkins：`http://127.0.0.1:8080/job/procure-reconcile/`。
- 正式API：`https://192.168.1.88:18443`，对应当前Mac的局域网地址。
- 本机诊断API：`http://127.0.0.1:18080`。
- 管理员资料：本机忽略提交的`.local/production-admin.txt`，正式环境随机生成六位数字密码，不存在共享默认密码。
- 独立部署配置：本机`.local/deploy.env`及Jenkins Secret file `procure-deploy-env`。首次初始化和认证冒烟需要独立bootstrap字段，已有管理员不会被覆盖；修改密码后需同步该凭据供发布验证。

客户端使用HTTPS地址。在其他Mac/Windows设备上需要信任`.local/tls/ca.crt`所表示的本项目根证书；只分发这个公开证书，不分发`ca.key`或`server.key`。桌面网络层采用系统根证书，信任导入后重启桌面客户端再连接。本轮不自动修改操作系统信任设置。地址变化后需重新签发匹配SAN的服务证书并更新配置，不能关闭证书验证。

## 每次构建

1. 检出可信main，记录完整提交SHA。标签为`<40位SHA>-<Jenkins构建号>`。
2. 在容器里检查uv锁、Ruff、全部后端测试（真实隔离PG，不允许跳过），以及客户端类型、React构建和组件测试。该阶段不加载正式部署秘密。
3. 构建`procure-api`、`procure-ops`、`procure-db`、`procure-gateway`四个镜像，记录SHA标签、源码revision及镜像ID。ops仅用于维护，不给业务API挂Docker socket。
4. 注入本项目Secret file，启动一次性维护容器。跨任务目标flock和持久journal阻止重叠发布及无条件重试。
5. 持久启用维护，停止并排空API，PG保持运行。逻辑dump与附件tar/逐文件SHA256配套保存，恢复到独立临时PG验证后才迁移。
6. 待发布镜像显式执行一次Alembic。首次受控初始化管理员，已有账号必须匹配且不覆盖；候选验证schema、文件原子读写、维护中的HTTP状态及经CA/主机名校验的网关HTTPS。
7. 开放后验证真实登录、会话、工作台和退出。全部通过才记录成功；失败保持维护与诊断现场，不自动覆盖数据库。

首次初始化由Jenkins Secret file经stdin提供，只给短生命周期维护命令；不扩展普通CLI的明文参数，也不把bootstrap密码注入长期API。日常管理员CLI仍采用交互隐藏输入。

## 资源及秘密边界

| 资源 | 名称或位置 |
| --- | --- |
| 正式容器 | `procure-db-1`、`procure-api-1`、`procure-gateway-1` |
| 正式网络 | `procure_default` |
| 数据库卷 | `procure_postgres`，PG不发布宿主端口 |
| 原件与维护门禁卷 | `procure_files`，API/ops同UID10001 |
| 配套备份 | `procure_backups` |
| 发布锁和journal | `procure_release_state` |
| TLS证书 | `procure_tls`；Caddy只读挂载 |
| CI临时资源 | `procure-ci-<构建号>`，无宿主端口、tmpfs PG，无正式凭据 |
| 恢复验证资源 | `procure-restore-<构建号>`前缀，独立网络/卷/随机密码，结束只清理该组 |

Jenkins工作目录在容器中，因此不把该路径当作Mac宿主bind源。PG初始化脚本和网关配置进入专属镜像；秘密用Secret file、stdin及独立命名卷提供。归档只有脱敏JUnit、源码/镜像标识和发布JSONL，不归档私有env、TLS私钥、完整inspect或展开的Compose。

`procure_files`由维护容器首次挂载初始化，`procure_tls`由受控证书准备步骤预置，正式Compose将它们作为外部命名卷。日常不重建PG，不执行`down -v`或全局prune。

## 失败排查

Jenkins构建页查看阶段和`.local/release-reports/release-<构建号>.jsonl`归档。发布状态保存在独立卷`/state/current.json`与历史记录，`procure-ops:<标签> --resume-inspect`仅只读查看，不自动解除维护或恢复数据库。

未完成的发布再次运行会拒绝自动接续。操作人员须先核对失败阶段、镜像、实际schema、备份哈希和是否已有新写入，再选择向前修复；不能直接删除journal、取消维护标记或恢复旧备份来伪装成功。

部署维护控制器当前只覆盖已实现的API、命令、上传和维护CLI；未来worker、LangGraph及检查点写入必须接入同一门禁后才能发布。桌面安装包仍分别构建，此流水线中的React构建不等于macOS/Windows安装验收。其他LAN设备的证书信任与网络可达性需在真实设备验收。

实际构建号、通过范围和发布结果以执行日志及Jenkins记录为准。

首个验收基线：Jenkins构建#1 SUCCESS（281秒），源码aeb4994b0cfa488bd08ad761ede075add82a19b4，后端469项和前端2项通过；schema为0003_infrastructure。最新状态以Jenkins构建页及归档发布记录为准。
