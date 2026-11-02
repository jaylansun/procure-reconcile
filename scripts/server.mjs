import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const cwd = fileURLToPath(new URL('../server/', import.meta.url));
const actions = {
  check: ['ruff', 'check', 'app', 'tests'],
  domain: ['pytest', 'tests/test_domain.py', '-q'],
  api: ['pytest', 'tests/test_health.py', 'tests/test_security.py', 'tests/integration', '-q'],
  migrate: ['alembic', 'upgrade', 'head'],
  admin: ['python', '-m', 'app.cli', 'init-admin'],
};
const args = actions[process.argv[2]];
if (!args) throw new Error('Unknown server action');
const result = spawnSync('uv', ['run', '--locked', ...args], { cwd, stdio: 'inherit' });
if (result.error) { console.error(result.error.message); process.exit(1); }
process.exit(result.status ?? 1);
