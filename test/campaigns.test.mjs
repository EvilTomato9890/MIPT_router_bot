import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store } from '../src/db.mjs';
import { Inventory, place } from '../src/service.mjs';
import { Campaigns } from '../src/campaigns.mjs';
import { setup, key, add, issue, receive, person, campaign } from './helpers.mjs';

test('07. VIP участвует в сборе, исключение и продление меняют очередь и напоминания', async (t) => {
  const { i, c, clock } = setup(t),
    id = add(i);
  issue(i, id);
  i.execute(1, key(), { kind: 'vip', routerId: id, vip: true, reason: 'Особая отметка' });
  const cid = await campaign(i, c);
  assert.equal(c.queue(2, cid).length, 1);
  clock.at = new Date('2027-06-17T10:00:00+03:00');
  const sent = [];
  const send = async (...args) => sent.push(args);
  assert.equal((await c.tick(send)).sent, 1);
  assert.equal((await c.tick(send)).sent, 0);
  i.execute(1, key(), { kind: 'extend', routerId: id, dueAt: '2027-08-01', reason: 'Продление' });
  assert.equal(c.queue(2, cid)[0].due, '2027-08-01');
  assert.equal((await c.tick(send)).sent, 0);
  i.execute(1, key(), {
    kind: 'exception',
    routerId: id,
    enabled: true,
    reviewAt: '2027-09-01',
    reason: 'Явное исключение',
  });
  assert.equal(c.queue(2, cid)[0].excluded, true);
  clock.at = new Date('2027-08-02T10:00:00+03:00');
  assert.equal((await c.tick(send)).sent, 0);
  i.execute(1, key(), {
    kind: 'exception',
    routerId: id,
    enabled: false,
    reason: 'Возобновить сбор',
  });
  assert.equal((await c.tick(send)).sent, 1);
  assert.equal(i.get(1, id).status, 'issued');
});
test('Кампания не угадывает набор, не включает организации и общие помещения', async (t) => {
  const { i, c } = setup(t),
    one = add(i, 'STUDENT'),
    two = add(i, 'ORG'),
    three = add(i, 'COMMON'),
    four = add(i, 'UNKNOWN');
  issue(i, one);
  issue(i, two, {
    recipient: { kind: 'organization', name: 'Тестовая организация' },
    place: place('Клуб 7ки', 'common'),
  });
  issue(i, three, { place: place('Общий зал', 'common') });
  issue(i, four, { recipient: { kind: 'person', name: 'Без подтверждённого набора' } });
  const cid = await campaign(i, c),
    p = c.preview(1, cid);
  assert.equal(p.included.length, 1);
  assert.equal(p.clarify.length, 1);
  assert.equal(c.queue(2, cid).length, 1);
  c.member(1, key(), cid, i.get(1, four).loan.id, {
    action: 'include',
    reason: 'Набор проверен отдельно',
  });
  assert.equal(c.queue(2, cid).length, 2);
});
test('Очередь: просрочка, сроки, естественная сортировка, действия не означают возврат', async (t) => {
  const { i, c, clock } = setup(t),
    one = add(i, 'A'),
    two = add(i, 'B'),
    three = add(i, 'C');
  issue(i, one, { place: place('102', 'room') });
  issue(i, two, { place: place('12', 'room') });
  issue(i, three, { place: place('2', 'room') });
  const cid = await campaign(i, c);
  assert.deepEqual(
    c.queue(2, cid).map((x) => x.loan.place.name),
    ['2', '12', '102'],
  );
  const loan = i.get(1, one).loan;
  c.member(2, key(), cid, loan.id, { action: 'not_home', reason: 'Не застали' });
  assert.equal(i.get(1, one).status, 'issued');
  c.member(2, key(), cid, loan.id, { action: 'new_date', reason: 'Обещал сдать позже' });
  assert.equal(i.get(1, one).loan.dueAt, '2027-07-01');
  assert.throws(
    () => c.member(2, key(), cid, loan.id, { action: 'priority', reason: 'x', priority: 1 }),
    /Нет доступа/,
  );
  receive(i, one);
  assert.equal(c.queue(2, cid).length, 2);
  assert.equal(c.report(2, cid).returned, 1);
  clock.at = new Date('2027-07-02T10:00:00+03:00');
  assert.equal(c.report(2, cid).overdue, 2);
});
test('09. После простоя — одна актуальная сводка; возврат и отзыв доступа отменяют отправки', async (t) => {
  const { i, c, clock, s } = setup(t),
    one = add(i, 'A'),
    two = add(i, 'B');
  issue(i, one);
  issue(i, two);
  const cid = await campaign(i, c);
  clock.at = new Date('2027-07-05T11:00:00+03:00');
  const sent = [];
  assert.equal((await c.tick(async (...x) => sent.push(x))).sent, 1);
  assert.equal(sent.length, 1);
  assert.equal((await c.tick(async (...x) => sent.push(x))).sent, 0);
  clock.at = new Date('2027-07-06T11:00:00+03:00');
  i.setRole(1, 2, null, key());
  assert.equal((await c.tick(async (...x) => sent.push(x))).sent, 0);
  i.setRole(1, 2, 'operator', key());
  receive(i, one);
  receive(i, two);
  assert.equal((await c.tick(async (...x) => sent.push(x))).sent, 0);
  assert.equal(i.get(1, one).status, 'stored');
});
test('Сбой доставки не меняет роутер, задача остаётся и имеет задержку повтора', async (t) => {
  const { i, c, clock, s } = setup(t),
    id = add(i);
  issue(i, id);
  await campaign(i, c);
  clock.at = new Date('2027-06-17T11:00:00+03:00');
  const before = i.get(1, id).version;
  assert.equal(
    (
      await c.tick(async () => {
        throw new Error('timeout');
      })
    ).failed,
    1,
  );
  assert.equal(i.get(1, id).version, before);
  assert.equal(s.get("SELECT count(*) AS n FROM reminders WHERE state='pending'").n, 1);
  let calls = 0;
  await c.tick(async () => calls++);
  assert.equal(calls, 0);
  clock.at = new Date(+clock.at + 60000);
  assert.equal((await c.tick(async () => calls++)).sent, 1);
  assert.equal(calls, 1);
});
test('Сохранённое расписание и отметки отправки переживают перезапуск', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'router-schedule-')),
    path = join(dir, 'db.sqlite');
  let s = new Store(path),
    now = new Date('2027-06-17T11:00:00+03:00'),
    i = new Inventory(s, { bootstrapAdminId: 1, now: () => now });
  i.setRole(1, 2, 'operator', key());
  const id = add(i);
  issue(i, id);
  let c = new Campaigns(i);
  await campaign(i, c);
  await c.tick(async () => {});
  s.close();
  s = new Store(path);
  t.after(() => {
    s.close();
    rmSync(dir, { recursive: true, force: true });
  });
  i = new Inventory(s, { now: () => now });
  c = new Campaigns(i);
  assert.equal((await c.tick(async () => assert.fail('Повторная отправка'))).sent, 0);
  assert.equal(c.list(1).length, 1);
});
