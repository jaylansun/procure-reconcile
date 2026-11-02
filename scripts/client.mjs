import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
const env = { ...process.env, CARGO_BUILD_JOBS: process.env.CARGO_BUILD_JOBS ?? '2' };
if (existsSync(path.join(root, '.local/cargo/bin/cargo'))) {
  env.CARGO_HOME = path.join(root, '.local/cargo');
  env.RUSTUP_HOME = path.join(root, '.local/rustup');
  env.PATH = `${env.CARGO_HOME}/bin${path.delimiter}${env.PATH}`;
}
const mode = process.argv[2];
const common = ['--filter', '@procure-reconcile/client', 'tauri'];
const commands = {
  dev: ['pnpm', [...common, 'dev', '--config', 'src-tauri/tauri.dev.conf.json']],
  check: ['cargo', ['check', '--manifest-path', 'client/src-tauri/Cargo.toml', '--locked']],
  'package-intel': ['pnpm', [...common, 'build', '--target', 'x86_64-apple-darwin']],
  'package-arm64': ['pnpm', [...common, 'build', '--target', 'aarch64-apple-darwin']],
  'package-windows': ['pnpm', [...common, 'build', '--config', 'src-tauri/tauri.windows.conf.json']],
};
if (!commands[mode]) throw new Error(`Unknown client action: ${mode}`);
if (mode === 'package-windows' && process.platform !== 'win32') throw new Error('请在 Windows 构建环境运行 Windows 打包。');
const [command, args] = commands[mode];
const result = spawnSync(command, args, { cwd: root, env, stdio: 'inherit', shell: process.platform === 'win32' });
if (result.error) { console.error(result.error.message); process.exit(1); }
process.exit(result.status ?? 1);
