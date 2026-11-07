import { assert } from './util.mjs';

export function config(env = process.env) {
  const c = {
    token: env.BOT_TOKEN || '',
    admin: Number(env.BOOTSTRAP_ADMIN_ID),
    db: env.DB_PATH || './data/router-bot.sqlite',
    mode: env.SHEETS_MODE || 'disabled',
    credentials: env.GOOGLE_APPLICATION_CREDENTIALS,
    spreadsheetId: env.SPREADSHEET_ID || '1N6lyJS7-PZ_hunmjnY06-0tnEEeOaP0Ypn-yEVfooK4',
    sheetId: Number(env.SHEET_ID || 966826055),
    title: env.SHEET_TITLE || '7ка',
    mappingApproved: env.SHEETS_MAPPING_APPROVED === 'true',
    reminderTime: env.REMINDER_TIME || '10:00',
    reminderOffsets: (env.REMINDER_OFFSETS || '14,7,1,0').split(',').map(Number),
    retentionDays: Number(env.CONTACT_RETENTION_DAYS ?? 365),
    backupDir: env.BACKUP_DIR || './backups',
    backupKeep: Number(env.BACKUP_KEEP || 14),
    alertHours: Number(env.SYNC_ALERT_HOURS || 6),
  };
  assert(/^[\d]+:[\w-]+$/.test(c.token), 'Заполните BOT_TOKEN в .env');
  assert(Number.isSafeInteger(c.admin) && c.admin > 0, 'Заполните BOOTSTRAP_ADMIN_ID');
  assert(
    ['disabled', 'memory', 'google'].includes(c.mode),
    'SHEETS_MODE: disabled, memory или google',
  );
  assert(
    !env.TIMEZONE || env.TIMEZONE === 'Europe/Moscow',
    'Поддерживается TIMEZONE=Europe/Moscow',
  );
  assert(/^([01]\d|2[0-3]):[0-5]\d$/.test(c.reminderTime), 'Неверное время напоминаний');
  assert(
    c.reminderOffsets.every((n) => Number.isInteger(n) && n >= 0 && n <= 365),
    'Неверные интервалы напоминаний',
  );
  assert(
    Number.isInteger(c.retentionDays) && c.retentionDays >= 0,
    'Неверный срок хранения контактов',
  );
  assert(
    Number.isInteger(c.backupKeep) && c.backupKeep >= 1,
    'BACKUP_KEEP должен быть положительным',
  );
  return c;
}
