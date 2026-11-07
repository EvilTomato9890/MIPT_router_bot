import test from 'node:test';
import assert from 'node:assert/strict';
import { GoogleSheets } from '../src/sheets/google.mjs';
import { LEGACY_HEADERS, EXTRA_HEADERS, rowValues, LAST_COLUMN } from '../src/sheets/schema.mjs';

function wire() {
  const headers = [...LEGACY_HEADERS, '', '', '', '', ...EXTRA_HEADERS],
    calls = [],
    cells = [headers, ['G-1', '', '101', 'Свободен', false, '', '', '', '', '']],
    formulaCells = new Map();
  const client = {
    async request(r) {
      calls.push(structuredClone(r));
      const url = new URL(r.url);
      if (url.searchParams.get('includeGridData')) {
        const rowData = cells.map((row, ri) => ({
          values: row.map((v, ci) => ({
            userEnteredValue: formulaCells.has(ri + ',' + ci)
              ? { formulaValue: formulaCells.get(ri + ',' + ci) }
              : { stringValue: String(v) },
          })),
        }));
        return {
          data: {
            sheets: [
              {
                data: [
                  {
                    startRow: 0,
                    startColumn: 0,
                    rowData: rowData.map((r) => ({ values: r.values.slice(0, 10) })),
                  },
                  {
                    startRow: 0,
                    startColumn: 14,
                    rowData: rowData.map((r) => ({ values: r.values.slice(14) })),
                  },
                ],
              },
            ],
          },
        };
      }
      if (url.pathname.endsWith('/values:batchGet'))
        return {
          data: {
            valueRanges: [
              { values: cells.map((r) => r.slice(0, 10)) },
              { values: cells.map((r) => r.slice(14)) },
            ],
          },
        };
      if (url.pathname.endsWith('/values:batchUpdate')) {
        assert.equal(r.data.valueInputOption, 'RAW');
        for (const d of r.data.data) {
          const m = d.range.match(/!([A-Z]+)(\d+)$/);
          assert.ok(m);
          let index = 0;
          for (const c of m[1]) index = index * 26 + c.charCodeAt(0) - 64;
          cells[Number(m[2]) - 1] ??= [];
          cells[Number(m[2]) - 1][index - 1] = d.values[0][0];
        }
        return { data: {} };
      }
      if (url.pathname.endsWith(':batchUpdate')) return { data: {} };
      return {
        data: {
          properties: { locale: 'en_GB', timeZone: 'Europe/Moscow', autoRecalc: 'HOUR' },
          sheets: [
            {
              properties: {
                sheetId: 966826055,
                title: '7ка',
                gridProperties: { rowCount: 996, columnCount: 41 },
              },
              conditionalFormats: [],
            },
            {
              properties: {
                sheetId: 0,
                title: '2ка',
                gridProperties: { rowCount: 1000, columnCount: 26 },
              },
            },
          ],
        },
      };
    },
  };
  const adapter = new GoogleSheets(
    { spreadsheetId: 'test-id', sheetId: 966826055, title: '7ка', mappingApproved: true },
    client,
  );
  return { adapter, calls, cells, formulaCells };
}
test('Google wire: точные заголовки, целевой лист, безопасные диапазоны и RAW значения', async () => {
  const { adapter, calls } = wire(),
    s = await adapter.read();
  assert.equal(LAST_COLUMN, 'AO');
  assert.equal(s.title, '7ка');
  assert.equal(s.headers[8][0], 'С');
  assert.equal(s.rows[0].values.inventory, 'G-1');
  const v = {
    ...s.rows[0].values,
    id: 'test-uuid',
    note: '=IMPORTXML("https://example.test","//x")',
  };
  await adapter.write([{ routerId: 'test-uuid', row: 2, previous: s.rows[0].values, values: v }]);
  const write = calls.find((r) => r.url.endsWith('/values:batchUpdate'));
  assert.equal(write.data.valueInputOption, 'RAW');
  assert.ok(write.data.data.every((d) => /!'?[A-Z]+\d+$/.test(d.range)));
  assert.ok(write.data.data.every((d) => !/^'2ка'!/.test(d.range)));
  assert.ok(write.data.data.every((d) => !/[!](K|L|M|N)\d/.test(d.range)));
  assert.equal(write.data.data.find((d) => d.range.endsWith('J2')).values[0][0], v.note);
});
test('Google wire: формулы распознаются по formulaValue; буквальный =текст не становится формулой', async () => {
  const { adapter, cells, formulaCells, calls } = wire();
  cells[1][9] = 'Вычисленное значение';
  formulaCells.set('1,9', '=B2');
  let s = await adapter.read();
  assert.deepEqual(s.rows[0].formulas, ['note']);
  await assert.rejects(
    () =>
      adapter.write([
        {
          routerId: 'test',
          row: 2,
          previous: s.rows[0].values,
          values: { ...s.rows[0].values, id: 'test', note: 'Перезапись' },
        },
      ]),
    /формул/,
  );
  assert.ok(!calls.some((r) => r.url.endsWith('/values:batchUpdate')));
  formulaCells.clear();
  cells[1][9] = '=буквальный текст';
  s = await adapter.read();
  assert.deepEqual(s.rows[0].formulas, []);
});
test('Google wire: повторное чтение перед записью блокирует позднюю ручную правку', async () => {
  const { adapter, cells, calls } = wire(),
    s = await adapter.read();
  cells[1][9] = 'Ручная правка после preview';
  await assert.rejects(
    () =>
      adapter.write([
        {
          routerId: 'test',
          row: 2,
          previous: s.rows[0].values,
          values: { ...s.rows[0].values, id: 'test' },
        },
      ]),
    /непосредственно/,
  );
  assert.ok(!calls.some((r) => r.url.endsWith('/values:batchUpdate')));
});
