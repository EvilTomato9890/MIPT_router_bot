import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Worker } from 'node:worker_threads';
import { Store } from '../src/db.mjs';
import { Inventory, place } from '../src/service.mjs';
import { csvCell, normalizeMac, isoDate, overdue } from '../src/util.mjs';
import { setup, key, add, issue, receive, person } from './helpers.mjs';

test('01. Права, отзыв, последняя роль администратора, bootstrap однократен', (t) => {
  const { s, i } = setup(t),
    id = add(i);
  for (const fn of [
    () => i.get(99, id),
    () => i.list(99),
    () => i.stats(99),
    () => i.history(99, id),
    () => i.execute(3, key(), { kind: 'lost', routerId: id, reason: 'test' }),
  ])
    assert.throws(fn, /Нет доступа/);
  i.setRole(1, 2, null, key());
  assert.throws(
    () => i.execute(2, key(), { kind: 'lost', routerId: id, reason: 'test' }),
    /Нет доступа/,
  );
  assert.throws(() => i.setRole(1, 1, null, key()), /последнего/);
  i.setRole(1, 4, 'admin', key());
  i.setRole(4, 1, null, key());
  new Inventory(s, { bootstrapAdminId: 1 });
  assert.throws(() => i.get(1, id), /Нет доступа/);
  assert.equal(i.get(4, id).id, id);
});
test('02. Уникальные инвентарные, MAC, серийные; заглушки', (t) => {
  const { i } = setup(t);
  add(i, 'X', { mac: 'aa-bb-cc-00-11-22', serial: 'AB- 12' });
  assert.throws(() => add(i, 'x'), /уже существует/);
  assert.throws(() => add(i, 'Y', { mac: 'aabb.cc00.1122' }), /дубликат/);
  assert.throws(() => add(i, 'Z', { serial: 'ab12' }), /дубликат/);
  add(i, 'N1', { serial: 'нет', mac: '-' });
  add(i, 'N2', { serial: 'нет', mac: 'нет' });
  assert.equal(i.list(1).length, 3);
});
test('03. Повтор операции и устаревшее подтверждение не создают выдач', (t) => {
  const { i } = setup(t),
    id = add(i),
    cmd = {
      kind: 'issue',
      routerId: id,
      recipient: person(),
      place: place('101', 'room'),
      dueAt: '2027-07-01',
      expected: { [id]: 1 },
    },
    k = key();
  const first = i.execute(2, k, cmd);
  assert.deepEqual(i.execute(2, k, cmd), first);
  assert.equal(i.loans(1, id).length, 1);
  assert.throws(() => i.execute(2, key(), cmd), /Карточка изменилась/);
  assert.throws(() => i.execute(2, k, { ...cmd, dueAt: '2027-08-01' }), /Ключ уже/);
  i.setRole(1, 2, null, key());
  assert.throws(() => i.execute(2, k, cmd), /Нет доступа/);
});
test('04. Полный цикл с продлением и повторной выдачей сохраняет историю', (t) => {
  const { i } = setup(t),
    id = add(i);
  issue(i, id);
  i.execute(1, key(), {
    kind: 'extend',
    routerId: id,
    dueAt: '2027-08-01',
    reason: 'Тестовое продление',
  });
  receive(i, id);
  issue(i, id, { recipient: person('Второй тестовый получатель') });
  const loans = i.loans(1, id);
  assert.equal(loans.length, 2);
  assert.equal(loans[0].dueAt, '2027-08-01');
  assert.equal(loans[0].open, false);
  assert.ok(loans[0].returnedAt);
  assert.equal(loans[1].open, true);
  assert.equal(i.history(1, id).length, 5);
});
test('05. Утрата, обнаружение и приём сохраняют прежнего получателя', (t) => {
  const { i, clock } = setup(t),
    id = add(i);
  issue(i, id);
  clock.at = new Date('2027-07-10T10:00:00+03:00');
  assert.equal(overdue(i.get(1, id).loan, clock.at), true);
  assert.equal(i.get(1, id).status, 'issued');
  i.execute(2, key(), { kind: 'lost', routerId: id, reason: 'Не найден при обходе' });
  assert.equal(i.get(1, id).loan.recipient.name, 'Тестовый получатель');
  assert.throws(() => issue(i, id), /только свободный/);
  i.execute(2, key(), { kind: 'found', routerId: id, reason: 'Найден у получателя' });
  assert.equal(i.get(1, id).status, 'lost');
  i.execute(1, key(), {
    kind: 'resolve_found',
    routerId: id,
    resolution: 'holder',
    reason: 'Остаётся у прежнего получателя',
  });
  assert.equal(i.get(1, id).status, 'issued');
  receive(i, id);
  assert.equal(i.get(1, id).status, 'stored');
  assert.ok(i.get(1, id).loss);
});
test('06. Непроверенный / повреждённый возврат и отсутствующий БП', (t) => {
  const { i } = setup(t),
    id = add(i);
  issue(i, id);
  receive(i, id, {
    condition: 'needs_check',
    kit: { power: false, cable: true, other: 'БП не сдан' },
  });
  assert.throws(() => issue(i, id), /исправный/);
  assert.equal(i.list(1, { report: 'missing_kit' }).length, 1);
  assert.equal(i.get(1, id).status, 'stored');
  i.execute(2, key(), {
    kind: 'condition',
    routerId: id,
    condition: 'broken',
    reason: 'Повреждён',
  });
  assert.throws(() => issue(i, id), /исправный/);
});
test('08. Атомарные замена и передача, обе стороны связаны', (t) => {
  const { i } = setup(t),
    old = add(i, 'OLD'),
    replacement = add(i, 'NEW'),
    busy = add(i, 'BUSY');
  issue(i, old);
  issue(i, busy);
  const v = i.get(1, old).version;
  const cmd = {
    kind: 'replace',
    routerId: old,
    newRouterId: busy,
    inventory: 'OLD',
    physical: true,
    storage: place('Склад', 'warehouse'),
    condition: 'broken',
    kit: { power: true, cable: true, other: '' },
    reason: 'Тест замены',
  };
  assert.throws(() => i.execute(2, key(), cmd), /недоступен/);
  assert.equal(i.get(1, old).version, v);
  assert.equal(i.get(1, old).loan.open, true);
  i.execute(2, key(), { ...cmd, newRouterId: replacement });
  assert.equal(i.get(1, old).status, 'stored');
  assert.equal(i.get(1, replacement).status, 'issued');
  assert.equal(i.loans(1, old)[0].linkedLoanId, i.get(1, replacement).loan.id);
  const prev = i.get(1, replacement).loan.id;
  assert.throws(
    () =>
      i.execute(2, key(), {
        kind: 'transfer',
        routerId: replacement,
        recipient: person('Следующий'),
        place: place('102', 'room'),
        reason: 'Без срока',
      }),
    /срок/,
  );
  assert.equal(i.get(1, replacement).loan.id, prev);
  i.execute(2, key(), {
    kind: 'transfer',
    routerId: replacement,
    recipient: person('Следующий'),
    place: place('102', 'room'),
    dueAt: '2027-07-02',
    reason: 'Передача',
  });
  assert.equal(i.loans(1, replacement).length, 2);
  assert.equal(i.get(1, replacement).loan.linkedLoanId, prev);
});
test('12. Исправление дописывает событие, зависимые операции запрещают откат', (t) => {
  const { s, i } = setup(t),
    id = add(i),
    bad = issue(i, id);
  i.execute(1, key(), {
    kind: 'correct',
    routerId: id,
    eventId: bad.eventId,
    reason: 'Ошибочно выбрали устройство',
  });
  assert.equal(i.get(1, id).status, 'stored');
  assert.equal(i.loans(1, id)[0].closedKind, 'correction');
  assert.equal(i.history(1, id).at(-1).correction_of, bad.eventId);
  const next = issue(i, id);
  i.execute(2, key(), { kind: 'note', routerId: id, note: 'Зависимое событие' });
  assert.throws(
    () =>
      i.execute(1, key(), {
        kind: 'correct',
        routerId: id,
        eventId: next.eventId,
        reason: 'Ошибка',
      }),
    /зависимые/,
  );
  assert.throws(() => s.run('DELETE FROM events'), /Журнал/);
  assert.throws(() => s.run("UPDATE events SET action='x'"), /Журнал/);
});
test('Даты, локальный день, отчёты и CSV не смешивают признаки', (t) => {
  const { i, clock } = setup(t);
  assert.throws(() => isoDate('2027-02-30'), /Дата/);
  assert.equal(normalizeMac('aa bb cc dd ee ff'), 'AA:BB:CC:DD:EE:FF');
  const id = add(i, '=SUM(1)');
  i.execute(1, key(), { kind: 'vip', routerId: id, vip: true, reason: 'Тестовая отметка' });
  assert.equal(i.stats(1).physical, 1);
  assert.equal(i.stats(1).vip, 1);
  assert.ok(csvCell(' \t=1+1').startsWith('"\''));
  clock.at = new Date('2027-06-01T22:00:00Z');
  issue(i, id);
  assert.equal(i.get(1, id).loan.issuedAt, '2027-06-02');
});
test('Политика хранения очищает контакты отдельно от движения, журнал не копирует контакты', (t) => {
  const { s, i, clock } = setup(t),
    id = add(i);
  issue(i, id, {
    recipient: {
      ...person(),
      contact: { raw: '+70000000000', phone: '+70000000000', telegram: 'test_user', userId: 12345 },
    },
  });
  receive(i, id);
  assert.ok(!JSON.stringify(i.history(1, id)).includes('+70000000000'));
  clock.at = new Date('2029-01-01T10:00:00Z');
  assert.equal(i.purgeContacts(1, 365, key()).count, 1);
  assert.equal(i.loans(1, id)[0].recipient.contact, null);
  assert.equal(i.loans(1, id).length, 1);
});
test('09. Данные и очередь переживают перезапуск; резервная копия целостна', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'router-test-'));
  const path = join(dir, 'db.sqlite');
  const s = new Store(path),
    i = new Inventory(s, { bootstrapAdminId: 1 }),
    id = add(i);
  await s.backup(join(dir, 'copy.sqlite'));
  s.close();
  const restored = new Store(path);
  t.after(() => {
    restored.close();
    rmSync(dir, { recursive: true, force: true });
  });
  assert.equal(new Inventory(restored).get(1, id).inventory, 'TEST-01');
  assert.equal(restored.get('SELECT count(*) AS n FROM outbox').n, 1);
  const backup = new Store(join(dir, 'copy.sqlite'));
  assert.equal(backup.get('PRAGMA integrity_check').integrity_check, 'ok');
  backup.close();
});
test('03b. Два реальных соединения SQLite одновременно выдают только одному оператору', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'router-race-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, 'race.sqlite');
  const s = new Store(path),
    i = new Inventory(s, { bootstrapAdminId: 1 });
  i.setRole(1, 2, 'operator', key());
  i.setRole(1, 4, 'operator', key());
  const id = add(i);
  s.close();
  const gate = new SharedArrayBuffer(4),
    workers = [2, 4].map(
      (actor) =>
        new Worker(new URL('./workers/issue-race.mjs', import.meta.url), {
          workerData: { path, id, actor, gate },
        }),
    );
  const exits = workers.map((w) => new Promise((res) => w.once('exit', res)));
  const ready = workers.map(
    (w) =>
      new Promise((res, rej) => {
        w.once('error', rej);
        w.once('message', res);
      }),
  );
  await Promise.all(ready);
  const result = workers.map(
    (w) =>
      new Promise((res, rej) => {
        w.once('error', rej);
        w.once('message', res);
      }),
  );
  Atomics.store(new Int32Array(gate), 0, 1);
  Atomics.notify(new Int32Array(gate), 0, 2);
  const outcomes = await Promise.all(result);
  await Promise.all(exits);
  assert.equal(outcomes.filter((x) => x.ok).length, 1);
  const check = new Store(path);
  assert.equal(check.get('SELECT count(*) AS n FROM loans WHERE open=1').n, 1);
  check.close();
});
