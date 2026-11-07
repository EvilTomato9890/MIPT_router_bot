import {
  assert,
  uuid,
  text,
  optional,
  normalizeSerial,
  normalizeMac,
  isoDate,
  clone,
  hash,
  overdue,
  STATUS,
  CONDITION,
  localClock,
} from './util.mjs';

export const ADMIN_ACTIONS = new Set([
  'vip',
  'extend',
  'write_off',
  'exception',
  'correct',
  'resolve_found',
]);
export const KIT = { power: null, cable: null, other: '' };
export const place = (
  name = '',
  type = 'other',
  certainty = 'confirmed',
  floor = '',
  section = '',
) => ({ name, type, certainty, floor, section });

export class Inventory {
  constructor(store, { now = () => new Date(), bootstrapAdminId } = {}) {
    this.s = store;
    this.now = now;
    // Bootstrap only once: restarting must never restore revoked rights.
    if (bootstrapAdminId && !store.setting('bootstrapped'))
      store.tx(() => {
        assert(
          Number.isSafeInteger(bootstrapAdminId) && bootstrapAdminId > 0,
          'Неверный ID администратора',
        );
        store.run('INSERT OR IGNORE INTO roles VALUES(?,?)', bootstrapAdminId, 'admin');
        store.set('bootstrapped', true);
      });
  }
  authorize(actor, min = 'observer') {
    assert(Number.isSafeInteger(actor) && actor > 0, 'Нет доступа', 'forbidden');
    const role = this.s.get('SELECT role FROM roles WHERE user_id=?', actor)?.role;
    assert(
      role &&
        { observer: 0, operator: 1, admin: 2 }[role] >= { observer: 0, operator: 1, admin: 2 }[min],
      'Нет доступа',
      'forbidden',
    );
    return role;
  }
  setRole(actor, target, role, key) {
    this.authorize(actor, 'admin');
    assert(Number.isSafeInteger(target) && target > 0, 'Укажите постоянный числовой Telegram ID');
    assert([null, 'admin', 'operator', 'observer'].includes(role), 'Неизвестная роль');
    return this.s.atomic(actor, key, { target, role }, () => {
      this.authorize(actor, 'admin');
      const old = this.s.get('SELECT role FROM roles WHERE user_id=?', target)?.role;
      assert(
        !(
          old === 'admin' &&
          role !== 'admin' &&
          this.s.get("SELECT count(*) AS n FROM roles WHERE role='admin'").n <= 1
        ),
        'Нельзя удалить последнего администратора',
      );
      this.s.run('DELETE FROM roles WHERE user_id=?', target);
      if (role) this.s.run('INSERT INTO roles VALUES(?,?)', target, role);
      this.s.run('DELETE FROM sessions WHERE user_id=?', target);
      this.s.event(actor, String(target), 'Изменение доступа', old, role);
      return { target, role };
    });
  }
  roles(actor) {
    this.authorize(actor, 'admin');
    return this.s.all('SELECT * FROM roles ORDER BY user_id');
  }
  _router(id) {
    const row = this.s.get('SELECT data FROM routers WHERE id=? OR inventory=?', id, id);
    assert(row, 'Роутер не найден');
    return JSON.parse(row.data);
  }
  _loan(routerId) {
    const row = this.s.get('SELECT data FROM loans WHERE router_id=? AND open=1', routerId);
    return row ? JSON.parse(row.data) : null;
  }
  _loans(routerId) {
    return this.s
      .all('SELECT data FROM loans WHERE router_id=? ORDER BY rowid', routerId)
      .map((r) => JSON.parse(r.data));
  }
  get(actor, id) {
    this.authorize(actor);
    const r = this._router(id);
    return { ...r, loan: this._loan(r.id) };
  }
  history(actor, id) {
    this.authorize(actor);
    const r = this._router(id);
    return this.s.all('SELECT * FROM events WHERE object_id=? ORDER BY rowid', r.id);
  }
  loans(actor, id) {
    this.authorize(actor);
    return this._loans(this._router(id).id);
  }
  list(actor, filter = {}) {
    this.authorize(actor);
    let rows = this.s.all('SELECT data FROM routers ORDER BY inventory COLLATE NOCASE').map((x) => {
      const r = JSON.parse(x.data);
      return { ...r, loan: this._loan(r.id) };
    });
    const q = String(filter.q || '').toLocaleLowerCase('ru');
    rows = rows.filter((r) => {
      const l = r.loan,
        recipient = l?.recipient;
      if (
        q &&
        ![
          r.inventory,
          r.serial,
          r.mac,
          r.mac?.replaceAll(':', ''),
          r.model,
          r.storage?.name,
          recipient?.name,
          recipient?.group,
          l?.place?.name,
        ].some((v) =>
          String(v || '')
            .toLocaleLowerCase('ru')
            .includes(q),
        )
      )
        return false;
      if (filter.status && r.status !== filter.status) return false;
      if (filter.condition && r.condition !== filter.condition) return false;
      if (filter.vip !== undefined && r.vip !== filter.vip) return false;
      if (
        filter.year &&
        String(recipient?.admissionYear || recipient?.academicYear) !== String(filter.year)
      )
        return false;
      if (filter.group && recipient?.group !== filter.group) return false;
      if (filter.place && !(l?.place?.name || r.storage?.name || '').includes(filter.place))
        return false;
      if (filter.operator && l?.assignedOperator !== Number(filter.operator)) return false;
      switch (filter.report) {
        case 'available':
          return r.status === 'stored' && r.condition === 'working';
        case 'overdue':
          return overdue(l, this.now());
        case 'incomplete':
          return (
            r.status === 'unknown' ||
            Boolean(
              l &&
              (!l.dueConfirmed ||
                !l.dueAt ||
                !recipient?.name ||
                recipient?.kind === 'unknown' ||
                (recipient?.kind === 'person' &&
                  !recipient.admissionYear &&
                  !recipient.academicYear)),
            )
          );
        case 'undefined_due':
          return Boolean(l && (!l.dueConfirmed || !l.dueAt));
        case 'unknown_holder':
          return Boolean(l && (!recipient?.name || recipient?.kind === 'unknown'));
        case 'unknown_cohort':
          return Boolean(
            l &&
            recipient?.kind !== 'organization' &&
            !recipient?.admissionYear &&
            !recipient?.academicYear,
          );
        case 'check':
          return r.condition !== 'working';
        case 'missing_kit':
          return r.kit?.power === false || r.kit?.cable === false;
        case 'pending':
          return Boolean(this.s.get('SELECT 1 FROM outbox WHERE router_id=?', r.id));
        case 'conflicts':
          return Boolean(this.s.get('SELECT 1 FROM conflicts WHERE router_id=?', r.id));
        default:
          return true;
      }
    });
    return rows.sort((a, b) => a.inventory.localeCompare(b.inventory, 'ru', { numeric: true }));
  }
  warnings(actor, recipient, usePlace) {
    return this.list(actor, { status: 'issued' })
      .filter((r) => {
        const p = r.loan.recipient;
        return (
          (recipient.id && p.id === recipient.id) ||
          (p.contact?.userId && p.contact.userId === recipient.contact?.userId) ||
          (p.name && p.name === recipient.name) ||
          (usePlace?.name && usePlace.name === r.loan.place?.name)
        );
      })
      .map(
        (r) =>
          `${r.inventory}: ${r.loan.recipient.name || 'получатель не определён'}, ${r.loan.place?.name || 'место не определено'}`,
      );
  }
  _save(r, group) {
    const other = this.s.all(
        'SELECT id,inventory,serial_norm,mac_norm FROM routers WHERE id<>?',
        r.id,
      ),
      invKey = r.inventory.normalize('NFKC').toLocaleUpperCase('ru'),
      serial = normalizeSerial(r.serial),
      mac = normalizeMac(r.mac);
    assert(
      !other.some((x) => x.inventory.normalize('NFKC').toLocaleUpperCase('ru') === invKey),
      'Инвентарный номер уже существует',
    );
    for (const [field, value] of [
      ['serial_norm', serial],
      ['mac_norm', mac],
    ]) {
      const same = value && other.find((x) => x[field] === value);
      assert(
        !same,
        `Возможный дубликат ${field === 'mac_norm' ? 'MAC' : 'серийного номера'}: ${same?.inventory}`,
      );
    }
    r.version = (r.version || 0) + 1;
    r.updatedAt = this.now().toISOString();
    this.s.run(
      `INSERT INTO routers VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET inventory=excluded.inventory,serial_norm=excluded.serial_norm,mac_norm=excluded.mac_norm,status=excluded.status,condition=excluded.condition,version=excluded.version,data=excluded.data`,
      r.id,
      r.inventory,
      normalizeSerial(r.serial),
      normalizeMac(r.mac),
      r.status,
      r.condition,
      r.version,
      JSON.stringify(r),
    );
    this._queue(r, group);
  }
  _queue(r, group) {
    const previous = this.s.get('SELECT group_id FROM outbox WHERE router_id=?', r.id);
    if (previous)
      this.s.run('UPDATE outbox SET group_id=? WHERE group_id=?', group, previous.group_id);
    const at = this.now().toISOString();
    this.s.run(
      `INSERT INTO outbox VALUES(?,?,?,'pending',0,?,?,NULL) ON CONFLICT(router_id) DO UPDATE SET version=excluded.version,group_id=excluded.group_id,state='pending',next_at=excluded.next_at`,
      r.id,
      group,
      r.version,
      at,
      at,
    );
  }
  _putLoan(l) {
    this.s.run(
      'INSERT INTO loans VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET open=excluded.open,data=excluded.data',
      l.id,
      l.routerId,
      l.open ? 1 : 0,
      JSON.stringify(l),
    );
  }
  _new(data) {
    const at = this.now().toISOString(),
      inv = text(data.inventory || '', 100);
    assert(inv, 'Укажите инвентарный номер');
    return {
      id: data.id || uuid(),
      inventory: inv,
      model: optional(data.model),
      serial: optional(data.serial),
      mac: normalizeMac(data.mac),
      status: 'stored',
      condition: data.condition || 'needs_check',
      vip: false,
      vipComment: '',
      storage: clone(data.storage || place('', 'other', 'absent')),
      kit: clone(data.kit || KIT),
      note: text(data.note || ''),
      legacy: {},
      createdAt: at,
      updatedAt: at,
      version: 0,
    };
  }
  _recipient(p, legacy = false) {
    assert(
      p && ['person', 'organization', ...(legacy ? ['unknown'] : [])].includes(p.kind),
      'Выберите тип получателя',
    );
    const r = clone(p);
    r.id = r.id || uuid();
    r.name = text(r.name || '');
    assert(legacy || r.name, 'Укажите получателя');
    if (r.kind === 'organization') {
      assert(!r.admissionYear && !r.academicYear, 'Для организации набор студентов неприменим');
    }
    if (r.admissionYear)
      assert(
        Number.isInteger(Number(r.admissionYear)) &&
          Number(r.admissionYear) >= 1990 &&
          Number(r.admissionYear) <= 2200,
        'Неверный год поступления',
      );
    if (r.contact?.userId)
      assert(
        Number.isSafeInteger(r.contact.userId) && r.contact.userId > 0,
        'Неверный подтверждённый Telegram ID',
      );
    return r;
  }
  _validate(r) {
    assert(STATUS[r.status] && CONDITION[r.condition], 'Неизвестный статус или состояние');
    assert(r.inventory && r.id, 'Пустой номер или ID');
    normalizeMac(r.mac);
    for (const p of [r.storage])
      if (p)
        assert(
          ['room', 'common', 'warehouse', 'other'].includes(p.type) &&
            ['absent', 'unknown', 'legacy', 'confirmed'].includes(p.certainty),
          'Неверное место',
        );
    for (const k of ['power', 'cable'])
      assert([null, true, false].includes(r.kit?.[k]), 'Комплект: да, нет или неизвестно');
    const l = this._loan(r.id);
    assert(!(r.status === 'issued' && !l), 'Выданный роутер должен иметь действующую выдачу');
    assert(
      !(['stored', 'unknown', 'written_off'].includes(r.status) && l),
      'Статус несовместим с действующей выдачей',
    );
  }
  _issue(r, d, actor, { legacy = false, issuedAt, linkedLoanId } = {}) {
    assert(
      r.status === 'stored' && (legacy || r.condition === 'working'),
      'Выдать можно только свободный исправный роутер',
    );
    assert(!this._loan(r.id), 'У роутера уже есть действующая выдача');
    const recipient = this._recipient(d.recipient, legacy);
    assert(legacy || d.place?.name, 'Укажите место использования');
    const l = {
      id: uuid(),
      routerId: r.id,
      open: true,
      recipient,
      place: clone(d.place || place('', 'other', 'absent')),
      issuedAt: legacy ? (issuedAt ?? null) : isoDate(d.issuedAt || localClock(this.now()).day),
      dueAt: isoDate(d.dueAt, true),
      dueConfirmed: legacy ? Boolean(d.dueConfirmed) : true,
      issuer: legacy ? null : actor,
      kitOut: clone(r.kit),
      conditionOut: legacy ? null : r.condition,
      returnedAt: null,
      receiver: null,
      kitIn: null,
      conditionIn: null,
      closedKind: null,
      linkedLoanId: linkedLoanId || null,
      assignedOperator: d.assignedOperator || actor,
      priority: 0,
      exception: null,
      incomplete: legacy ? ['legacy'] : [],
    };
    assert(legacy || l.dueAt, 'Укажите подтверждённый срок');
    if (l.dueAt && l.issuedAt) assert(l.dueAt >= l.issuedAt, 'Срок раньше даты выдачи');
    this._putLoan(l);
    r.status = 'issued';
    r.storage = null;
    return l;
  }
  _return(r, d, actor, { legacy = false, closedKind = 'return' } = {}) {
    const l = this._loan(r.id);
    assert(l, 'Нет действующей выдачи');
    assert(legacy || d.physical === true, 'Подтвердите физический приём роутера');
    assert(legacy || d.inventory === r.inventory, 'Сверьте инвентарный номер');
    assert(legacy || d.storage?.name, 'Укажите место хранения');
    l.open = false;
    l.returnedAt = legacy
      ? isoDate(d.returnedAt, true)
      : isoDate(d.returnedAt || localClock(this.now()).day);
    l.receiver = actor;
    l.kitIn = clone(d.kit || KIT);
    l.conditionIn = d.condition || 'needs_check';
    l.closedKind = closedKind;
    this._putLoan(l);
    r.status = 'stored';
    r.condition = l.conditionIn;
    r.kit = clone(l.kitIn);
    r.storage = clone(d.storage || place('', 'other', 'unknown'));
    r.legacy.owner = '';
    r.legacy.contact = null;
    return l;
  }
  execute(actor, key, command) {
    const min = ADMIN_ACTIONS.has(command.kind) ? 'admin' : 'operator';
    this.authorize(actor, min);
    return this.s.atomic(actor, key, command, (group) => {
      this.authorize(actor, min);
      const d = clone(command),
        at = this.now().toISOString();
      for (const [id, version] of Object.entries(d.expected || {}))
        assert(
          this._router(id).version === version,
          'Карточка изменилась. Откройте новое подтверждение.',
          'stale',
        );
      let r = d.kind === 'add' ? this._new(d) : this._router(d.routerId),
        other,
        before = this._bundle(r),
        extraBefore;
      let reason = d.reason ? text(d.reason) : null,
        correctionOf = null;
      switch (d.kind) {
        case 'add':
          assert(!this.s.get('SELECT 1 FROM routers WHERE id=?', r.id), 'ID уже существует');
          break;
        case 'issue':
          this._issue(r, d, actor);
          break;
        case 'return':
          this._return(r, d, actor);
          break;
        case 'extend': {
          assert(reason, 'Укажите причину продления');
          const l = this._loan(r.id);
          assert(l, 'Нет действующей выдачи');
          const due = isoDate(d.dueAt);
          assert(!l.dueAt || due > l.dueAt, 'Новый срок должен быть позже предыдущего');
          l.dueAt = due;
          l.dueConfirmed = true;
          this._putLoan(l);
          break;
        }
        case 'transfer': {
          assert(reason, 'Укажите причину передачи');
          assert(
            r.status === 'issued' && r.condition === 'working',
            'Передавать можно исправный выданный роутер',
          );
          const l = this._loan(r.id);
          l.open = false;
          l.closedKind = 'transfer';
          l.closedAt = at;
          l.receiver = actor;
          this._putLoan(l);
          r.status = 'stored';
          const next = this._issue(r, d, actor, {
            legacy: true,
            issuedAt: isoDate(d.issuedAt || at.slice(0, 10)),
            linkedLoanId: l.id,
          });
          next.recipient = this._recipient(d.recipient);
          assert(next.dueAt && d.place?.name, 'Для передачи нужны срок и место');
          next.dueConfirmed = true;
          next.issuer = actor;
          next.incomplete = [];
          next.kitOut = clone(d.kit || r.kit);
          next.conditionOut = r.condition;
          this._putLoan(next);
          l.linkedLoanId = next.id;
          this._putLoan(l);
          break;
        }
        case 'replace': {
          assert(reason, 'Укажите причину замены');
          other = this._router(d.newRouterId);
          assert(other.id !== r.id, 'Нужен другой роутер');
          extraBefore = this._bundle(other);
          const old = this._loan(r.id);
          assert(old, 'Нет действующей выдачи');
          assert(
            other.status === 'stored' && other.condition === 'working' && !this._loan(other.id),
            'Новый роутер недоступен',
          );
          const closed = this._return(r, d, actor, { closedKind: 'replacement' });
          const next = this._issue(
            other,
            { ...d, recipient: old.recipient, place: old.place, dueAt: old.dueAt || d.dueAt },
            actor,
            { linkedLoanId: closed.id },
          );
          closed.linkedLoanId = next.id;
          this._putLoan(closed);
          break;
        }
        case 'move': {
          assert(d.place?.name, 'Укажите место');
          const l = this._loan(r.id);
          if (l) {
            l.place = clone(d.place);
            this._putLoan(l);
          } else r.storage = clone(d.place);
          break;
        }
        case 'lost':
          assert(reason, 'Укажите обстоятельства утраты');
          assert(
            ['issued', 'stored', 'unknown'].includes(r.status),
            'Утрата уже отмечена или оборудование списано',
          );
          r.status = 'lost';
          r.loss = { reason, at, actor };
          break;
        case 'found':
          assert(r.status === 'lost', 'Роутер не отмечен утерянным');
          assert(reason, 'Укажите, где и у кого обнаружен');
          r.found = { reason, at, actor };
          break;
        case 'resolve_found': {
          assert(r.status === 'lost' && r.found, 'Сначала зафиксируйте обнаружение');
          assert(reason, 'Укажите решение по утрате');
          if (d.resolution === 'holder') {
            assert(this._loan(r.id), 'Нет прежней выдачи');
            r.status = 'issued';
          } else if (this._loan(r.id)) this._return(r, d, actor);
          else {
            assert(
              d.physical === true && d.inventory === r.inventory && d.storage?.name,
              'Нужны физический приём, номер и место',
            );
            r.status = 'stored';
            r.condition = d.condition || 'needs_check';
            r.storage = clone(d.storage);
            r.kit = clone(d.kit || KIT);
          }
          break;
        }
        case 'condition':
          assert(reason, 'Укажите результат проверки / причину');
          assert(CONDITION[d.condition], 'Неизвестное состояние');
          r.condition = d.condition;
          if (d.kit) r.kit = clone(d.kit);
          break;
        case 'write_off': {
          assert(reason, 'Укажите причину списания');
          const l = this._loan(r.id);
          if (l) {
            assert(
              d.resolveOutstanding === true,
              'Отдельно подтвердите решение по незакрытой выдаче',
            );
            l.open = false;
            l.closedKind = 'written_off';
            l.closedAt = at;
            this._putLoan(l);
          }
          r.status = 'written_off';
          break;
        }
        case 'vip':
          assert(reason, 'Укажите смысл отметки VIP');
          assert(typeof d.vip === 'boolean', 'Выберите VIP');
          r.vip = d.vip;
          r.vipComment = reason;
          break;
        case 'exception': {
          assert(reason, 'Укажите причину исключения');
          const l = this._loan(r.id);
          assert(l, 'Нет выдачи');
          l.exception =
            d.enabled === false ? null : { reason, reviewAt: isoDate(d.reviewAt), actor };
          this._putLoan(l);
          break;
        }
        case 'note':
          r.note = text(d.note || '');
          break;
        case 'correct': {
          assert(reason, 'Укажите причину исправления');
          const event = this.s.get(
            'SELECT * FROM events WHERE id=? AND object_id=?',
            d.eventId,
            r.id,
          );
          assert(event, 'Исходная операция не найдена');
          const latest = this.s.get(
            'SELECT id FROM events WHERE object_id=? ORDER BY rowid DESC LIMIT 1',
            r.id,
          );
          assert(
            latest?.id === event.id,
            'Есть зависимые события: требуется новое предметное действие, слепой откат запрещён',
          );
          assert(
            event.action !== 'add' &&
              event.action !== 'replace' &&
              event.action !== 'Импорт из таблицы',
            'Связанную операцию исправляйте предметным действием',
          );
          const prior = JSON.parse(event.before_json);
          assert(prior?.router, 'Нет предыдущего состояния');
          // No deletion: mark newly opened loans as corrected, restore earlier loan state.
          const previousIds = new Set(prior.loans.map((l) => l.id));
          for (const l of this._loans(r.id))
            if (!previousIds.has(l.id)) {
              l.open = false;
              l.closedKind = 'correction';
              l.closedAt = at;
              this._putLoan(l);
            }
          for (const l of prior.loans) {
            const current = this.s.get('SELECT data FROM loans WHERE id=?', l.id);
            const saved = current ? JSON.parse(current.data) : {};
            if (l.recipient?.contact === undefined && saved.recipient?.contact)
              l.recipient.contact = saved.recipient.contact;
            this._putLoan(l);
          }
          const version = r.version;
          r = { ...prior.router, version };
          correctionOf = event.id;
          break;
        }
        default:
          assert(false, 'Неизвестная операция');
      }
      this._validate(r);
      this._save(r, group);
      const eventId = this.s.event(
        actor,
        r.id,
        d.kind,
        before,
        this._bundle(r),
        reason,
        correctionOf,
        at,
      );
      if (other) {
        this._validate(other);
        this._save(other, group);
        this.s.event(actor, other.id, d.kind, extraBefore, this._bundle(other), reason, null, at);
      }
      return { routerId: r.id, version: r.version, eventId, sheets: 'pending' };
    });
  }
  _bundle(r) {
    return { router: clone(r), loans: r.id ? this._loans(r.id) : [] };
  }
  purgeContacts(actor, days, key) {
    this.authorize(actor, 'admin');
    assert(Number.isInteger(days) && days > 0, 'Срок хранения должен быть положительным');
    return this.s.atomic(actor, key, { purge: days }, () => {
      this.authorize(actor, 'admin');
      let n = 0;
      const cutoff = new Date(+this.now() - days * 86400000).toISOString().slice(0, 10);
      for (const row of this.s.all('SELECT data FROM loans WHERE open=0')) {
        const l = JSON.parse(row.data),
          date = l.returnedAt || l.closedAt;
        if (date && date.slice(0, 10) < cutoff && l.recipient?.contact) {
          l.recipient.contact = null;
          l.contactsPurgedAt = this.now().toISOString();
          this._putLoan(l);
          n++;
        }
      }
      this.s.event(actor, 'privacy', 'Удаление контактов', null, { count: n });
      return { count: n };
    });
  }
  stats(actor) {
    const rows = this.list(actor);
    return {
      physical: rows.length,
      statuses: Object.fromEntries(
        Object.keys(STATUS).map((s) => [s, rows.filter((r) => r.status === s).length]),
      ),
      vip: rows.filter((r) => r.vip).length,
      loans: this.s.get('SELECT count(*) AS n FROM loans').n,
    };
  }
  loanReport(actor, from, to) {
    this.authorize(actor);
    isoDate(from);
    isoDate(to);
    assert(from <= to, 'Начало позже конца периода');
    const loans = this.s.all('SELECT data FROM loans').map((r) => JSON.parse(r.data)),
      known = loans.filter((l) => l.issuedAt && l.issuedAt >= from && l.issuedAt <= to);
    return {
      from,
      to,
      loans: known.length,
      physical: new Set(known.map((l) => l.routerId)).size,
      unknownDates: loans.filter((l) => !l.issuedAt).length,
    };
  }
}
