import {
  mkdirSync,
  openSync,
  writeFileSync,
  closeSync,
  readFileSync,
  unlinkSync,
  readdirSync,
  statSync,
} from 'node:fs';
import { resolve, join, dirname } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { Store } from './db.mjs';
import { Inventory } from './service.mjs';
import { Campaigns } from './campaigns.mjs';
import { Telegram } from './telegram.mjs';
import { Bot } from './bot.mjs';
import { config } from './config.mjs';
import { Synchronizer } from './sheets/sync.mjs';
import { ColorRepair, colorPlan } from './sheets/colors.mjs';
import { GoogleSheets } from './sheets/google.mjs';
import { MemorySheets } from './sheets/memory.mjs';
import { DomainError, localClock } from './util.mjs';

export function lockProcess(path) {
  const file = resolve(path) + '.lock';
  mkdirSync(dirname(file), { recursive: true });
  try {
    const pid = Number(readFileSync(file, 'utf8'));
    if (!Number.isSafeInteger(pid) || pid <= 0)
      throw new DomainError('Проверьте устаревший файл блокировки вручную');
    try {
      process.kill(pid, 0);
      throw new DomainError('Другой процесс бота уже работает');
    } catch (e) {
      if (e.code !== 'ESRCH') throw e;
      unlinkSync(file);
    }
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  const fd = openSync(file, 'wx');
  writeFileSync(fd, String(process.pid));
  closeSync(fd);
  return () => {
    try {
      if (readFileSync(file, 'utf8') === String(process.pid)) unlinkSync(file);
    } catch {
      /* no personal data in logs */
    }
  };
}

async function main() {
  const c = config(),
    unlock = lockProcess(c.db),
    s = new Store(c.db),
    i = new Inventory(s, { bootstrapAdminId: c.admin }),
    t = new Telegram(c.token);
  const adapter =
    c.mode === 'google' ? new GoogleSheets(c) : c.mode === 'memory' ? new MemorySheets() : null;
  const sync = adapter ? new Synchronizer(i, adapter) : null,
    colors = adapter ? new ColorRepair(i, adapter) : null;
  const campaigns = new Campaigns(i, { time: c.reminderTime, offsets: c.reminderOffsets });
  const bot = new Bot(i, t, {
    sync,
    colors,
    campaigns,
    mode: c.mode,
    provision: c.mode === 'google' ? () => adapter.provision() : null,
  });
  let stopping = false,
    lastMaintenance = 0;
  const stop = () => {
    stopping = true;
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  try {
    const webhook = await t.call('getWebhookInfo');
    if (webhook.url)
      throw new DomainError(
        'У бота настроен webhook. Сначала отключите его по инструкции без удаления ожидающих обновлений.',
      );
    await t.call('setMyCommands', {
      commands: [
        { command: 'menu', description: 'Главное меню' },
        { command: 'find', description: 'Поиск роутера' },
        { command: 'reports', description: 'Отчёты' },
        { command: 'collection', description: 'Сбор' },
        { command: 'help', description: 'Помощь' },
      ],
    });
    console.info('Бот запущен. Режим таблицы:', c.mode);
    while (!stopping) {
      if (Date.now() - lastMaintenance >= 30000) {
        lastMaintenance = Date.now();
        if (sync) await sync.flush();
        if (adapter && s.setting('colors_managed'))
          try {
            const plan = colorPlan(await adapter.read());
            if (plan.changed) await adapter.batch(plan.requests);
          } catch {
            /* durable outbox retains data */
          }
        await campaigns.tick((id, message) => {
          i.authorize(id, 'operator');
          return t.send(id, message);
        });
        const { day } = localClock(new Date());
        const expired = s.all('SELECT user_id,data FROM sessions');
        for (const row of expired)
          if (Date.parse(JSON.parse(row.data).updatedAt) + 1800000 < Date.now())
            s.run('DELETE FROM sessions WHERE user_id=?', row.user_id);
        s.run('DELETE FROM drafts WHERE expires_at<?', new Date().toISOString());
        const admins = s.all("SELECT user_id FROM roles WHERE role='admin'").map((r) => r.user_id);
        if (c.retentionDays && s.setting('privacy_day') !== day && admins.length) {
          i.purgeContacts(admins[0], c.retentionDays, 'privacy:' + day);
          s.set('privacy_day', day);
        }
        if (s.setting('backup_day') !== day) {
          try {
            mkdirSync(c.backupDir, { recursive: true });
            await s.backup(join(c.backupDir, `router-bot-${day}.sqlite`));
            s.set('backup_day', day);
            const files = readdirSync(c.backupDir)
              .filter((n) => /^router-bot-\d{4}-\d{2}-\d{2}\.sqlite$/.test(n))
              .sort()
              .reverse();
            for (const f of files.slice(c.backupKeep)) unlinkSync(join(c.backupDir, f));
          } catch {
            console.warn('Резервная копия не создана. Проверьте место и права каталога.');
          }
        }
        const old = s.get(
          'SELECT count(*) AS n FROM outbox WHERE first_at<?',
          new Date(Date.now() - c.alertHours * 3600000).toISOString(),
        ).n;
        if (old && s.setting('sync_alert_day') !== day) {
          let delivered = false;
          for (const id of admins)
            try {
              i.authorize(id, 'admin');
              await t.send(
                id,
                `Обновление таблицы ожидает ${old} записей более ${c.alertHours} ч. Проверьте /reports → «Ожидают таблицу» и «Конфликты».`,
              );
              delivered = true;
            } catch {
              /* retry next cycle */
            }
          if (delivered) s.set('sync_alert_day', day);
        }
      }
      try {
        const offset = s.setting('telegram_offset', 0),
          updates = await t.call(
            'getUpdates',
            { offset, timeout: 25, allowed_updates: ['message', 'callback_query'] },
            35000,
          );
        for (const u of updates) {
          if (stopping) break;
          await bot.handle(u);
          s.set('telegram_offset', u.update_id + 1);
          s.run('DELETE FROM processed_updates WHERE id<?', u.update_id - 10000);
        }
      } catch {
        console.warn('Telegram временно недоступен. Повтор через 5 секунд.');
        await delay(5000);
      }
    }
  } finally {
    s.close();
    unlock();
  }
}

main().catch((e) => {
  console.error(
    e instanceof DomainError
      ? e.message
      : 'Не удалось запустить бота. Проверьте конфигурацию, каталог данных и доступ к сервисам.',
  );
  process.exitCode = 1;
});
