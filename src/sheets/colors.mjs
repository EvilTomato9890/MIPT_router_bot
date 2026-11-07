import { assert, clone, equal, hash } from '../util.mjs';
import { EXTRA_FIELDS, column, validateSheet } from './schema.mjs';

const MARKER = 'routerbot:status:v1';
export const PALETTE = {
  overdue: [0.95686275, 0.78039217, 0.7647059],
  lost: [0.63529414, 0.76862746, 0.7882353],
  stored: [0.7176471, 0.88235295, 0.8039216],
  issued: [1, 0.8980392, 0.6],
  unknown: [0.86, 0.86, 0.86],
  written_off: [0.76, 0.69, 0.83],
};
const rgb = (a) => ({ red: a[0], green: a[1], blue: a[2] });
export function expectedRules(sheet) {
  const due = '$' + column(14 + EXTRA_FIELDS.indexOf('dueAt')) + '2';
  const confirmed = '$' + column(14 + EXTRA_FIELDS.indexOf('dueConfirmed')) + '2';
  const tests = {
    overdue: `OR(TRIM($D2)="Просрочен",AND(TRIM($D2)="Выдан",${confirmed}=TRUE,REGEXMATCH(TO_TEXT(${due}),"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),IFERROR(DATE(VALUE(LEFT(${due},4)),VALUE(MID(${due},6,2)),VALUE(RIGHT(${due},2)))<TODAY(),FALSE)))`,
    lost: 'TRIM($D2)="Утерян"',
    stored: 'TRIM($D2)="Свободен"',
    issued: 'TRIM($D2)="Выдан"',
    unknown: 'OR(TRIM($D2)="?",TRIM($D2)="Неизвестно")',
    written_off: 'TRIM($D2)="Списан"',
  };
  return Object.entries(tests).map(([name, test]) => ({
    ranges: [{ sheetId: sheet.sheetId, startRowIndex: 1, startColumnIndex: 0, endColumnIndex: 10 }],
    booleanRule: {
      condition: {
        type: 'CUSTOM_FORMULA',
        values: [{ userEnteredValue: `=AND(N("${MARKER}")=0,LEN(TRIM(TO_TEXT($A2)))>0,${test})` }],
      },
      format: {
        backgroundColor: rgb(PALETTE[name]),
        textFormat: { foregroundColor: { red: 0.1, green: 0.1, blue: 0.1 } },
      },
    },
  }));
}
export function isManaged(rule, sheetId) {
  const formula = rule.booleanRule?.condition?.values?.[0]?.userEnteredValue || '';
  if (formula.includes(MARKER)) return true;
  // Adopt only exact legacy status rules in the agreed accounting area.
  return (
    /^=\$D\d+\s*=\s*"(?:Свободен|Выдан|Просрочен|Утерян|\?|Неизвестно|Списан)"$/.test(formula) &&
    rule.ranges?.every(
      (r) => r.sheetId === sheetId && (r.startColumnIndex || 0) === 0 && r.endColumnIndex === 10,
    ) &&
    Boolean(
      rule.booleanRule?.format?.backgroundColor || rule.booleanRule?.format?.backgroundColorStyle,
    )
  );
}
function canonical(rule) {
  const r = clone(rule),
    fmt = r.booleanRule?.format;
  if (fmt) {
    if (fmt.backgroundColorStyle) {
      fmt.backgroundColor = fmt.backgroundColorStyle.rgbColor;
      delete fmt.backgroundColorStyle;
    }
    if (fmt.textFormat?.foregroundColorStyle) {
      fmt.textFormat.foregroundColor = fmt.textFormat.foregroundColorStyle.rgbColor;
      delete fmt.textFormat.foregroundColorStyle;
    }
    for (const color of [fmt.backgroundColor, fmt.textFormat?.foregroundColor])
      if (color)
        for (const f of ['red', 'green', 'blue']) color[f] = Number((color[f] || 0).toFixed(5));
  }
  for (const range of r.ranges || [])
    for (const f of ['startColumnIndex', 'startRowIndex']) range[f] ??= 0;
  return r;
}
export function colorPlan(sheet) {
  validateSheet(sheet, { requireExtras: true });
  assert(
    sheet.timeZone === 'Europe/Moscow',
    'Часовой пояс таблицы должен быть Europe/Moscow. Согласуйте его с владельцем.',
  );
  assert(
    ['en_GB', 'en_US'].includes(sheet.locale),
    'Для текущих формул проверены локали en_GB/en_US. Другая локаль требует отдельной проверки на копии.',
  );
  const rules = sheet.rules || [],
    expected = expectedRules(sheet),
    managed = rules.map((r, i) => (isManaged(r, sheet.sheetId) ? i : -1)).filter((i) => i >= 0);
  const good =
    managed.length === expected.length &&
    managed.every((i, j) => i === j && equal(canonical(rules[i]), canonical(expected[j])));
  const requests = [],
    issues = [],
    labels = ['Просрочен', 'Утерян', 'Свободен', 'Выдан', 'Неизвестно', 'Списан'];
  labels.forEach((label, j) => {
    const candidates = managed.filter((i) => {
      const f = rules[i].booleanRule.condition.values[0].userEnteredValue;
      return label === 'Неизвестно'
        ? f.includes('"?"') || f.includes('"Неизвестно"')
        : f.includes('"' + label + '"') && !(label === 'Выдан' && f.includes('"Просрочен"'));
    });
    if (!candidates.length) issues.push(`${label}: правило отсутствует`);
    if (candidates.length > 1) issues.push(`${label}: дублируется (${candidates.length})`);
    for (const i of candidates) {
      if (!equal(canonical(rules[i].ranges), canonical(expected[j].ranges)))
        issues.push(`${label}: проверить диапазон A2:J без конца`);
      if (i !== j) issues.push(`${label}: неверный приоритет ${i + 1}, нужен ${j + 1}`);
      if (!equal(canonical(rules[i]), canonical(expected[j])))
        issues.push(`${label}: формула, ссылка или оформление требуют обновления`);
    }
  });
  if (!good) {
    for (const index of [...managed].reverse())
      requests.push({ deleteConditionalFormatRule: { sheetId: sheet.sheetId, index } });
    expected.forEach((rule, index) => requests.push({ addConditionalFormatRule: { rule, index } }));
  }
  if (!['HOUR', 'MINUTE'].includes(sheet.autoRecalc)) {
    requests.push({
      updateSpreadsheetProperties: { properties: { autoRecalc: 'HOUR' }, fields: 'autoRecalc' },
    });
    issues.push('Смена дня: включить пересчёт HOUR для книги');
  }
  const checked = rules.filter((r) =>
    /\$E\d+/.test(r.booleanRule?.condition?.values?.[0]?.userEnteredValue || ''),
  );
  return {
    digest: hash({
      rules,
      headers: sheet.headers,
      timeZone: sheet.timeZone,
      autoRecalc: sheet.autoRecalc,
    }),
    requests,
    issues,
    recognized: managed.length,
    foreign: rules.length - managed.length,
    legacyChecked: checked,
    expected: expected.length,
    changed: requests.length > 0,
    notes: [
      'Диапазон A2:J без фиксированного конца; заголовок и L:N исключены.',
      'Просрочка имеет приоритет; срок читается из подтверждённых новых полей, не Checkout.',
      'Наследуемое зачёркивание E=TRUE сохранится без изменения.',
      'Для смены дня пересчёт таблицы — HOUR (вся книга).',
    ],
  };
}
export class ColorRepair {
  constructor(inventory, adapter) {
    this.i = inventory;
    this.a = adapter;
  }
  async preview(actor) {
    this.i.authorize(actor, 'admin');
    const sheet = await this.a.read();
    this.i.authorize(actor, 'admin');
    const p = colorPlan(sheet);
    return { id: this.i.s.draft(actor, 'colors', p, this.i.now()), ...p };
  }
  async apply(actor, id) {
    this.i.authorize(actor, 'admin');
    const d = this.i.s.getDraft(actor, id, this.i.now());
    assert(d.kind === 'colors', 'Неверное подтверждение');
    const previous = this.i.s.get('SELECT actor,result FROM requests WHERE key=?', 'colors:' + id);
    if (previous) {
      assert(previous.actor === actor, 'Нет доступа');
      return JSON.parse(previous.result);
    }
    const sheet = await this.a.read();
    this.i.authorize(actor, 'admin');
    const fresh = colorPlan(sheet);
    assert(
      fresh.digest === d.data.digest,
      'Правила изменились после просмотра; повторите /repair_colors',
      'stale',
    );
    if (fresh.changed) await this.a.batch(fresh.requests);
    const result = colorPlan(await this.a.read());
    assert(!result.changed, 'Правила не прошли проверку после записи');
    return this.i.s.atomic(actor, 'colors:' + id, { color: id }, () => {
      this.i.authorize(actor, 'admin');
      this.i.s.set('colors_managed', true);
      this.i.s.event(actor, 'sheet:' + sheet.sheetId, 'Восстановление раскраски', null, {
        recognized: fresh.recognized,
        expected: fresh.expected,
      });
      return { changed: fresh.changed, checkedPreserved: fresh.legacyChecked.length };
    });
  }
}
