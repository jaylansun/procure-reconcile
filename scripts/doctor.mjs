import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
const env = { ...process.env };
if (existsSync(path.join(root, '.local/cargo/bin/cargo'))) {
  env.CARGO_HOME = path.join(root, '.local/cargo');
  env.RUSTUP_HOME = path.join(root, '.local/rustup');
  env.PATH = `${env.CARGO_HOME}/bin${path.delimiter}${env.PATH}`;
}
console.log(`平台 ${process.platform}/${process.arch} | Node ${process.version}`);
const projectPython = path.join(root, 'server', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
let failed = false;
for (const [label, cmd, args] of [
  ['项目Python（uv .venv）', projectPython, ['--version']],
  ['uv项目/锁文件', 'uv', ['lock', '--directory', 'server', '--check', '--offline']],
  ['uv', 'uv', ['--version']],
  ['pnpm', 'pnpm', ['--version']], ['Rust', 'rustc', ['--version']],
  ['Cargo', 'cargo', ['--version']], ['Docker引擎', 'docker', ['version', '--format', '{{.Server.Version}}']],
]) {
  const r = spawnSync(cmd, args, { cwd: root, env, encoding: 'utf8', timeout: 8000 });
  const ok = !r.error && r.status === 0;
  console.log(`${ok ? '可用' : '缺失/不可用'} ${label}: ${ok ? (r.stdout.trim() || '项目配置与锁文件一致') : (r.error?.message ?? r.stderr.trim()).split('\n')[0]}`);
  if (!ok) failed = true;
}
console.log('只检查环境，不起停服务、不修改配置。项目Python缺失时请执行 uv sync --directory server --locked。');
process.exitCode = failed ? 1 : 0;
