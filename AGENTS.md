# procure-reconcile

已确认需求见 docs/implementation-plan.md，当前实施状态以 docs/execution-log.md 和真实代码/测试为准。

- 顶层目录固定 client/（React/TypeScript/Tauri 2）与 server/（FastAPI/Python），数据库固定 PostgreSQL。不要改回其他目录名或数据库。
- Python后端以server/作为独立uv项目：pyproject.toml声明依赖，uv.lock锁版本，.python-version选择Python3.13，server/.venv由uv维护。新增/移除依赖用uv add/remove，校验/安装及运行用uv lock --check、uv sync --locked、uv run --locked，不手工修改锁文件或另建requirements.txt依赖来源；Docker安装不含dev组。
- Python 由 PyCharm 独立启动；客户端命令不得启动、关闭或重启 Python/Docker。
- 正式服务位于这台 Mac 的 OrbStack；局域网客户端使用服务 Mac 的 HTTPS 地址。开发 API、开发 PG 保持回环，正式 PG 仅内部网络。
- 开发/测试/部署使用独立数据库、附件及凭据。不得修改或复用 DevPilot 的数据库、卷、容器或凭据。
- 数据库期望迁移版本通过app/persistence/schema.py从Alembic迁移脚本自动读取head，与数据库完整版本集合校验；不得重新写死SCHEMA_REVISION。就绪检查和管理员初始化都不自动执行迁移。
- 管理员密码允许6至128个字符，可用6位纯数字（含前导零），不强制字母/符号。
- 不提交密码、会话、模型密钥、用户原件或本机配置。会话只由原生凭据设施保存。
- 数量/单价最多6位、百分数税率4位、金额2位；Decimal/NUMERIC，ROUND_HALF_UP，按行舍入后汇总。
- 业务事实、确认快照与后续调整分别保存。已确认需求不重复提问，未知设备版本不虚报兼容。
- 修改后执行对应检查，更新执行日志。文档任务不代表已完成；未实际通过的安装包/部署/模型能力必须保留待验收。
- 当前为用户手写练习模式：按要求准备空白文件，完整实现和测试代码仅在对话中提供，配中文文档字符串和必要的中文注释。由用户手写入文件；后续检查以用户实际写入的代码为准，只有用户明确要求代写或修改时才写入实现。
- 保留用户根 main.py 示例和现有 Git 历史；不覆盖 .idea。未经用户要求不推送、发布或操作 Jenkins。
