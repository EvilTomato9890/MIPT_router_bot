import { Store } from '../src/db.mjs';
import { resolve, join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { existsSync } from 'node:fs';
const source = process.env.DB_PATH || './data/router-bot.sqlite';
const dest =
  process.argv[2] ||
  join(
    process.env.BACKUP_DIR || './backups',
    'manual-' + new Date().toISOString().replace(/[:.]/g, '-') + '.sqlite',
  );
if (resolve(source) === resolve(dest)) throw new Error('Резервная копия должна иметь другой путь');
if (!existsSync(source)) throw new Error('Исходная БД не найдена; пустая копия не создаётся');
const store = new Store(source);
try {
  await store.backup(dest);
} finally {
  store.close();
}
const test = new DatabaseSync(dest, { readOnly: true });
try {
  const r = test.prepare('PRAGMA integrity_check').get();
  if (Object.values(r)[0] !== 'ok') throw new Error('Копия не прошла проверку');
} finally {
  test.close();
}
console.info('Резервная копия создана и проверена:', resolve(dest));
