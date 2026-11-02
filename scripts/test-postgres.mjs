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
const result = spawnSync('uv', [
  'run', '--directory', 'server', '--locked', 'pytest', '-q', '--tb=short', ...process.argv.slice(2),
], { cwd: root, env, encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
// 异常退出可能将密码截断，无法可靠按完整字符串脱敏，因此不输出残缺内容。
if (result.error || result.signal) {
  console.error(`测试进程未正常完成（${result.error?.code ?? result.signal}），已隐藏不完整输出。`);
  process.exit(1);
}
// 测试失败时第三方驱动的堆栈也可能包含连接参数，输出前统一隐藏测试密码。
const secrets = [...new Set([value, encodeURIComponent(value)])].sort((a, b) => b.length - a.length);
for (const [stream, output] of [[process.stdout, result.stdout], [process.stderr, result.stderr]]) {
  let safe = output ?? '';
  for (const secret of secrets) safe = safe.replaceAll(secret, '[REDACTED]');
  stream.write(safe);
}
process.exit(result.status ?? 1);
