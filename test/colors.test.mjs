import test from 'node:test';
import assert from 'node:assert/strict';
import { expectedRules, colorPlan, PALETTE } from '../src/sheets/colors.mjs';
import { setup } from './helpers.mjs';

const checked = {
  ranges: [
    {
      sheetId: 966826055,
      startRowIndex: 0,
      endRowIndex: 996,
      startColumnIndex: 0,
      endColumnIndex: 10,
    },
  ],
  booleanRule: {
    condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: '=$E1=ИСТИНА' }] },
    format: { textFormat: { strikethrough: true } },
  },
};
const legacy = {
  ranges: [
    {
      sheetId: 966826055,
      startRowIndex: 0,
      endRowIndex: 996,
      startColumnIndex: 0,
      endColumnIndex: 10,
    },
  ],
  booleanRule: {
    condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: '=$D1="Выдан"' }] },
    format: { backgroundColor: { red: 1, green: 0.9, blue: 0.6 } },
  },
};
test('19. Repair сохраняет Checked, постороннее оформление и все данные; повтор без дублей', async (t) => {
  const { a, colors, i, s } = setup(t),
    foreign = {
      ranges: [{ sheetId: 966826055, startColumnIndex: 11, endColumnIndex: 14 }],
      booleanRule: {
        condition: { type: 'TEXT_EQ', values: [{ userEnteredValue: 'Легенда' }] },
        format: { textFormat: { bold: true } },
      },
    };
  a.sheet.rules = [legacy, checked, foreign, structuredClone(legacy)];
  a.sheet.autoRecalc = 'ON_CHANGE';
  const data = JSON.stringify(a.sheet.rows),
    preview = await colors.preview(1);
  assert.ok(preview.issues.some((x) => x.includes('дублируется')));
  assert.equal(preview.legacyChecked.length, 1);
  await colors.apply(1, preview.id);
  assert.equal(a.sheet.rules.length, 8);
  assert.deepEqual(a.sheet.rules.slice(-2), [checked, foreign]);
  assert.equal(JSON.stringify(a.sheet.rows), data);
  assert.equal(i.stats(1).physical, 0);
  const repeat = await colors.preview(1);
  assert.equal(repeat.changed, false);
  await colors.apply(1, repeat.id);
  const n = s.get('SELECT count(*) AS n FROM events').n;
  await colors.apply(1, repeat.id);
  assert.equal(s.get('SELECT count(*) AS n FROM events').n, n);
  assert.equal(a.sheet.rules.length, 8);
});
test('20. Формулы всех статусов, оба неизвестных, пустые строки, новые строки, срок и приоритет', (t) => {
  const { a } = setup(t),
    rules = expectedRules(a.sheet),
    formulas = rules.map((r) => r.booleanRule.condition.values[0].userEnteredValue);
  assert.equal(rules.length, 6);
  for (const r of rules) {
    assert.equal(r.ranges[0].startRowIndex, 1);
    assert.equal(r.ranges[0].endColumnIndex, 10);
    assert.equal(r.ranges[0].endRowIndex, undefined);
  }
  assert.match(formulas[0], /TODAY\(\)/);
  assert.match(formulas[0], /\$AD2/);
  assert.match(formulas[0], /\$AE2=TRUE/);
  assert.ok(!formulas[0].includes('$H2'));
  assert.match(formulas[0], /TRIM\(\$D2\)="Выдан"/);
  assert.ok(formulas.every((f) => f.includes('LEN(TRIM(TO_TEXT($A2)))>0')));
  assert.ok(formulas[4].includes('"?"') && formulas[4].includes('"Неизвестно"'));
  assert.ok(!formulas.some((f) => f.includes('VIP') || f.includes('$E2')));
  assert.notDeepEqual(PALETTE.unknown, PALETTE.written_off);
});
test('20b. Административные права и свежесть восстановления проверяются повторно', async (t) => {
  const { colors, a, i } = setup(t);
  await assert.rejects(() => colors.preview(2), /Нет доступа/);
  await assert.rejects(() => colors.preview(3), /Нет доступа/);
  const p = await colors.preview(1);
  a.sheet.rules.push(checked);
  await assert.rejects(() => colors.apply(1, p.id), /Правила изменились/);
  i.setRole(1, 2, 'admin', 'color:grant');
  const next = await colors.preview(2);
  i.setRole(1, 2, null, 'color:revoke');
  await assert.rejects(() => colors.apply(2, next.id), /Нет доступа/);
});
test('Неподдержанная локаль останавливает repair до записи', (t) => {
  const { a } = setup(t),
    plan = colorPlan(a.sheet);
  assert.ok(plan.changed);
  a.sheet.locale = 'ru_RU';
  assert.throws(() => colorPlan(a.sheet), /локал/);
});
