import { randomBytes } from 'node:crypto';
import { mkdirSync, existsSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const local = fileURLToPath(new URL('../.local/', import.meta.url));
mkdirSync(local, { recursive: true, mode: 0o700 });
const file = `${local}/test-db.env`;
if (existsSync(file)) { console.error('已有测试配置，未覆盖。'); process.exit(1); }
writeFileSync(file, `PROCURE_PG_ADMIN_PASSWORD=${randomBytes(24).toString('hex')}\nPROCURE_PG_APP_PASSWORD=${randomBytes(24).toString('hex')}\n`, { mode: 0o600, flag: 'wx' });
console.log('已生成独立测试PG私有配置，未显示密码。');
