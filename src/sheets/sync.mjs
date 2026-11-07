import { assert, uuid, hash, equal, clone, normalizeSerial, contact } from '../util.mjs';
import {
  validateSheet,
  projection,
  mergeThree,
  decode,
  validateFlat,
  FIELDS,
  LINKED,
} from './schema.mjs';

export class Synchronizer {
  constructor(inventory, adapter) {
    this.i = inventory;
    this.s = inventory.s;
    this.a = adapter;
  }
  _project(r) {
    return projection(r, this.i._loan(r.id), this.i._loans(r.id).at(-1));
  }
  _digest(sheet) {
    return hash({
      headers: sheet.headers,
      rows: sheet.rows.map((r) => ({ row: r.row, values: r.values, formulas: r.formulas || [] })),
      sheetId: sheet.sheetId,
      title: sheet.title,
    });
  }
  async preview(actor) {
    this.i.authorize(actor, 'admin');
    const sheet = await this.a.read();
    this.i.authorize(actor, 'admin');
    validateSheet(sheet);
    const entries = [],
      seen = new Set(),
      ids = new Set(),
      invs = new Set();
    const routers = this.i.list(actor);
    for (const row of sheet.rows) {
      const v = row.values;
      if (!String(v.inventory || '').trim()) {
        if (
          Object.entries(v).some(
            ([k, x]) => k !== 'checked' && x !== '' && x !== null && x !== undefined,
          )
        )
          entries.push({
            type: 'error',
            row: row.row,
            message: 'Строка без инвентарного номера; требуется уточнение',
          });
        continue;
      }
      const inv = String(v.inventory).trim().toLocaleLowerCase('ru'),
        id = v.id || null;
      if (invs.has(inv) || (id && ids.has(id))) {
        entries.push({ type: 'error', row: row.row, message: 'Дубли ID или инвентарного номера' });
        continue;
      }
      invs.add(inv);
      if (id) ids.add(id);
      let r = routers.find((x) => x.id === id),
        sameInv = routers.find((x) => x.inventory.toLocaleLowerCase('ru') === inv);
      if (!r && sameInv) {
        const snap = this.s.get('SELECT data FROM snapshots WHERE router_id=?', sameInv.id);
        if (!id && snap && !JSON.parse(snap.data).id) r = sameInv;
        else {
          entries.push({
            type: 'conflict',
            routerId: sameInv.id,
            row: row.row,
            message: 'ID изменён или очищен: восстановите прежний router_id',
            identity: true,
          });
          seen.add(sameInv.id);
          continue;
        }
      }
      if (id && !r) {
        entries.push({
          type: 'error',
          row: row.row,
          message: 'Неизвестный router_id: новые строки должны иметь пустой ID',
        });
        continue;
      }
      if (r) seen.add(r.id);
      try {
        validateFlat(v);
        if (r && r.inventory !== String(v.inventory).trim()) {
          entries.push({
            type: 'conflict',
            routerId: r.id,
            row: row.row,
            message: 'Изменение постоянного инвентарного номера требует отдельного разбора',
            identity: true,
          });
          continue;
        }
        const decoded = decode(v);
        const macMatch = routers.find(
          (x) => x.id !== r?.id && decoded.mac && x.mac === decoded.mac,
        );
        const serialMatch = routers.find(
          (x) =>
            x.id !== r?.id &&
            normalizeSerial(decoded.serial) &&
            normalizeSerial(x.serial) === normalizeSerial(decoded.serial),
        );
        assert(!macMatch && !serialMatch, 'Возможный дубликат MAC или серийного номера');
        if (!r) {
          entries.push({
            type: 'new',
            row: row.row,
            values: clone(v),
            incomplete: this._incomplete(decoded),
          });
          continue;
        }
        const snap = this.s.get('SELECT data FROM snapshots WHERE router_id=?', r.id);
        if (!snap) {
          entries.push({
            type: 'conflict',
            routerId: r.id,
            row: row.row,
            message: 'Нет согласованного снимка; исходящая запись приостановлена',
            identity: true,
          });
          continue;
        }
        const base = JSON.parse(snap.data),
          bot = this._project(r),
          merge = mergeThree(base, v, bot);
        if (!equal(base.rawContact ?? '', v.rawContact)) {
          const parsed = contact(v.rawContact);
          for (const f of ['phone', 'telegram'])
            if (equal(base[f] ?? '', v[f] ?? '')) {
              merge.merged[f] = parsed[f] || '';
              if (!equal(merge.merged[f], bot[f]))
                merge.changes.push({
                  field: f,
                  before: base[f] ?? '',
                  sheet: merge.merged[f],
                  bot: bot[f],
                  derived: true,
                });
            }
        }
        const needs = this._transitionNeeds(r, merge.merged);
        const resume = Boolean(this.s.get('SELECT 1 FROM conflicts WHERE router_id=?', r.id));
        entries.push({
          type: merge.conflicts.length
            ? 'conflict'
            : needs
              ? 'review'
              : merge.changes.length || resume
                ? 'change'
                : 'same',
          routerId: r.id,
          row: row.row,
          version: r.version,
          values: clone(v),
          base,
          bot,
          ...merge,
          message:
            needs || (resume ? 'Расхождение устранено; исходящая отправка возобновится' : null),
        });
      } catch (e) {
        entries.push({ type: 'error', routerId: r?.id, row: row.row, message: e.message });
      }
    }
    // Any duplicate invalidates BOTH occurrences, never import the first arbitrarily.
    const duplicateInv = new Set(),
      duplicateIds = new Set(),
      counts = new Map(),
      icounts = new Map();
    for (const row of sheet.rows) {
      const v = row.values,
        k = String(v.inventory || '')
          .trim()
          .toLocaleLowerCase('ru');
      if (k) counts.set(k, (counts.get(k) || 0) + 1);
      if (v.id) icounts.set(v.id, (icounts.get(v.id) || 0) + 1);
    }
    for (const [k, n] of counts) if (n > 1) duplicateInv.add(k);
    for (const [k, n] of icounts) if (n > 1) duplicateIds.add(k);
    for (const e of entries) {
      const row = sheet.rows.find((x) => x.row === e.row);
      if (
        row &&
        (duplicateInv.has(
          String(row.values.inventory || '')
            .trim()
            .toLocaleLowerCase('ru'),
        ) ||
          duplicateIds.has(row.values.id))
      ) {
        e.type = 'error';
        e.message = 'Дубли ID / инвентарного номера: пропущены все спорные строки';
      }
    }
    for (const r of routers)
      if (!seen.has(r.id) && this.s.get('SELECT 1 FROM snapshots WHERE router_id=?', r.id))
        entries.push({
          type: 'missing',
          routerId: r.id,
          version: r.version,
          message: 'Строка исчезла; БД и история сохранены. Восстановите строку с прежним ID.',
        });
    const preview = {
      digest: this._digest(sheet),
      versions: Object.fromEntries(routers.map((r) => [r.id, r.version])),
      entries,
      at: this.i.now().toISOString(),
    };
    const id = this.s.draft(actor, 'sync', preview, this.i.now());
    return { id, ...preview };
  }
  _incomplete(d) {
    return [
      d.condition === 'needs_check' ? 'техническое состояние не проверено' : null,
      d.status === 'unknown' ? 'неизвестный статус' : null,
      ...(['issued', 'lost'].includes(d.status)
        ? [
            !d.issuedAt ? 'дата выдачи неизвестна' : null,
            !d.dueConfirmed || !d.dueAt ? 'срок не определён' : null,
            !d.recipient.name ? 'получатель неизвестен' : null,
            !d.recipient.contact?.raw ? 'нет контакта' : null,
            d.recipient.kind === 'unknown' ? 'тип получателя не подтверждён' : null,
            !d.recipient.admissionYear && !d.recipient.academicYear ? 'набор не определён' : null,
          ]
        : []),
    ].filter(Boolean);
  }
  _transitionNeeds(r, v) {
    const d = decode(v),
      l = this.i._loan(r.id);
    if (r.status === 'written_off' && d.status !== 'written_off')
      return 'Списанное оборудование требует отдельного решения администратора';
    if (r.status === 'lost' && d.status !== 'lost')
      return 'Обнаружение и физический приём оформите в карточке';
    if (d.status === 'lost' && r.status !== 'lost' && !l && d.recipient.name)
      return 'Уточните сведения о прежнем получателе до оформления новой утраты';
    if (d.status !== r.status) {
      if (
        d.status === 'issued' &&
        !(
          d.recipient.name &&
          d.recipient.kind !== 'unknown' &&
          d.place.name &&
          d.issuedAt &&
          d.dueAt &&
          d.dueConfirmed &&
          d.condition === 'working'
        )
      )
        return 'Для выдачи нужны подтверждённые получатель, место, дата, срок и исправность';
      if (d.status === 'stored' && l) return 'Возврат требует физического приёма через карточку';
      if (d.status === 'written_off') return 'Списание с причиной оформите в карточке';
      if (d.status === 'unknown' && l) return 'Нельзя потерять текущую выдачу при смене статуса';
    }
    if (
      l &&
      (d.recipient.name !== l.recipient.name ||
        (d.recipient.id && d.recipient.id !== l.recipient.id)) &&
      !(
        d.recipient.name &&
        d.recipient.kind !== 'unknown' &&
        d.issuedAt &&
        d.dueAt &&
        d.dueConfirmed
      )
    )
      return 'Смена получателя требует данных связанной передачи; прежний получатель сохраняется';
    return null;
  }
  async apply(actor, draftId, resolutions = {}) {
    this.i.authorize(actor, 'admin');
    const draft = this.s.getDraft(actor, draftId, this.i.now());
    assert(draft.kind === 'sync', 'Неверное подтверждение');
    const p = draft.data,
      sheet = await this.a.read();
    this.i.authorize(actor, 'admin');
    validateSheet(sheet);
    assert(
      this._digest(sheet) === p.digest,
      'Таблица изменилась после просмотра. Выполните /sync заново.',
      'stale',
    );
    return this.s.atomic(actor, 'sync:' + draftId, { draftId, resolutions }, (group) => {
      this.i.authorize(actor, 'admin');
      // Include additions/deletions in optimistic check, not just existing versions.
      const current = Object.fromEntries(this.i.list(actor).map((r) => [r.id, r.version]));
      assert(
        equal(current, p.versions),
        'База изменилась после просмотра. Выполните /sync заново.',
        'stale',
      );
      const result = { added: 0, updated: 0, unchanged: 0, skipped: 0, conflicts: 0 };
      const blockedGroups = new Set();
      for (const e of p.entries)
        if (
          ['error', 'review', 'missing', 'conflict'].includes(e.type) &&
          !resolutions[e.routerId]
        ) {
          const o =
            e.routerId && this.s.get('SELECT group_id FROM outbox WHERE router_id=?', e.routerId);
          if (o) blockedGroups.add(o.group_id);
        }
      for (const e of p.entries) {
        const pending =
          e.routerId && this.s.get('SELECT group_id FROM outbox WHERE router_id=?', e.routerId);
        if (
          ['error', 'review', 'missing'].includes(e.type) ||
          e.identity ||
          (pending && blockedGroups.has(pending.group_id))
        ) {
          if (e.routerId) this._block(e.routerId, e);
          result.skipped++;
          continue;
        }
        if (e.type === 'same') {
          const r = this.i._router(e.routerId),
            bot = this._project(r);
          if (equal(e.values, bot)) {
            this.s.run('DELETE FROM outbox WHERE router_id=?', r.id);
            this.s.run('DELETE FROM conflicts WHERE router_id=?', r.id);
          }
          result.unchanged++;
          continue;
        }
        let values = e.merged || e.values;
        if (e.type === 'conflict') {
          const resolution = resolutions[e.routerId];
          if (!resolution) {
            this._block(e.routerId, e);
            result.conflicts++;
            continue;
          }
          assert(
            ['sheet', 'bot', 'custom'].includes(resolution.source) && resolution.reason,
            'Для разбора нужен источник и причина',
          );
          values =
            resolution.source === 'sheet'
              ? { ...e.bot, ...e.values }
              : resolution.source === 'bot'
                ? e.bot
                : { ...e.merged, ...resolution.values };
          if (resolution.source === 'bot') {
            this.s.run(
              'INSERT OR REPLACE INTO snapshots VALUES(?,?)',
              e.routerId,
              JSON.stringify(e.values),
            );
            this.s.run('DELETE FROM conflicts WHERE router_id=?', e.routerId);
            this.s.run("UPDATE outbox SET state='pending' WHERE router_id=?", e.routerId);
            this.i._queue(this.i._router(e.routerId), group);
            this.s.event(
              actor,
              e.routerId,
              'Разрешение конфликта',
              e.values,
              values,
              resolution.reason,
            );
            result.updated++;
            continue;
          }
        }
        if (e.routerId)
          assert(
            !this._transitionNeeds(this.i._router(e.routerId), values),
            this._transitionNeeds(this.i._router(e.routerId), values) || 'Неполный переход',
          );
        this._import(actor, values, e, group, resolutions[e.routerId]?.reason);
        e.type === 'new' ? result.added++ : result.updated++;
      }
      this.s.set('last_sync_at', this.i.now().toISOString());
      return result;
    });
  }
  _block(id, details) {
    this.s.run('INSERT OR REPLACE INTO conflicts VALUES(?,?)', id, JSON.stringify(details));
    this.s.run("UPDATE outbox SET state='blocked' WHERE router_id=?", id);
  }
  _import(actor, v, entry, group, reason) {
    const d = decode(v, { initial: !entry.routerId });
    let r = entry.routerId ? this.i._router(entry.routerId) : this.i._new({ ...d, id: uuid() });
    const before = entry.routerId ? this.i._bundle(r) : null,
      old = this.i._loan(r.id),
      status = d.status;
    if (!entry.routerId) {
      r.status = 'stored';
      if (status === 'issued' || (status === 'lost' && d.recipient.name))
        this.i._issue(r, d, actor, { legacy: true, issuedAt: d.issuedAt });
    } else if (old) {
      if (
        d.recipient.name !== old.recipient.name ||
        (d.recipient.id && d.recipient.id !== old.recipient.id)
      ) {
        old.open = false;
        old.closedKind = 'sheet_transfer';
        old.closedAt = this.i.now().toISOString();
        this.i._putLoan(old);
        r.status = 'stored';
        const next = this.i._issue(r, d, actor, {
          legacy: true,
          issuedAt: d.issuedAt,
          linkedLoanId: old.id,
        });
        old.linkedLoanId = next.id;
        this.i._putLoan(old);
      } else {
        old.recipient = {
          ...old.recipient,
          ...d.recipient,
          id: d.recipient.id || old.recipient.id,
        };
        old.place = clone(d.place);
        old.dueAt = d.dueAt;
        old.dueConfirmed = d.dueConfirmed;
        old.issuedAt = d.issuedAt;
        old.assignedOperator = d.assignedOperator || old.assignedOperator;
        this.i._putLoan(old);
      }
    } else if (status === 'issued')
      this.i._issue(r, d, actor, { legacy: true, issuedAt: d.issuedAt });
    for (const f of [
      'inventory',
      'model',
      'serial',
      'mac',
      'condition',
      'vip',
      'vipComment',
      'note',
      'kit',
      'legacy',
    ])
      r[f] = clone(d[f]);
    r.status = status;
    r.storage = this.i._loan(r.id) ? null : clone(d.storage);
    if (status === 'lost' && before?.router.status !== 'lost')
      r.loss = {
        reason: reason || 'Импорт статуса из таблицы; обстоятельства уточнить',
        at: this.i.now().toISOString(),
        actor,
      };
    this.i._validate(r);
    // Parent row must exist before creating an inherited loan (deferred FK is used below).
    this.i._save(r, group);
    this.s.run('INSERT OR REPLACE INTO snapshots VALUES(?,?)', r.id, JSON.stringify(entry.values));
    this.s.run('DELETE FROM conflicts WHERE router_id=?', r.id);
    this.s.event(
      actor,
      r.id,
      'Импорт из таблицы',
      before,
      this.i._bundle(r),
      reason || 'Подтверждён импорт; автор исходной правки неизвестен',
    );
  }
  async flush() {
    const groups = this.s.all(
      'SELECT DISTINCT group_id FROM outbox WHERE next_at<=?',
      this.i.now().toISOString(),
    );
    let sent = 0,
      blocked = 0,
      failed = 0;
    for (const { group_id } of groups) {
      const jobs = this.s.all('SELECT * FROM outbox WHERE group_id=?', group_id);
      if (jobs.some((j) => j.state === 'blocked')) {
        blocked += jobs.length;
        continue;
      }
      try {
        const sheet = await this.a.read();
        validateSheet(sheet, { requireExtras: true });
        const writes = [];
        for (const j of jobs) {
          const r = this.i._router(j.router_id),
            snap = this.s.get('SELECT data FROM snapshots WHERE router_id=?', r.id),
            base = snap ? JSON.parse(snap.data) : null;
          let matches = sheet.rows.filter((row) => row.values.id === r.id);
          if (!matches.length && base && !base.id)
            matches = sheet.rows.filter(
              (row) => !row.values.id && row.values.inventory === r.inventory,
            );
          const byInv = sheet.rows.filter(
            (row) =>
              String(row.values.inventory).toLocaleLowerCase('ru') ===
              r.inventory.toLocaleLowerCase('ru'),
          );
          assert(
            matches.length <= 1 && byInv.length <= 1,
            'Дубли ID / инвентарного номера',
            'conflict',
          );
          const remote = matches[0];
          if (base) assert(remote, 'Строка удалена или ID изменён', 'conflict');
          else
            assert(
              !remote && !byInv.length,
              'Новая строка уже существует: выполните /sync',
              'conflict',
            );
          const bot = this._project(r);
          if (remote && base && !equal(remote.values, base) && !equal(remote.values, bot))
            throw Object.assign(new Error('Непринятые ручные правки: выполните /sync'), {
              code: 'conflict',
            });
          assert(r.version === j.version, 'Версия исходящего задания устарела', 'stale');
          writes.push({
            routerId: r.id,
            version: r.version,
            values: bot,
            row: remote?.row || null,
            previous: remote?.values || null,
            formulas: remote?.formulas || [],
          });
        }
        // Adapter rereads again immediately before the atomic Sheets batch write.
        await this.a.write(writes, sheet);
        const readback = await this.a.read();
        validateSheet(readback, { requireExtras: true });
        for (const w of writes)
          assert(
            readback.rows.some((r) => r.values.id === w.routerId && equal(r.values, w.values)),
            'Не удалось подтвердить запись таблицы',
            'conflict',
          );
        this.s.tx(() => {
          for (const w of writes) {
            this.s.run(
              'INSERT OR REPLACE INTO snapshots VALUES(?,?)',
              w.routerId,
              JSON.stringify(w.values),
            );
            this.s.run('DELETE FROM outbox WHERE router_id=? AND version=?', w.routerId, w.version);
            this.s.run('DELETE FROM conflicts WHERE router_id=?', w.routerId);
          }
        });
        sent += writes.length;
      } catch (e) {
        const conflict = ['conflict', 'schema'].includes(e.code),
          safe = conflict ? e.message : 'Таблица недоступна; будет повтор';
        this.s.tx(() => {
          for (const j of jobs) {
            const delay = Math.min(3600000, 1000 * 2 ** Math.min(j.attempts + 1, 12));
            this.s.run(
              'UPDATE outbox SET attempts=attempts+1,next_at=?,last_error=?,state=? WHERE router_id=?',
              new Date(+this.i.now() + delay).toISOString(),
              safe,
              conflict ? 'blocked' : 'pending',
              j.router_id,
            );
            if (conflict) this._block(j.router_id, { type: 'conflict', message: safe });
          }
        });
        conflict ? (blocked += jobs.length) : (failed += jobs.length);
      }
    }
    return { sent, blocked, failed };
  }
}
