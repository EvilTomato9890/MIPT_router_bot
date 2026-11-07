import { assert, uuid, text, isoDate, clone, dayDiff, localClock, overdue, hash } from './util.mjs';

export class Campaigns {
  constructor(inventory, { time = '10:00', offsets = [14, 7, 1, 0] } = {}) {
    this.i = inventory;
    this.s = inventory.s;
    this.time = time;
    this.offsets = [...offsets].sort((a, b) => b - a);
  }
  _get(id) {
    const row = this.s.get('SELECT * FROM campaigns WHERE id=?', id);
    assert(row, 'Кампания не найдена');
    return { ...JSON.parse(row.data), version: row.version };
  }
  _save(c) {
    c.version = (c.version || 0) + 1;
    this.s.run(
      'INSERT INTO campaigns VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=excluded.version',
      c.id,
      JSON.stringify(c),
      c.version,
    );
  }
  list(actor) {
    this.i.authorize(actor);
    return this.s.all('SELECT id FROM campaigns').map((r) => this._get(r.id));
  }
  save(actor, key, data) {
    this.i.authorize(actor, 'admin');
    return this.s.atomic(actor, key, { campaign: data }, () => {
      this.i.authorize(actor, 'admin');
      const old = data.id ? this._get(data.id) : null,
        c = { ...(old || { id: uuid(), active: false }), ...clone(data) };
      c.name = text(c.name || '');
      assert(c.name, 'Название кампании обязательно');
      assert(c.admissionYear || c.academicYear, 'Укажите набор или учебный год');
      isoDate(c.startsAt);
      isoDate(c.deadline);
      assert(c.deadline >= c.startsAt, 'Срок раньше начала сбора');
      assert(
        Array.isArray(c.responsibleIds) && c.responsibleIds.length > 0,
        'Назначьте ответственных',
      );
      for (const id of c.responsibleIds) this.i.authorize(id, 'operator');
      for (const f of ['location', 'hours', 'instruction'])
        assert(text(c[f] || ''), `Укажите ${f}`);
      if (old && data.expectedVersion !== undefined)
        assert(old.version === data.expectedVersion, 'Кампания изменилась', 'stale');
      this._save(c);
      this.s.event(actor, c.id, 'Настройка кампании', old, c);
      return c;
    });
  }
  _eligible(l, c) {
    return (
      l.recipient.kind === 'person' &&
      l.place?.type === 'room' &&
      l.place?.certainty === 'confirmed' &&
      ((c.admissionYear && Number(l.recipient.admissionYear) === Number(c.admissionYear)) ||
        (c.academicYear && l.recipient.academicYear === c.academicYear))
    );
  }
  preview(actor, id) {
    this.i.authorize(actor, 'admin');
    const c = this._get(id),
      included = [],
      clarify = [];
    for (const r of this.i.list(actor))
      if (r.loan) {
        const l = r.loan;
        if (this._eligible(l, c)) included.push({ router: r, loan: l });
        else if (
          l.recipient.kind === 'unknown' ||
          (l.recipient.kind === 'person' && !l.recipient.admissionYear && !l.recipient.academicYear)
        )
          clarify.push({ router: r, loan: l });
      }
    return { campaign: c, included, clarify };
  }
  launch(actor, key, id, expected) {
    this.i.authorize(actor, 'admin');
    return this.s.atomic(actor, key, { launch: id, expected }, () => {
      this.i.authorize(actor, 'admin');
      const p = this.preview(actor, id),
        c = p.campaign;
      assert(c.version === expected, 'Кампания изменилась', 'stale');
      for (const { loan } of p.included)
        this.s.run(
          'INSERT OR IGNORE INTO campaign_members VALUES(?,?,?)',
          id,
          loan.id,
          JSON.stringify({ source: 'cohort', at: this.i.now().toISOString() }),
        );
      c.active = true;
      this._save(c);
      this.s.event(actor, id, 'Запуск кампании', null, {
        count: p.included.length,
        clarify: p.clarify.length,
      });
      return { included: p.included.length, clarify: p.clarify.length };
    });
  }
  member(actor, key, id, loanId, data) {
    const isAdmin = ['include', 'exclude', 'assign', 'priority'].includes(data.action);
    this.i.authorize(actor, isAdmin ? 'admin' : 'operator');
    return this.s.atomic(actor, key, { id, loanId, data }, (group) => {
      this.i.authorize(actor, isAdmin ? 'admin' : 'operator');
      this._get(id);
      const row = this.s.get('SELECT * FROM loans WHERE id=?', loanId);
      assert(row, 'Выдача не найдена');
      const l = JSON.parse(row.data),
        prev = this.s.get(
          'SELECT data FROM campaign_members WHERE campaign_id=? AND loan_id=?',
          id,
          loanId,
        );
      assert(l.open, 'Выдача уже закрыта; обновите очередь');
      assert(prev || data.action === 'include', 'Выдача не включена в кампанию');
      const m = prev ? JSON.parse(prev.data) : { source: 'manual' };
      assert(data.reason, 'Укажите примечание');
      if (['include', 'exclude'].includes(data.action)) {
        m.excluded = data.action === 'exclude';
        m.reason = data.reason;
      } else if (data.action === 'assign') {
        this.i.authorize(data.operatorId, 'operator');
        l.assignedOperator = data.operatorId;
        this.i._putLoan(l);
        this.i._save(this.i._router(l.routerId), group);
      } else if (data.action === 'priority') {
        assert(Number.isFinite(data.priority), 'Приоритет должен быть числом');
        m.priority = data.priority;
      } else {
        assert(
          ['not_home', 'contacted', 'new_date', 'problem', 'next'].includes(data.action),
          'Приём оформляется физическим возвратом',
        );
        m.lastAction = { ...data, actor, at: this.i.now().toISOString() };
      }
      this.s.run(
        'INSERT OR REPLACE INTO campaign_members VALUES(?,?,?)',
        id,
        loanId,
        JSON.stringify(m),
      );
      this.s.event(
        actor,
        id,
        'Действие в очереди',
        prev ? JSON.parse(prev.data) : null,
        { loanId, ...m },
        data.reason,
      );
      return m;
    });
  }
  queue(actor, id, { includeReturned = false, group, place, operator } = {}) {
    this.i.authorize(actor);
    const c = this._get(id),
      today = localClock(this.i.now()).day;
    if (c.active)
      for (const row of this.s.all('SELECT data FROM loans WHERE open=1')) {
        const l = JSON.parse(row.data);
        if (this._eligible(l, c))
          this.s.run(
            'INSERT OR IGNORE INTO campaign_members VALUES(?,?,?)',
            id,
            l.id,
            JSON.stringify({ source: 'cohort', at: this.i.now().toISOString() }),
          );
      }
    const rows = this.s
      .all('SELECT loan_id,data FROM campaign_members WHERE campaign_id=?', id)
      .map((row) => {
        const l = JSON.parse(this.s.get('SELECT data FROM loans WHERE id=?', row.loan_id).data),
          r = this.i._router(l.routerId),
          m = JSON.parse(row.data);
        const excluded = Boolean(m.excluded || (l.exception && l.exception.reviewAt >= today));
        const due = l.dueConfirmed && l.dueAt && l.dueAt > c.deadline ? l.dueAt : c.deadline;
        return {
          router: r,
          loan: l,
          member: m,
          due,
          excluded,
          returned: !l.open,
          overdue: l.open && due < today,
          lost: r.status === 'lost',
        };
      })
      .filter(
        (x) =>
          (includeReturned || !x.returned) &&
          (!group || x.loan.recipient.group === group) &&
          (!place || x.loan.place.name.includes(place)) &&
          (!operator || x.loan.assignedOperator === operator),
      );
    const natural = (a, b) =>
      String(a || '').localeCompare(String(b || ''), 'ru', { numeric: true });
    return rows.sort(
      (a, b) =>
        Number(a.excluded) - Number(b.excluded) ||
        Number(b.overdue) - Number(a.overdue) ||
        (a.member.priority || 0) - (b.member.priority || 0) ||
        natural(a.due, b.due) ||
        natural(a.loan.place?.floor, b.loan.place?.floor) ||
        natural(a.loan.place?.section, b.loan.place?.section) ||
        natural(a.loan.place?.name, b.loan.place?.name) ||
        natural(a.router.inventory, b.router.inventory),
    );
  }
  report(actor, id) {
    const rows = this.queue(actor, id, { includeReturned: true });
    return {
      plan: rows.length,
      returned: rows.filter((x) => x.returned).length,
      remaining: rows.filter((x) => !x.returned && !x.excluded).length,
      overdue: rows.filter((x) => x.overdue && !x.excluded).length,
      excluded: rows.filter((x) => x.excluded).length,
      lost: rows.filter((x) => x.lost && !x.returned).length,
    };
  }
  async tick(send) {
    const now = this.i.now(),
      { day, time } = localClock(now);
    if (time < this.time) return { sent: 0 };
    let sent = 0,
      failed = 0;
    for (const row of this.s.all('SELECT id FROM campaigns')) {
      const c = this._get(row.id);
      if (!c.active || day < c.startsAt) continue;
      const admin = this.s.get("SELECT user_id FROM roles WHERE role='admin'")?.user_id;
      if (!admin) continue;
      const queue = this.queue(admin, c.id).filter((x) => !x.excluded),
        candidate = queue
          .map((x) => {
            const diff = dayDiff(x.due, day),
              stage =
                diff < 0 ? 'overdue:' + day : [...this.offsets].reverse().find((n) => diff <= n);
            return { ...x, stage };
          })
          .filter((x) => x.stage !== undefined);
      const signature = hash(candidate.map((x) => [x.loan.id, x.due, x.stage]));
      const liveKeys = [];
      for (const target of c.responsibleIds) {
        try {
          this.i.authorize(target, 'operator');
        } catch {
          continue;
        }
        if (!candidate.length) continue;
        const key = c.id + ':' + target + ':' + signature;
        liveKeys.push(key);
        const daily = this.s.setting(`notice:${c.id}:${target}`);
        if (daily === day) continue;
        this.s.run(
          "INSERT INTO reminders VALUES(?,?,?,?, 'pending',0,NULL) ON CONFLICT(key) DO UPDATE SET state=CASE WHEN reminders.state='cancelled' THEN 'pending' ELSE reminders.state END",
          key,
          c.id,
          target,
          now.toISOString(),
        );
      }
      for (const pending of this.s.all(
        "SELECT * FROM reminders WHERE campaign_id=? AND state='pending'",
        c.id,
      )) {
        if (!liveKeys.includes(pending.key)) {
          this.s.run("UPDATE reminders SET state='cancelled' WHERE key=?", pending.key);
          continue;
        }
        if (pending.due_at > now.toISOString()) continue;
        try {
          this.i.authorize(pending.target, 'operator');
          const summary = `Сбор: ${c.name}\nОсталось: ${queue.length}; просрочено: ${queue.filter((x) => x.overdue).length}; утрата: ${queue.filter((x) => x.lost).length}.\n${c.location}, ${c.hours}\n${c.instruction}\nОткройте «Сбор» для очереди.`;
          await send(pending.target, summary);
          this.s.tx(() => {
            this.s.run(
              "UPDATE reminders SET state='sent',due_at=? WHERE key=?",
              now.toISOString(),
              pending.key,
            );
            this.s.set(`notice:${c.id}:${pending.target}`, day);
          });
          sent++;
        } catch {
          const retry = new Date(
            +now + Math.min(3600000, 30000 * 2 ** Math.min(pending.attempts, 7)),
          ).toISOString();
          this.s.run(
            'UPDATE reminders SET attempts=attempts+1,due_at=?,last_error=? WHERE key=?',
            retry,
            'Доставка не подтверждена; повтор при следующем запуске планировщика',
            pending.key,
          );
          failed++;
        }
      }
    }
    return { sent, failed };
  }
}
