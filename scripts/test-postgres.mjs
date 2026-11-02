import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
let text;
try { text = readFileSync(new URL('../.local/test-db.env', import.meta.url), 'utf8'); }
catch { console.error('请先准备 .local/test-db.env 和独立 procure-test PG。'); process.exit(1); }
const value = text.split(/\r?\n/).find(line => line.startsWith('PROCURE_PG_APP_PASSWORD='))?.split('=').slice(1).join('=');
if (!value) throw new Error('测试应用密码缺失');
const env = { ...process.env, TEST_DATABASE_URL: `postgresql+psycopg://procure_app:${encodeURIComponent(value)}@127.0.0.1:17432/procure_test` };
const result = spawnSync('uv', ['run', '--directory', 'server', '--locked', 'pytest', '-q'], { cwd: root, env, stdio: 'inherit' });
if (result.error) { console.error(result.error.message); process.exit(1); }
process.exit(result.status ?? 1);
