import { clone, assert, equal } from '../util.mjs';
import { LEGACY_HEADERS, EXTRA_HEADERS, rowValues } from './schema.mjs';

// Test double. Never presented as a verified Google connection.
export class MemorySheets {
  constructor(rows = [], { extras = true } = {}) {
    this.sheet = {
      sheetId: 966826055,
      title: '7ка',
      locale: 'en_GB',
      timeZone: 'Europe/Moscow',
      autoRecalc: 'HOUR',
      rowCount: 1000,
      columnCount: 41,
      headers: [...LEGACY_HEADERS, '', '', '', '', ...EXTRA_HEADERS],
      rules: [],
      rows: rows.map((cells, i) => ({
        row: i + 2,
        values: Array.isArray(cells) ? rowValues(cells, extras) : clone(cells),
        formulas: [],
      })),
    };
    if (!extras) this.sheet.headers = LEGACY_HEADERS.slice();
    this.offline = false;
    this.writeCalls = 0;
    this.beforeWrite = null;
  }
  async read() {
    assert(!this.offline, 'Таблица недоступна');
    return clone(this.sheet);
  }
  async write(writes) {
    assert(!this.offline, 'Таблица недоступна');
    if (this.beforeWrite) await this.beforeWrite();
    const next = clone(this.sheet);
    for (const w of writes) {
      const found =
        next.rows.find((r) => r.values.id === w.routerId) ||
        (w.row ? next.rows.find((r) => r.row === w.row) : null);
      assert(
        (!found && !w.previous) || (found && equal(found.values, w.previous)),
        'Таблица изменилась перед записью',
        'conflict',
      );
      for (const f of w.formulas || [])
        assert(
          equal(w.previous?.[f], w.values[f]),
          'Изменение формулы требует отдельного разбора',
          'conflict',
        );
      if (found) found.values = clone(w.values);
      else
        next.rows.push({
          row: Math.max(1, ...next.rows.map((r) => r.row)) + 1,
          values: clone(w.values),
          formulas: [],
        });
    }
    this.sheet = next;
    this.writeCalls++;
  }
  async batch(requests) {
    assert(!this.offline, 'Таблица недоступна');
    for (const req of requests) {
      if (req.deleteConditionalFormatRule)
        this.sheet.rules.splice(req.deleteConditionalFormatRule.index, 1);
      if (req.addConditionalFormatRule)
        this.sheet.rules.splice(
          req.addConditionalFormatRule.index,
          0,
          clone(req.addConditionalFormatRule.rule),
        );
      if (req.updateSpreadsheetProperties)
        Object.assign(this.sheet, req.updateSpreadsheetProperties.properties);
    }
  }
}
