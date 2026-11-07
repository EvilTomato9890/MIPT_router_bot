import { GoogleAuth } from 'google-auth-library';
import { assert, equal, DomainError } from '../util.mjs';
import {
  column,
  LAST_COLUMN,
  EXTRA_FIELDS,
  EXTRA_HEADERS,
  LEGACY_FIELDS,
  rowValues,
  validateSheet,
} from './schema.mjs';

export class GoogleSheets {
  constructor(
    { spreadsheetId, sheetId, title, credentials, mappingApproved = false },
    client = null,
  ) {
    assert(/^[\w-]+$/.test(spreadsheetId), 'Неверный Spreadsheet ID');
    this.id = spreadsheetId;
    this.sheetId = sheetId;
    this.title = title;
    this.approved = mappingApproved;
    this.auth =
      client ||
      new GoogleAuth({
        keyFile: credentials,
        scopes: ['https://www.googleapis.com/auth/spreadsheets'],
      });
  }
  async request(path, method = 'GET', body) {
    try {
      const c = typeof this.auth.getClient === 'function' ? await this.auth.getClient() : this.auth;
      const result = await c.request({
        url: `https://sheets.googleapis.com/v4/spreadsheets/${this.id}${path}`,
        method,
        data: body,
        timeout: 30000,
        retry: false,
      });
      return result.data;
    } catch {
      throw new DomainError(
        'Google Sheets недоступна. Локальные данные сохранены; проверьте подключение и повторите команду.',
        'network',
      );
    }
  }
  async metadata() {
    return this.request(
      '?fields=spreadsheetId,properties(locale,timeZone,autoRecalc),sheets(properties,conditionalFormats)',
    );
  }
  async read() {
    const meta = await this.metadata(),
      target = meta.sheets.find((s) => s.properties.sheetId === this.sheetId);
    assert(
      target && target.properties.title === this.title,
      'ID или имя целевого листа изменились; запись остановлена',
      'schema',
    );
    const { rowCount, columnCount } = target.properties.gridProperties;
    const prefix = "'" + this.title.replaceAll("'", "''") + "'!";
    const ranges = [`${prefix}A1:J${rowCount}`];
    if (columnCount > 14)
      ranges.push(
        `${prefix}O1:${column(Math.min(columnCount, 14 + EXTRA_FIELDS.length) - 1)}${rowCount}`,
      );
    const query = ranges.map((r) => 'ranges=' + encodeURIComponent(r)).join('&');
    const [values, grid] = await Promise.all([
      this.request('/values:batchGet?' + query + '&valueRenderOption=UNFORMATTED_VALUE'),
      this.request(
        '?' +
          query +
          '&includeGridData=true&fields=sheets(data(startRow,startColumn,rowData(values(userEnteredValue))))',
      ),
    ]);
    const base = values.valueRanges[0].values || [],
      extra = values.valueRanges[1]?.values || [],
      formulas = new Map();
    let gridEnd = 0;
    for (const sheet of grid.sheets || [])
      for (const data of sheet.data || []) {
        gridEnd = Math.max(gridEnd, (data.startRow || 0) + (data.rowData?.length || 0));
        (data.rowData || []).forEach((row, ri) =>
          row.values?.forEach((cell, ci) => {
            if (cell.userEnteredValue?.formulaValue) {
              const index = (data.startColumn || 0) + ci,
                field =
                  index < 10 ? LEGACY_FIELDS[index] : index >= 14 ? EXTRA_FIELDS[index - 14] : null;
              if (field) {
                const number = (data.startRow || 0) + ri + 1;
                formulas.set(number, [...(formulas.get(number) || []), field]);
              }
            }
          }),
        );
      }
    const header = [...(base[0] || [])];
    extra[0]?.forEach((v, i) => (header[14 + i] = v));
    const hasExtras = EXTRA_HEADERS.every((h, i) => header[14 + i] === h),
      rows = [];
    for (let i = 1; i < Math.max(base.length, extra.length, gridEnd); i++) {
      const cells = [...(base[i] || [])];
      (extra[i] || []).forEach((v, j) => (cells[14 + j] = v));
      rows.push({
        row: i + 1,
        values: rowValues(cells, hasExtras),
        formulas: formulas.get(i + 1) || [],
      });
    }
    return {
      sheetId: this.sheetId,
      title: this.title,
      headers: header,
      rows,
      rules: target.conditionalFormats || [],
      rowCount,
      columnCount,
      ...meta.properties,
    };
  }
  async provision() {
    assert(this.approved, 'Сначала согласуйте схему O:' + LAST_COLUMN);
    const s = await this.read();
    validateSheet(s);
    const empty = s.headers.slice(14).every((x) => !x);
    if (!empty) {
      validateSheet(s, { requireExtras: true });
      return { changed: false };
    }
    // Never take over populated columns just because their headers are empty.
    for (const row of s.rows)
      assert(
        EXTRA_FIELDS.every((f) => !row.values[f]),
        'Служебная область занята; выберите отдельный лист вручную',
      );
    if (s.columnCount > 14) {
      const raw = await this.request(
        '/values/' +
          encodeURIComponent(
            "'" +
              this.title.replaceAll("'", "''") +
              "'!O2:" +
              column(Math.min(s.columnCount, 41) - 1) +
              s.rowCount,
          ) +
          '?valueRenderOption=FORMULA',
      );
      assert(
        (raw.values || []).every((r) => r.every((v) => v === '' || v === null)),
        'В O:' + LAST_COLUMN + ' есть данные / формулы. Подключение остановлено.',
      );
    }
    const req = [];
    if (s.columnCount < 41)
      req.push({
        appendDimension: {
          sheetId: this.sheetId,
          dimension: 'COLUMNS',
          length: 41 - s.columnCount,
        },
      });
    req.push({
      updateCells: {
        range: {
          sheetId: this.sheetId,
          startRowIndex: 0,
          endRowIndex: 1,
          startColumnIndex: 14,
          endColumnIndex: 41,
        },
        rows: [{ values: EXTRA_HEADERS.map((v) => ({ userEnteredValue: { stringValue: v } })) }],
        fields: 'userEnteredValue',
      },
    });
    await this.batch(req);
    validateSheet(await this.read(), { requireExtras: true });
    return { changed: true };
  }
  async write(writes) {
    assert(this.approved, 'Запись Google отключена до согласования схемы', 'schema');
    const s = await this.read();
    validateSheet(s, { requireExtras: true });
    const data = [],
      used = new Set(
        s.rows
          .filter(
            (r) =>
              r.formulas.length ||
              Object.entries(r.values).some(([k, v]) => k !== 'checked' && v !== '' && v !== null),
          )
          .map((r) => r.row),
      );
    let max = s.rowCount;
    for (const w of writes) {
      let found = s.rows.filter((r) => r.values.id === w.routerId);
      if (!found.length && w.row) found = s.rows.filter((r) => r.row === w.row);
      assert(found.length <= 1, 'Дубли router_id', 'conflict');
      const old = found[0];
      assert(
        (!old && !w.previous) || (old && equal(old.values, w.previous)),
        'Таблица изменилась непосредственно перед записью',
        'conflict',
      );
      if (!old)
        assert(
          !s.rows.some((r) => r.values.inventory === w.values.inventory),
          'Новый инвентарный номер уже существует',
          'conflict',
        );
      let row = old?.row;
      if (!row) {
        row = 2;
        while (used.has(row)) row++;
        used.add(row);
        max = Math.max(max, row);
      }
      const fields = [...LEGACY_FIELDS, ...EXTRA_FIELDS];
      for (const f of fields) {
        if (old && equal(old.values[f], w.values[f])) continue;
        assert(
          !old?.formulas.includes(f),
          'Бот не перезаписывает формулу; разберите поле вручную',
          'conflict',
        );
        const idx = LEGACY_FIELDS.includes(f)
          ? LEGACY_FIELDS.indexOf(f)
          : 14 + EXTRA_FIELDS.indexOf(f);
        data.push({
          range: "'" + this.title.replaceAll("'", "''") + "'!" + column(idx) + row,
          values: [[w.values[f] ?? '']],
        });
      }
    }
    if (max > s.rowCount)
      await this.batch([
        { appendDimension: { sheetId: this.sheetId, dimension: 'ROWS', length: max - s.rowCount } },
      ]);
    if (data.length)
      await this.request('/values:batchUpdate', 'POST', { valueInputOption: 'RAW', data });
  }
  async batch(requests) {
    assert(this.approved, 'Изменения Google отключены до согласования схемы');
    if (requests.length) await this.request(':batchUpdate', 'POST', { requests });
  }
}
