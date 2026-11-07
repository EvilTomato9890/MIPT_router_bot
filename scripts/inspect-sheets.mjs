import { GoogleSheets } from '../src/sheets/google.mjs';
import { validateSheet } from '../src/sheets/schema.mjs';
import { assert, DomainError } from '../src/util.mjs';
try {
  assert(
    process.env.SHEETS_MODE === 'google',
    'Для проверки задайте SHEETS_MODE=google и Google-доступ. Записей команда не выполняет.',
  );
  const a = new GoogleSheets({
    spreadsheetId: process.env.SPREADSHEET_ID || '1N6lyJS7-PZ_hunmjnY06-0tnEEeOaP0Ypn-yEVfooK4',
    sheetId: Number(process.env.SHEET_ID || 966826055),
    title: process.env.SHEET_TITLE || '7ка',
    credentials: process.env.GOOGLE_APPLICATION_CREDENTIALS,
  });
  const s = await a.read(),
    extras = validateSheet(s),
    rows = s.rows.filter((r) => String(r.values.inventory ?? '').trim()),
    statuses = {};
  for (const r of rows) {
    const status = String(r.values.status).trim();
    statuses[status] = (statuses[status] || 0) + 1;
  }
  console.info(
    JSON.stringify(
      {
        readOnly: true,
        title: s.title,
        sheetId: s.sheetId,
        locale: s.locale,
        timeZone: s.timeZone,
        headers: s.headers,
        records: rows.length,
        statuses,
        permanentIdColumnsReady: extras,
        formattingRules: s.rules.length,
      },
      null,
      2,
    ),
  );
} catch (e) {
  console.error(
    e instanceof DomainError
      ? e.message
      : 'Чтение Google не удалось. Проверьте ключ, API, ID таблицы и право доступа сервисного аккаунта.',
  );
  process.exitCode = 1;
}
