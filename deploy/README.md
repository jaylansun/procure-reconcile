# 容器基础配置

本项目使用独立 Compose 项目和资源。不得借用 DevPilot 的库、卷、容器或凭据。开发API由PyCharm启动，仅开发PG进入容器。

- 开发：`.local/dev-db.env` + `compose.dev.yml`，项目 `procure-dev`，PG仅 `127.0.0.1:15432`，普通应用角色 `procure_app`。
- 测试：独立 `compose.test.yml`，项目 `procure-test`，PG仅 `127.0.0.1:17432`，tmpfs数据；必须单独密码，不连接开发或正式实例。
- 部署：私有 `.local/deploy.env` + `compose.yml`，项目 `procure`，正式PG不发布端口，API回环18080仅供本机验证。
- LAN：`compose.lan.yml` 是可选TLS叠加配置，先配置服务Mac的LAN网卡IP、发布端口，以及 `.local/tls/server.crt` / `server.key`。证书必须包含访问地址的SAN，各客户端需要信任签发根。不关闭证书验证，也不把回环发布当LAN已完成。

PG17和基础镜像尚需在实际构建后记录精确版本/digest。初始化脚本仅作用于空数据目录；调整env不自动变更旧库密码。业务账号不是PG超级用户，本阶段迁移与运行仍用同一应用角色，进一步分权在D05完善。

容器启动只检查存活，不在entrypoint执行迁移或创建默认管理员。迁移、初始化账号都必须作为受控显式命令执行。应用账号密码由交互设置；PG密码和模型密钥不能放镜像或仓库。

当前没有自动部署到正式服务。Jenkinsfile需在C07/C08的目标锁、一致备份、单次迁移及失败恢复协议完成后接入，不能用一份未经验证的流水线宣称一键发布已完成。

## 当前开发机的临时镜像来源

2026-10-16，Docker Hub registry连接超时。开发/测试私有env暂设 `PROCURE_POSTGRES_IMAGE=pgvector/pgvector:pg17`，复用本机已缓存的**镜像层**，分别创建全新的 `procure-dev` 和 `procure-test` 实例及密码，未复用DevPilot数据/卷。实测服务版本PG17.11，扩展仅plpgsql，未创建vector扩展。

正式 `compose.yml` 仍指定官方PG17。网络恢复后，替换开发镜像前须保存当前资料并按维护规则操作；不能因改镜像就清理数据卷。该临时测试配置不表示本项目引入向量功能。
