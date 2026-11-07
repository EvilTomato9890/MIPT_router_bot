import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
function walk(path) {
  return readdirSync(path, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory()
      ? walk(join(path, e.name))
      : e.name.endsWith('.mjs')
        ? [join(path, e.name)]
        : [],
  );
}
let failures = 0;
for (const path of [...walk('src'), ...walk('scripts'), ...walk('test')]) {
  const result = spawnSync(process.execPath, ['--check', path], { encoding: 'utf8' });
  if (result.status !== 0) {
    failures++;
    process.stderr.write(result.stderr);
  }
}
if (failures) process.exitCode = 1;
else console.info('Синтаксис всех модулей проверен.');
