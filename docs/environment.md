# 开发环境实测（2026-10-16）

- 根目录：`/Users/sunjiale/Documents/procure-reconcile`；原WebRemix目录不存在。
- Git：origin为用户提供的jaylansun/procure-reconcile；main跟踪origin/main，初始a11e683。用户原有main.py和.idea保留；README在初始版本基础上补充。
- OS：macOS 15.8.1（24H32），Intel x86_64，内存8 GiB。其他平台系统与架构仍待核实。
- Node：24.21.0；pnpm：11.25.0；uv：0.12.10；项目Python：3.13.2，uv维护的独立server/.venv；当前基解释器为已有Python3.13.2。uv版本范围>=0.12.10,<0.13由pyproject约束，doctor检查项目解释器及锁一致性。
- Rust/Cargo：1.99.1，本项目.local内隔离安装，无Shell配置改动。根脚本自动发现，本机首次依赖编译较慢；后续构建默认限制2个作业。
- Docker context：orbstack。用户明确授权重启后恢复，DevPilot原4容器重新运行且健康；未修改其配置、卷或凭据。
- 新开发PG：procure-dev-db-1，仅回环15432，独立procure-dev_postgres卷。
- 新测试PG：procure-test，仅回环17432，tmpfs，完成测试后停止；与开发/正式分开。
- 当前开发/测试PG实测17.11，普通应用角色procure_app，非超级用户；扩展只有plpgsql。因registry连接超时，暂用本机缓存的pgvector/pgvector:pg17镜像层，未启用vector；正式Compose仍按官方PG17。
- 开发私有配置server/.env、.local/dev-db.env与测试配置权限600，已核查被Git忽略。未读取或复用DevPilot秘密。
- 原生桌面窗口、正式服务镜像、LAN TLS和其他设备安装尚待对应任务实测，版本不能由本机通过外推。

- 普通终端pnpm入口已补齐：Corepack0.36.0管理pnpm11.25.0，shim位于用户nvm Node24.21.0/bin；精简PATH的交互zsh及激活uv环境后均验证可用，客户端命令不再依赖Codex自带pnpm。
