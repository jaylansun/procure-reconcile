import { randomBytes, randomUUID } from 'node:crypto';
import { mkdirSync, existsSync, writeFileSync, chmodSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
const dbEnv = path.join(root, '.local/dev-db.env');
const apiEnv = path.join(root, 'server/.env');
if (existsSync(dbEnv) || existsSync(apiEnv)) {
  console.error('已有开发配置，未覆盖。请核对 .local/dev-db.env 与 server/.env；数据库初始化后的密码变更须受控执行。');
  process.exit(1);
}
mkdirSync(path.join(root, '.local/dev/files'), { recursive: true, mode: 0o700 });
const adminPassword = randomBytes(24).toString('hex');
const appPassword = randomBytes(24).toString('hex');
writeFileSync(dbEnv, `PROCURE_PG_ADMIN_PASSWORD=${adminPassword}\nPROCURE_PG_APP_PASSWORD=${appPassword}\n`, { mode: 0o600, flag: 'wx' });
writeFileSync(apiEnv, `PROCURE_ENVIRONMENT=development\nPROCURE_INSTANCE_ID=dev-${randomUUID()}\nPROCURE_DATABASE_URL=postgresql+psycopg://procure_app:${appPassword}@127.0.0.1:15432/procure_dev\nPROCURE_FILES_DIR=../.local/dev/files\nPROCURE_SESSION_HOURS=24\n`, { mode: 0o600, flag: 'wx' });
chmodSync(dbEnv, 0o600); chmodSync(apiEnv, 0o600);
console.log('已生成独立开发PG/API私有配置（未显示密码）。管理员登录密码请通过 admin:init 单独交互设置。');
