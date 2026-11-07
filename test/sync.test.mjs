import test from 'node:test';
import assert from 'node:assert/strict';
import { MemorySheets } from '../src/sheets/memory.mjs';
import { place } from '../src/service.mjs';
import { setup, key, add, issue, receive, person } from './helpers.mjs';

const legacy = (inv, status = 'Свободен', more = {}) => ({
  inventory: inv,
  mac: '',
  place: 'Клуб 7ки',
  status,
  checked: true,
  owner: '',
  checkin: '',
  checkout: '',
  rawContact: '',
  note: '',
  ...more,
});

test('13–15. A:J, FALSE без номера, неизвестное, Checked, организация, даты и контакты', async (t) => {
  const a = new MemorySheets(
    [
      legacy('L1', 'Выдан', {
        owner: 'Тестовый человек',
        rawContact: 'TG @demo_user +7 (000) 000-00-00',
        checkout: 'НЕСОПОСТАВЛЕННАЯ ДАТА',
      }),
      legacy('L2', '?', { owner: 'Историческая пометка' }),
      legacy('L3', 'Выдан', { owner: 'Студсовет ФПМИ' }),
      legacy('L4'),
      { inventory: '', checked: false },
    ],
    { extras: false },
  );
  const { i, sync } = setup(t, { adapter: a }),
    p = await sync.preview(1);
  assert.equal(p.entries.filter((e) => e.type === 'new').length, 4);
  assert.equal(i.list(1).length, 0);
  const r = await sync.apply(1, p.id);
  assert.equal(r.added, 4);
  const rows = i.list(1),
    l = rows.find((r) => r.inventory === 'L1');
  assert.equal(l.condition, 'needs_check');
  assert.equal(l.loan.dueAt, null);
  assert.equal(l.loan.issuedAt, null);
  assert.equal(l.loan.dueConfirmed, false);
  assert.equal(l.loan.recipient.kind, 'unknown');
  assert.equal(l.loan.recipient.contact.telegram, 'demo_user');
  assert.equal(l.loan.recipient.contact.userId, null);
  assert.ok(l.loan.recipient.contact.raw.startsWith('TG'));
  assert.equal(l.legacy.checkout, 'НЕСОПОСТАВЛЕННАЯ ДАТА');
  assert.equal(l.legacy.checked, true);
  assert.equal(l.loan.place.name, 'Клуб 7ки');
  assert.equal(l.loan.place.certainty, 'legacy');
  assert.equal(rows.find((r) => r.inventory === 'L2').status, 'unknown');
  assert.equal(rows.find((r) => r.inventory === 'L3').loan.recipient.kind, 'organization');
  assert.equal(i.list(1, { report: 'available' }).length, 0);
  const repeat = await sync.preview(1);
  assert.equal(repeat.entries.filter((e) => e.type === 'new').length, 0);
  await sync.apply(1, repeat.id);
  assert.equal(i.list(1).length, 4);
  assert.equal(i.loans(1, l.id).length, 1);
});
test('10. Недоступность таблицы: учёт и outbox сохранены, повтор без дублей', async (t) => {
  const { i, a, sync, clock, s } = setup(t),
    id = add(i);
  a.offline = true;
  assert.equal((await sync.flush()).failed, 1);
  assert.equal(i.get(1, id).status, 'stored');
  assert.equal(s.get('SELECT count(*) AS n FROM outbox').n, 1);
  a.offline = false;
  clock.at = new Date(+clock.at + 10000);
  assert.equal((await sync.flush()).sent, 1);
  assert.equal((await sync.flush()).sent, 0);
  assert.equal(a.sheet.rows.length, 1);
});
test('11. Сортировка и вставка строк не меняют постоянный ID; неизвестная структура блокирует запись', async (t) => {
  const { i, a, sync } = setup(t),
    one = add(i, 'ONE'),
    two = add(i, 'TWO');
  await sync.flush();
  a.sheet.rows.reverse();
  a.sheet.rows.forEach((r, n) => (r.row = n + 8));
  i.execute(2, key(), { kind: 'note', routerId: one, note: 'Проверка ID' });
  assert.equal((await sync.flush()).sent, 1);
  assert.equal(a.sheet.rows.find((r) => r.values.id === one).values.note, 'Проверка ID');
  assert.equal(a.sheet.rows.find((r) => r.values.id === two).values.note, '');
  a.sheet.headers[1] = 'MAC без исходного пробела';
  i.execute(2, key(), { kind: 'note', routerId: two, note: 'Нельзя отправлять' });
  assert.equal((await sync.flush()).blocked, 1);
  assert.equal(a.sheet.rows.find((r) => r.values.id === two).values.note, '');
});
test('16. Ручные место, контакты, комментарий и новая строка: preview, отмена и импорт', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  issue(i, id);
  await sync.flush();
  const v = a.sheet.rows[0].values;
  v.place = 'КДС 7ки';
  v.rawContact = 'TG @changed_demo';
  v.note = 'Ручное примечание';
  a.sheet.rows.push({ row: 20, values: legacy('NEW-MANUAL'), formulas: [] });
  let p = await sync.preview(1);
  assert.equal(p.entries.filter((e) => e.type === 'change').length, 1);
  assert.equal(i.get(1, id).note, '');
  assert.equal(i.list(1).length, 1);
  p = await sync.preview(1);
  const r = await sync.apply(1, p.id);
  assert.equal(r.added, 1);
  assert.equal(r.updated, 1);
  assert.equal(i.get(1, id).loan.place.name, 'КДС 7ки');
  assert.equal(i.get(1, id).loan.recipient.contact.telegram, 'changed_demo');
  assert.equal(i.get(1, id).note, 'Ручное примечание');
  assert.equal(i.loans(1, id).length, 1);
  assert.ok(i.history(1, id).some((e) => e.action === 'Импорт из таблицы' && e.actor === 1));
});
test('16b. Статус и получатель из Sheets проходят предметные проверки, история сохраняется', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  await sync.flush();
  const v = a.sheet.rows[0].values;
  Object.assign(v, {
    status: 'Выдан',
    owner: 'Тестовый из таблицы',
    recipientKind: 'person',
    place: '102',
    issuedAt: '2027-06-01',
    dueAt: '2027-07-01',
    dueConfirmed: true,
    admissionYear: 2026,
  });
  let p = await sync.preview(1);
  assert.equal(p.entries[0].type, 'change');
  await sync.apply(1, p.id);
  await sync.flush();
  assert.equal(i.get(1, id).status, 'issued');
  assert.equal(i.loans(1, id).length, 1);
  const active = a.sheet.rows[0].values;
  active.owner = 'Новый тестовый';
  active.recipientId = '';
  p = await sync.preview(1);
  await sync.apply(1, p.id);
  assert.equal(i.loans(1, id).length, 2);
  assert.equal(i.loans(1, id)[0].closedKind, 'sheet_transfer');
  await sync.flush();
  a.sheet.rows[0].values.status = 'Свободен';
  p = await sync.preview(1);
  assert.equal(p.entries[0].type, 'review');
  await sync.apply(1, p.id);
  assert.equal(i.get(1, id).status, 'issued');
});
test('17. Независимые изменения объединяются, совпадающие принимаются без конфликта', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  issue(i, id);
  await sync.flush();
  i.execute(2, key(), { kind: 'move', routerId: id, place: place('103', 'room') });
  a.sheet.rows[0].values.note = 'Из таблицы';
  const p = await sync.preview(1);
  assert.equal(p.entries[0].type, 'change');
  await sync.apply(1, p.id);
  assert.equal(i.get(1, id).loan.place.name, '103');
  assert.equal(i.get(1, id).note, 'Из таблицы');
  await sync.flush();
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Совпадение' });
  a.sheet.rows[0].values.note = 'Совпадение';
  const same = await sync.preview(1);
  assert.equal(same.entries[0].type, 'change');
  await sync.apply(1, same.id);
  assert.equal(i.get(1, id).note, 'Совпадение');
});
test('17b. Конфликты одного поля и связанного набора не перезаписываются', async (t) => {
  const { i, a, sync, s } = setup(t),
    id = add(i);
  issue(i, id);
  await sync.flush();
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Бот' });
  a.sheet.rows[0].values.note = 'Таблица';
  const p = await sync.preview(1);
  assert.equal(p.entries[0].type, 'conflict');
  await sync.apply(1, p.id);
  assert.equal(i.get(1, id).note, 'Бот');
  assert.equal(s.get('SELECT state FROM outbox WHERE router_id=?', id).state, 'blocked');
  const resolve = await sync.preview(1);
  await sync.apply(1, resolve.id, {
    [id]: { source: 'sheet', reason: 'Принята проверенная правка' },
  });
  assert.equal(i.get(1, id).note, 'Таблица');
  await sync.flush();
  i.execute(2, key(), { kind: 'move', routerId: id, place: place('104', 'room') });
  a.sheet.rows[0].values.owner = 'Другой тестовый';
  const linked = await sync.preview(1);
  assert.ok(linked.entries[0].conflicts.includes('связанные поля выдачи'));
});
test('17c. Удаление, изменение и дубли ID не удаляют БД и не создают двойник', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  await sync.flush();
  const original = structuredClone(a.sheet.rows[0]);
  a.sheet.rows[0].values.id = 'ПОДМЕНА';
  let p = await sync.preview(1);
  assert.ok(p.entries.some((e) => e.type === 'conflict' && e.identity));
  await sync.apply(1, p.id);
  assert.equal(i.list(1).length, 1);
  a.sheet.rows = [];
  p = await sync.preview(1);
  assert.equal(p.entries[0].type, 'missing');
  await sync.apply(1, p.id);
  assert.equal(i.list(1).length, 1);
  a.sheet.rows = [original, structuredClone(original)];
  a.sheet.rows[1].row = 9;
  p = await sync.preview(1);
  assert.equal(p.entries.filter((e) => e.type === 'error').length, 2);
  await sync.apply(1, p.id);
  assert.equal(i.list(1).length, 1);
});
test('18. Отложенная запись не затирает непринятую ручную правку; другие роутеры работают', async (t) => {
  const { i, a, sync } = setup(t),
    one = add(i, 'ONE'),
    two = add(i, 'TWO');
  await sync.flush();
  a.sheet.rows.find((r) => r.values.id === one).values.note = 'Ручное';
  i.execute(2, key(), { kind: 'note', routerId: one, note: 'Из бота' });
  i.execute(2, key(), { kind: 'note', routerId: two, note: 'Независимое' });
  const r = await sync.flush();
  assert.equal(r.blocked, 1);
  assert.equal(r.sent, 1);
  assert.equal(a.sheet.rows.find((r) => r.values.id === one).values.note, 'Ручное');
  const p = await sync.preview(1);
  await sync.apply(1, p.id, { [one]: { source: 'bot', reason: 'Проверено' } });
  await sync.flush();
  assert.equal(a.sheet.rows.find((r) => r.values.id === one).values.note, 'Из бота');
});
test('18b. Изменение Sheets или БД после preview требует нового сравнения', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  await sync.flush();
  a.sheet.rows[0].values.note = 'A';
  let p = await sync.preview(1);
  a.sheet.rows[0].values.note = 'B';
  await assert.rejects(() => sync.apply(1, p.id), /Таблица изменилась/);
  assert.equal(i.get(1, id).note, '');
  p = await sync.preview(1);
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Изменение БД' });
  await assert.rejects(() => sync.apply(1, p.id), /База изменилась/);
});
test('18c. Принятый импорт заменяет старое исходящее задание, повторный sync без правок', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  await sync.flush();
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Устаревшая запись' });
  a.sheet.rows[0].values.note = 'Импортированное';
  const p = await sync.preview(1);
  await sync.apply(1, p.id, { [id]: { source: 'sheet', reason: 'Импорт' } });
  await sync.flush();
  assert.equal(a.sheet.rows[0].values.note, 'Импортированное');
  const clean = await sync.preview(1);
  assert.ok(clean.entries.every((e) => e.type === 'same'));
  assert.equal(i.list(1).length, 1);
});
test('Связанная замена блокируется целиком при ручном конфликте одной стороны', async (t) => {
  const { i, a, sync, s } = setup(t),
    old = add(i, 'OLD'),
    next = add(i, 'NEXT');
  issue(i, old);
  await sync.flush();
  i.execute(2, key(), {
    kind: 'replace',
    routerId: old,
    newRouterId: next,
    inventory: 'OLD',
    physical: true,
    storage: place('Склад', 'warehouse'),
    condition: 'broken',
    kit: { power: true, cable: true, other: '' },
    reason: 'Замена',
  });
  a.sheet.rows.find((r) => r.values.id === old).values.note = 'Ручная правка';
  assert.equal((await sync.flush()).blocked, 2);
  assert.equal(a.sheet.rows.find((r) => r.values.id === next).values.status, 'Свободен');
  assert.equal(s.get('SELECT count(*) AS n FROM outbox').n, 2);
});
test('Права на sync и формулы при исходящей записи', async (t) => {
  const { i, a, sync } = setup(t),
    id = add(i);
  await sync.flush();
  await assert.rejects(() => sync.preview(2), /Нет доступа/);
  await assert.rejects(() => sync.preview(3), /Нет доступа/);
  a.sheet.rows[0].formulas = ['note'];
  i.execute(2, key(), { kind: 'note', routerId: id, note: '=1+1' });
  assert.equal((await sync.flush()).blocked, 1);
  assert.equal(a.sheet.rows[0].values.note, '');
});
