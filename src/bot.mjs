import {
  assert,
  DomainError,
  STATUS,
  CONDITION,
  csvCell,
  overdue,
  localClock,
  uuid,
} from './util.mjs';
import { ADMIN_ACTIONS } from './service.mjs';
import { fields, command, LABELS } from './dialogs.mjs';
import { LINKED, CONTACT_FIELDS } from './sheets/schema.mjs';
import { fieldLabel, valueLabel } from './ru.mjs';

const button = (text, callback_data) => ({ text, callback_data });
const keyboard = (rows) => ({ inline_keyboard: rows });
const rowsOf = (items, size = 2) =>
  Array.from({ length: Math.ceil(items.length / size) }, (_, i) =>
    items.slice(i * size, (i + 1) * size),
  );
const display = (v) => (v && typeof v === 'object' ? JSON.stringify(v) : valueLabel(v));
const READ_WIZARDS = new Set(['filters', 'loan_report', 'queue_filters']);
const ADMIN_WIZARDS = new Set([
  'role',
  'campaign',
  'campaign_edit',
  'campaign_include',
  'resolve',
  'resolve_custom',
  ...ADMIN_ACTIONS,
]);

export class Bot {
  constructor(
    inventory,
    telegram,
    { sync = null, colors = null, campaigns, mode = 'disabled', provision = null } = {},
  ) {
    this.i = inventory;
    this.s = inventory.s;
    this.t = telegram;
    this.sync = sync;
    this.colors = colors;
    this.c = campaigns;
    this.mode = mode;
    this.provision = provision;
  }
  async send(actor, text, buttons) {
    this.i.authorize(actor);
    return this.t.send(actor, text, buttons ? keyboard(buttons) : undefined);
  }
  _session(actor) {
    const r = this.s.get('SELECT data FROM sessions WHERE user_id=?', actor);
    return r ? JSON.parse(r.data) : null;
  }
  _saveSession(actor, data) {
    this.s.run(
      'INSERT OR REPLACE INTO sessions VALUES(?,?)',
      actor,
      JSON.stringify({ ...data, updatedAt: this.i.now().toISOString() }),
    );
  }
  _clear(actor) {
    this.s.run('DELETE FROM sessions WHERE user_id=?', actor);
  }
  async handle(update) {
    const cb = update.callback_query,
      m = cb?.message || update.message,
      actor = (cb?.from || m?.from)?.id;
    if (!m || !actor) return;
    if (m.chat?.type !== 'private' || m.chat.id !== actor) {
      if (cb) await this.t.answer(cb.id, 'Работайте с ботом в личном чате');
      return;
    }
    try {
      this.i.authorize(actor);
      if (this.s.get('SELECT 1 FROM processed_updates WHERE id=?', update.update_id)) {
        if (cb) await this.t.answer(cb.id, 'Уже обработано');
        return;
      }
      this.s.run('INSERT INTO processed_updates VALUES(?)', update.update_id);
      if (cb) {
        await this.t.answer(cb.id);
        await this.callback(actor, cb.data || '');
      } else await this.message(actor, String(m.text || ''), update.update_id);
    } catch (e) {
      const safe =
        e instanceof DomainError
          ? e.message
          : 'Не удалось выполнить запрос. Учёт сохранён; повторно откройте карточку.';
      if (e.code === 'forbidden') {
        this._clear(actor);
        if (cb) await this.t.answer(cb.id, 'Нет доступа').catch(() => {});
        await this.t.send(actor, `Нет доступа. Ваш Telegram ID: ${actor}`).catch(() => {});
      } else await this.t.send(actor, safe).catch(() => {});
    }
  }
  async menu(actor) {
    const role = this.i.authorize(actor),
      buttons = [
        button('Найти роутер', 'search'),
        button('Отчёты', 'reports'),
        button('Сбор', 'campaigns'),
      ];
    if (role !== 'observer')
      buttons.push(
        button('Добавить', 'start:add'),
        button('Выдать', 'select:issue'),
        button('Принять', 'select:return'),
      );
    if (role === 'admin') buttons.push(button('Настройки', 'settings'));
    return this.send(
      actor,
      `Учёт роутеров · общежитие № 7\nРоль: ${{ admin: 'Администратор', operator: 'Оператор', observer: 'Наблюдатель' }[role]}\n${this.mode === 'memory' ? 'ДЕМОНСТРАЦИЯ: Google не подключён.' : this.mode === 'disabled' ? 'Google не подключён; операции сохраняются в SQLite.' : 'Обновления Google Sheets проверяются при синхронизации; очередь — в отчётах.'}`,
      rowsOf(buttons),
    );
  }
  async message(actor, message, updateId) {
    if (['/start', '/menu', '/cancel', 'Отмена'].includes(message)) {
      this._clear(actor);
      return this.menu(actor);
    }
    if (message === '/help')
      return this.send(
        actor,
        'Меню: /menu\nПоиск: /find текст\nСписок: /list\nОтчёты: /reports\nЭкспорт: /export\nСбор: /collection\nАдминистратор: /settings, /sync, /repair_colors, /provision\nВ диалогах: «Назад», «Отмена». Изменения происходят только после итогового подтверждения.',
        [[button('Меню', 'menu')]],
      );
    if (message === '/sync') return this.syncPreview(actor);
    if (message === '/repair_colors') return this.colorPreview(actor);
    if (message === '/settings') return this.settings(actor);
    if (message === '/provision') {
      this.i.authorize(actor, 'admin');
      assert(this.provision, 'Подключение служебных колонок недоступно в этом режиме');
      const id = this.s.draft(actor, 'provision', {}, this.i.now());
      return this.send(
        actor,
        'Подключить согласованную схему O:AO листа «7ка»? Будут добавлены только заголовки служебных полей и недостающие колонки в конце сетки. A:J, K:N и «2ка» сохраняются. При занятых служебных ячейках операция остановится. Требуется SHEETS_MAPPING_APPROVED=true.',
        [[button('Подключить', 'confirm:' + id), button('Отмена', 'menu')]],
      );
    }
    if (message === '/reports') return this.reports(actor);
    if (message === '/collection') return this.campaignList(actor);
    if (message === '/export') return this.export(actor);
    if (message === '/list') return this.showList(actor, {});
    if (message.startsWith('/find ')) return this.showList(actor, { q: message.slice(6).trim() });
    const session = this._session(actor);
    if (!session) return this.menu(actor);
    assert(
      new Date(session.updatedAt).getTime() + 1800000 > +this.i.now(),
      'Диалог устарел. Начните заново.',
    );
    if (session.mode === 'search') {
      this._clear(actor);
      return this.showList(actor, { q: message });
    }
    if (session.mode === 'select') {
      this.i.authorize(actor, 'operator');
      const r = this.i.get(actor, message.trim());
      this._clear(actor);
      return this.start(actor, session.kind, { routerId: r.id, expected: { [r.id]: r.version } });
    }
    if (session.mode === 'wizard') return this.answerWizard(actor, message);
    return this.menu(actor);
  }
  async callback(actor, data) {
    const [action, a, b, c] = data.split(':');
    if (action === 'menu') {
      this._clear(actor);
      return this.menu(actor);
    }
    if (action === 'search') {
      this._saveSession(actor, { mode: 'search' });
      return this.send(actor, 'Введите номер, MAC, модель, ФИО, группу или место.', [
        [button('Отмена', 'menu')],
      ]);
    }
    if (action === 'select') {
      this.i.authorize(actor, 'operator');
      this._saveSession(actor, { mode: 'select', kind: a });
      return this.send(actor, 'Введите инвентарный номер.', [[button('Отмена', 'menu')]]);
    }
    if (action === 'start') return this.start(actor, a);
    if (action === 'act') {
      const r = this.i.get(actor, b);
      return this.start(actor, a, { routerId: r.id, expected: { [r.id]: r.version } });
    }
    if (action === 'back') return this.back(actor);
    if (action === 'answer') {
      const session = this._session(actor);
      assert(session?.mode === 'wizard', 'Диалог устарел');
      assert(session.nonce === a && session.step === Number(b), 'Кнопка от другого шага устарела');
      return this.answerWizard(actor, c);
    }
    if (action === 'confirm') return this.confirm(actor, a);
    if (action === 'card') return this.card(actor, a);
    if (action === 'hist') return this.history(actor, a, Number(b || 0));
    if (action === 'list') {
      const d = this.s.getDraft(actor, a, this.i.now());
      assert(d.kind === 'list', 'Неверный список');
      return this.showList(actor, d.data, Number(b || 0), a);
    }
    if (action === 'reports') return this.reports(actor);
    if (action === 'report')
      return this.showList(
        actor,
        a === 'vip' ? { vip: true } : a in STATUS ? { status: a } : { report: a },
      );
    if (action === 'settings') return this.settings(actor);
    if (action === 'sync') return this.syncPreview(actor);
    if (action === 'colors') return this.colorPreview(actor);
    if (action === 'sv') return this.syncPage(actor, a, Number(b || 0));
    if (action === 'sa') {
      this.i.authorize(actor, 'admin');
      const d = this.s.getDraft(actor, a, this.i.now());
      assert(d.kind === 'sync', 'Неверное подтверждение');
      const result = await this.sync.apply(actor, a, d.data.resolutions || {});
      return this.send(
        actor,
        `Импорт завершён. Добавлено: ${result.added}; обновлено: ${result.updated}; без изменений: ${result.unchanged}; пропущено: ${result.skipped}; конфликтов: ${result.conflicts}.\nПропущенные расхождения остаются в отчёте.`,
        [[button('Меню', 'menu')]],
      );
    }
    if (action === 'sr') return this.resolvePage(actor, a, Number(b));
    if (action === 'sx') {
      this.i.authorize(actor, 'admin');
      const draft = this.s.getDraft(actor, a, this.i.now()),
        e = draft.data.entries[Number(b)];
      assert(
        e && e.type === 'conflict' && !e.identity,
        'Конфликт требует ручного восстановления идентификатора',
      );
      const customFields =
        c === 'custom'
          ? [
              ...new Set(
                e.conflicts.flatMap((f) =>
                  f === 'связанные поля выдачи'
                    ? [...LINKED]
                    : f === 'связанные контактные поля'
                      ? [...CONTACT_FIELDS]
                      : [f],
                ),
              ),
            ]
          : [];
      return this.start(actor, c === 'custom' ? 'resolve_custom' : 'resolve', {
        syncDraft: a,
        entryIndex: Number(b),
        source: c,
        customFields,
      });
    }
    if (action === 'campaigns') return this.campaignList(actor);
    if (action === 'cp') return this.campaignPreview(actor, a);
    if (action === 'ce') {
      this.i.authorize(actor, 'admin');
      return this.start(actor, 'campaign_edit', { campaignId: a });
    }
    if (action === 'cin') {
      this.i.authorize(actor, 'admin');
      return this.start(actor, 'campaign_include', { campaignId: a });
    }
    if (action === 'queue') return this.queue(actor, a, Number(b || 0));
    if (action === 'qfilter') return this.start(actor, 'queue_filters', { campaignId: a });
    if (action === 'qf') {
      const d = this.s.getDraft(actor, a, this.i.now());
      assert(d.kind === 'queue_nav', 'Просмотр очереди устарел');
      return this.queue(actor, d.data.campaignId, Number(b || 0), d.data.filter, a);
    }
    if (action === 'qa') {
      this.i.authorize(actor, 'operator');
      const d = this.s.getDraft(actor, a, this.i.now());
      assert(d.kind === 'queue', 'Очередь устарела');
      const loanId = d.data.loanIds[Number(b)],
        x = this.c.queue(actor, d.data.campaignId).find((x) => x.loan.id === loanId);
      assert(x, 'Очередь изменилась; выдача закрыта или исключена');
      return this.start(actor, 'queue_action', {
        campaignId: d.data.campaignId,
        loanId: x.loan.id,
      });
    }
    if (action === 'include')
      return this.start(actor, 'queue_action', { campaignId: a, loanId: b, action: 'include' });
    assert(false, 'Кнопка устарела. Откройте меню.');
  }
  async start(actor, kind, data = {}) {
    this.i.authorize(
      actor,
      READ_WIZARDS.has(kind) ? 'observer' : ADMIN_WIZARDS.has(kind) ? 'admin' : 'operator',
    );
    this._saveSession(actor, {
      mode: 'wizard',
      kind,
      data,
      step: 0,
      visited: [],
      nonce: uuid().slice(0, 8),
    });
    if (['return', 'replace', 'resolve_found'].includes(kind))
      await this.send(
        actor,
        'При приёме: сверить номер → проверить комплект → осмотреть и проверить работоспособность → подтвердить физический приём → указать место хранения. Без проверки выбирайте «Нужна проверка».',
      );
    return this.ask(actor);
  }
  _field(session) {
    return fields(session.kind, session.data)[session.step];
  }
  async ask(actor) {
    const session = this._session(actor);
    assert(session?.mode === 'wizard', 'Начните диалог заново');
    const all = fields(session.kind, session.data);
    while (
      session.step < all.length &&
      all[session.step].when &&
      !all[session.step].when(session.data)
    )
      session.step++;
    this._saveSession(actor, session);
    if (session.step >= all.length) return this.finish(actor, session);
    const f = all[session.step],
      buttons = f.choices
        ? rowsOf(
            f.choices.map((x) =>
              button(x.label, `answer:${session.nonce}:${session.step}:${x.value}`),
            ),
          )
        : [];
    buttons.push([button('Назад', 'back'), button('Отмена', 'menu')]);
    return this.send(
      actor,
      `${LABELS[session.kind]} · шаг ${session.visited.length + 1}\n${f.prompt}`,
      buttons,
    );
  }
  async answerWizard(actor, value) {
    const session = this._session(actor);
    assert(session?.mode === 'wizard', 'Начните диалог заново');
    this.i.authorize(
      actor,
      READ_WIZARDS.has(session.kind)
        ? 'observer'
        : ADMIN_WIZARDS.has(session.kind)
          ? 'admin'
          : 'operator',
    );
    const f = this._field(session);
    assert(f, 'Диалог уже завершён');
    const parsed = f.parse(value);
    session.data[f.key] = parsed;
    session.visited.push(session.step);
    session.step++;
    this._saveSession(actor, session);
    return this.ask(actor);
  }
  async back(actor) {
    const s = this._session(actor);
    assert(s?.mode === 'wizard', 'Начните диалог заново');
    if (!s.visited.length) {
      this._clear(actor);
      return this.menu(actor);
    }
    s.step = s.visited.pop();
    const all = fields(s.kind, s.data);
    for (let j = s.step; j < all.length; j++) delete s.data[all[j].key];
    this._saveSession(actor, s);
    return this.ask(actor);
  }
  async finish(actor, session) {
    const { kind, data } = session;
    if (kind === 'queue_filters') {
      const filter = Object.fromEntries(
        Object.entries(data).filter(([k, v]) => k !== 'campaignId' && v !== '' && v !== null),
      );
      this._clear(actor);
      return this.queue(actor, data.campaignId, 0, filter);
    }
    if (kind === 'loan_report') {
      const r = this.i.loanReport(actor, data.from, data.to);
      this._clear(actor);
      return this.send(
        actor,
        `Выдачи ${r.from} — ${r.to}: ${r.loans}. Разных физических роутеров в этих выдачах: ${r.physical}. Выдач с неизвестной датой, вне подсчёта периода: ${r.unknownDates}.`,
        [[button('Отчёты', 'reports')]],
      );
    }
    if (kind === 'filters') {
      const filter = Object.fromEntries(
        Object.entries(data).filter(([k, v]) => v !== '' && v !== null && v !== 'all'),
      );
      if (filter.vip) filter.vip = filter.vip === 'yes';
      this._clear(actor);
      return this.showList(actor, filter);
    }
    let prepared = { kind, data };
    if (
      ![
        'role',
        'campaign',
        'campaign_edit',
        'campaign_include',
        'queue_action',
        'resolve',
        'resolve_custom',
      ].includes(kind)
    )
      prepared = { kind: 'operation', command: command(kind, data, this.i, actor) };
    if (kind === 'campaign_include') {
      const r = this.i.get(actor, data.loanInventory);
      assert(r.loan, 'У роутера нет действующей выдачи');
      prepared = {
        kind: 'queue_action',
        data: {
          campaignId: data.campaignId,
          loanId: r.loan.id,
          action: 'include',
          reason: data.reason,
        },
      };
    }
    if (kind === 'campaign' || kind === 'campaign_edit') {
      const c =
        kind === 'campaign_edit' ? this.c.list(actor).find((c) => c.id === data.campaignId) : {};
      assert(c, 'Кампания не найдена');
      prepared = {
        kind: 'campaign',
        data: {
          ...c,
          ...data,
          responsibleIds: data.responsibleIds.split(',').map((x) => Number(x.trim())),
          ...(kind === 'campaign_edit' ? { id: c.id, expectedVersion: c.version } : {}),
        },
      };
    }
    if (
      kind === 'queue_action' &&
      ['include', 'exclude', 'assign', 'priority'].includes(data.action)
    )
      this.i.authorize(actor, 'admin');
    let warning = '';
    if (['issue', 'transfer'].includes(kind))
      warning = this.i
        .warnings(actor, prepared.command.recipient, prepared.command.place)
        .join('\n');
    const id = this.s.draft(actor, 'confirm', prepared, this.i.now());
    this._clear(actor);
    const lines = Object.entries(data)
      .filter(
        ([k]) =>
          ![
            'expected',
            'routerId',
            'customFields',
            'syncDraft',
            'entryIndex',
            'campaignId',
            'loanId',
          ].includes(k),
      )
      .map(([k, v]) => `${fieldLabel(k)}: ${display(v)}`);
    if (data.routerId) lines.unshift('Роутер: ' + this.i.get(actor, data.routerId).inventory);
    await this.sendChunks(
      actor,
      `${LABELS[kind]} — проверьте итог\n${lines.join('\n')}${warning ? '\nВозможные другие выдачи тому же человеку / в то же место (совпадение имени не доказывает личность):\n' + warning : ''}`,
    );
    return this.send(actor, 'Применить показанные изменения?', [
      [button('Подтвердить', 'confirm:' + id), button('Отмена', 'menu')],
    ]);
  }
  async confirm(actor, id) {
    const d = this.s.getDraft(actor, id, this.i.now());
    if (d.kind === 'colors') {
      this.i.authorize(actor, 'admin');
      const r = await this.colors.apply(actor, id);
      return this.send(
        actor,
        `Раскраска ${r.changed ? 'восстановлена' : 'уже верна'}. Наследуемых правил Checked сохранено: ${r.checkedPreserved}.`,
        [[button('Настройки', 'settings')]],
      );
    }
    if (d.kind === 'provision') {
      this.i.authorize(actor, 'admin');
      assert(this.provision, 'Недоступно');
      await this.provision();
      return this.send(
        actor,
        'Служебные заголовки проверены. Для импорта: /sync. Реальный импорт требует отдельного просмотра и подтверждения.',
      );
    }
    if (d.kind === 'launch') {
      this.i.authorize(actor, 'admin');
      const c = this.c._get(d.data.id),
        nowVersions = Object.fromEntries(this.i.list(actor).map((r) => [r.id, r.version]));
      assert(
        JSON.stringify(nowVersions) === JSON.stringify(d.data.versions),
        'Список выдач изменился; повторите просмотр кампании',
      );
      const r = this.c.launch(actor, 'launch:' + id, c.id, d.data.version);
      return this.send(
        actor,
        `Кампания запущена. Включено: ${r.included}, требуют уточнения: ${r.clarify}.`,
      );
    }
    assert(d.kind === 'confirm', 'Неверное подтверждение');
    const p = d.data;
    if (p.kind === 'operation') {
      const result = this.i.execute(actor, 'dialog:' + id, p.command);
      if (this.sync) await this.sync.flush();
      const pending = this.s.get('SELECT state FROM outbox WHERE router_id=?', result.routerId);
      await this.send(
        actor,
        pending
          ? 'Операция сохранена, обновление таблицы ожидается.'
          : 'Операция сохранена, обновление таблицы подтверждено.',
      );
      return this.card(actor, result.routerId);
    }
    if (p.kind === 'role') {
      this.i.authorize(actor, 'admin');
      this.i.setRole(
        actor,
        p.data.target,
        p.data.role === 'revoke' ? null : p.data.role,
        'role:' + id,
      );
      return this.send(actor, 'Права обновлены. Отзыв действует на старые кнопки и диалоги.');
    }
    if (p.kind === 'campaign') {
      this.i.authorize(actor, 'admin');
      const c = this.c.save(actor, 'campaign:' + id, p.data);
      return this.campaignPreview(actor, c.id);
    }
    if (p.kind === 'queue_action') {
      const r = this.c.member(actor, 'queue:' + id, p.data.campaignId, p.data.loanId, p.data);
      return this.send(
        actor,
        'Действие записано. Срок меняется только решением администратора; приём — только после физического возврата.',
        [[button('Очередь', 'queue:' + p.data.campaignId)]],
      );
    }
    if (['resolve', 'resolve_custom'].includes(p.kind)) {
      this.i.authorize(actor, 'admin');
      const target = this.s.getDraft(actor, p.data.syncDraft, this.i.now()),
        entry = target.data.entries[p.data.entryIndex];
      assert(entry?.routerId, 'Конфликт не найден');
      target.data.resolutions ??= {};
      const custom = Object.fromEntries((p.data.customFields || []).map((f) => [f, p.data[f]]));
      target.data.resolutions[entry.routerId] = {
        source: p.data.source,
        reason: p.data.reason,
        ...(p.kind === 'resolve_custom' ? { values: custom } : {}),
      };
      this.s.run('UPDATE drafts SET data=? WHERE id=?', JSON.stringify(target.data), target.id);
      return this.send(
        actor,
        'Решение сохранено в предварительном просмотре. Теперь примените проверенный набор.',
        [[button('К просмотру', 'sv:' + target.id + ':0')]],
      );
    }
    assert(false, 'Неверная операция');
  }
  async showList(actor, filter = {}, page = 0, id = null) {
    const all = this.i.list(actor, filter);
    id ??= this.s.draft(actor, 'list', filter, this.i.now());
    page = Math.max(0, Math.min(page, Math.max(0, Math.ceil(all.length / 6) - 1)));
    const part = all.slice(page * 6, page * 6 + 6);
    const buttons = part.map((r) => [
      button(`${r.inventory} · ${STATUS[r.status]}${r.vip ? ' · VIP' : ''}`, 'card:' + r.id),
    ]);
    buttons.push([
      ...(page > 0 ? [button('←', 'list:' + id + ':' + (page - 1))] : []),
      ...(page * 6 + 6 < all.length ? [button('→', 'list:' + id + ':' + (page + 1))] : []),
    ]);
    buttons.push([button('Фильтры', 'start:filters'), button('Меню', 'menu')]);
    return this.send(
      actor,
      `Найдено: ${all.length}. Страница ${page + 1}.\n${part.map((r) => `${r.inventory}: ${STATUS[r.status]}, ${CONDITION[r.condition]}, ${r.loan?.place?.name || r.storage?.name || 'место не указано'}`).join('\n') || 'Записей нет'}`,
      buttons.filter((r) => r.length),
    );
  }
  async card(actor, id) {
    const r = this.i.get(actor, id),
      role = this.i.authorize(actor),
      l = r.loan,
      p = l?.recipient,
      c = p?.contact;
    const lines = [
      `Роутер ${r.inventory}`,
      `ID: ${r.id}`,
      `Модель: ${r.model || '—'}; серийный: ${r.serial || '—'}; MAC: ${r.mac || '—'}`,
      `Статус: ${STATUS[r.status]}${r.vip ? ' · VIP: ' + r.vipComment : ''}`,
      `Состояние: ${CONDITION[r.condition]}`,
      `Место: ${l?.place?.name || r.storage?.name || 'не указано'} (${valueLabel(l?.place?.certainty || r.storage?.certainty || 'unknown')})`,
      `Комплект: БП ${r.kit.power === null ? 'неизвестно' : r.kit.power ? 'есть' : 'нет'}, кабель ${r.kit.cable === null ? 'неизвестно' : r.kit.cable ? 'есть' : 'нет'} ${r.kit.other || ''}`,
    ];
    if (!l && r.legacy?.owner)
      lines.push(`Наследуемый Owner: ${r.legacy.owner} (не подтверждает действующую выдачу)`);
    if (l)
      lines.push(
        `Получатель: ${p.name || 'неизвестен'} (${valueLabel(p.kind)})`,
        `Группа: ${p.group || '—'}; набор: ${p.admissionYear || p.academicYear || 'не подтверждён'}`,
        `Контакты: ${c?.phone || '—'}; ${c?.telegram ? '@' + c.telegram : '—'}; подтверждённый ID: ${c?.userId || '—'}`,
        `Выдан: ${l.issuedAt || 'дата неизвестна'}; срок: ${l.dueConfirmed && l.dueAt ? l.dueAt : 'не определён'}${overdue(l, this.i.now()) ? ' · ПРОСРОЧЕНО' : ''}`,
        l.exception ? `Исключение: ${l.exception.reason}, пересмотр ${l.exception.reviewAt}` : '',
      );
    lines.push(
      `Комментарий: ${r.note || '—'}`,
      `Checked (смысл не подтверждён): ${display(r.legacy?.checked)}`,
      `Версия ${r.version}; обновлено ${r.updatedAt}`,
    );
    const outbox = this.s.get('SELECT state,last_error FROM outbox WHERE router_id=?', r.id);
    if (outbox)
      lines.push(
        `Таблица: ${outbox.state === 'blocked' ? 'конфликт' : 'обновление ожидается'}${outbox.last_error ? ' · ' + outbox.last_error : ''}`,
      );
    const actions = [button('История', 'hist:' + r.id + ':0')];
    if (role !== 'observer') {
      let kinds = ['move', 'condition', 'note'];
      if (r.status === 'stored') kinds.push('issue', 'lost');
      if (l) kinds.push('return', 'transfer', 'replace');
      if (r.status === 'issued') kinds.push('lost');
      if (r.status === 'lost') kinds.push('found');
      if (role === 'admin')
        kinds.push(
          'vip',
          'write_off',
          'correct',
          ...(l ? ['extend', 'exception'] : []),
          ...(r.status === 'lost' && r.found ? ['resolve_found'] : []),
        );
      for (const kind of kinds) actions.push(button(LABELS[kind], `act:${kind}:${r.id}`));
    }
    await this.sendChunks(actor, lines.filter(Boolean).join('\n'));
    return this.send(actor, 'Действия', [...rowsOf(actions), [button('Меню', 'menu')]]);
  }
  async history(actor, id, page) {
    const history = this.i.history(actor, id),
      loans = this.i.loans(actor, id),
      part = history.slice(page * 5, page * 5 + 5);
    const closed = {
      return: 'Возвращён',
      replacement: 'Замена',
      transfer: 'Передача',
      sheet_transfer: 'Передача из таблицы',
      written_off: 'Списан',
      correction: 'Исправленная выдача',
    };
    await this.sendChunks(
      actor,
      `История · событий ${history.length}, выдач ${loans.length}\n` +
        part
          .map(
            (e) =>
              `${e.at} · ${LABELS[e.action] || e.action}\nID: ${e.id}; сотрудник: ${e.actor}${e.correction_of ? '\nИсправляет: ' + e.correction_of : ''}${e.reason ? '\nПричина: ' + e.reason : ''}`,
          )
          .join('\n\n') +
        '\n\n' +
        loans
          .map(
            (l) =>
              `${l.id}: ${l.recipient?.name || 'получатель неизвестен'}; выдача ${l.issuedAt || '?'}; срок ${l.dueAt || '?'}; физический возврат ${l.returnedAt || '—'}; ${l.open ? 'открыта' : closed[l.closedKind] || l.closedKind}`,
          )
          .join('\n'),
    );
    return this.send(actor, 'Навигация', [
      [
        ...(page > 0 ? [button('←', `hist:${id}:${page - 1}`)] : []),
        ...(page * 5 + 5 < history.length ? [button('→', `hist:${id}:${page + 1}`)] : []),
        button('Карточка', 'card:' + id),
      ],
    ]);
  }
  async reports(actor) {
    const stats = this.i.stats(actor);
    return this.send(
      actor,
      `Физических роутеров: ${stats.physical}. Выдач за всё время: ${stats.loans}.\n${Object.entries(
        stats.statuses,
      )
        .map(([s, n]) => `${STATUS[s]}: ${n}`)
        .join('\n')}\nVIP: ${stats.vip} (пересекающийся признак).`,
      [
        ...rowsOf(
          Object.entries({
            available: 'Доступные к выдаче',
            issued: 'Выданные',
            overdue: 'Просроченные',
            lost: 'Утерянные',
            unknown: 'Неизвестные',
            incomplete: 'Неполные данные',
            undefined_due: 'Срок не определён',
            unknown_holder: 'Получатель не определён',
            unknown_cohort: 'Набор не подтверждён',
            check: 'Проверка / ремонт',
            vip: 'VIP',
            missing_kit: 'Недостающий комплект',
            pending: 'Ожидают таблицу',
            conflicts: 'Конфликты',
          }).map(([k, v]) => button(v, 'report:' + k)),
        ),
        [button('Выдачи за период', 'start:loan_report'), button('Фильтры', 'start:filters')],
        [button('Меню', 'menu')],
      ],
    );
  }
  async export(actor) {
    const rows = this.i.list(actor),
      data = [
        [
          'Инвентарный номер',
          'Модель',
          'MAC',
          'Статус',
          'Состояние',
          'VIP',
          'Место',
          'Получатель',
          'Срок',
        ],
        ...rows.map((r) => [
          r.inventory,
          r.model,
          r.mac,
          STATUS[r.status],
          CONDITION[r.condition],
          r.vip,
          r.loan?.place?.name || r.storage?.name,
          r.loan?.recipient?.name,
          r.loan?.dueConfirmed ? r.loan.dueAt : 'Срок не определён',
        ]),
      ];
    this.i.authorize(actor);
    return this.t.file(
      actor,
      'routers.csv',
      '\uFEFF' + data.map((r) => r.map(csvCell).join(';')).join('\r\n'),
    );
  }
  async settings(actor) {
    this.i.authorize(actor, 'admin');
    const roles = this.i.roles(actor),
      n = this.s.get('SELECT count(*) AS n FROM conflicts').n;
    return this.send(
      actor,
      `Последняя синхронизация: ${this.s.setting('last_sync_at', 'ещё не выполнялась')}. Нерешённых расхождений: ${n}.\nДоступ:\n${roles.map((r) => `${r.user_id} · ${valueLabel(r.role)}`).join('\n')}\nПодключение служебных колонок: /provision`,
      [
        [button('Доступ', 'start:role')],
        [button('Синхронизировать из таблицы', 'sync')],
        [button('Восстановить раскраску', 'colors')],
        [button('Создать кампанию', 'start:campaign'), button('Сбор', 'campaigns')],
        [button('Меню', 'menu')],
      ],
    );
  }
  async syncPreview(actor) {
    this.i.authorize(actor, 'admin');
    assert(this.sync, 'Google Sheets не подключён. Настройте .env по инструкции.');
    const p = await this.sync.preview(actor);
    return this.syncPage(actor, p.id, 0);
  }
  async syncPage(actor, id, page) {
    this.i.authorize(actor, 'admin');
    const p = this.s.getDraft(actor, id, this.i.now());
    assert(p.kind === 'sync', 'Неверное подтверждение');
    const entries = p.data.entries,
      changed = entries.filter((e) => e.type !== 'same'),
      part = changed.slice(page * 4, page * 4 + 4),
      buttons = [];
    const detail = part
      .map((e) => {
        const index = entries.indexOf(e);
        if (e.type === 'conflict' && !e.identity)
          buttons.push([button(`Разобрать строку ${e.row}`, `sr:${id}:${index}`)]);
        return `Строка ${e.row || 'исчезла'} · ${{ new: 'Новый', change: 'Изменения', conflict: 'Конфликт', review: 'Уточнение', error: 'Ошибка', missing: 'Исчезла' }[e.type] || e.type}${p.data.resolutions?.[e.routerId] ? ' · решение выбрано' : ''}\n${e.message || ''}${e.incomplete ? '\nНеполнота: ' + e.incomplete.join('; ') : ''}\n${(e.changes || []).map((c) => `${fieldLabel(c.field)}: ${display(c.before)} → ${display(c.sheet)} (бот: ${display(c.bot)})`).join('\n')}${
          e.type === 'new'
            ? '\n' +
              Object.entries(e.values)
                .map(([k, v]) => `${fieldLabel(k)}: ${display(v)}`)
                .join('\n')
            : ''
        }`;
      })
      .join('\n\n');
    await this.sendChunks(
      actor,
      `/sync · ${this.mode === 'memory' ? 'ДЕМОНСТРАЦИЯ · ' : ''}${changed.length ? 'Предварительный просмотр' : 'Изменений нет'}\nНовые: ${entries.filter((e) => e.type === 'new').length}, изменения: ${entries.filter((e) => e.type === 'change').length}, конфликты / уточнения: ${entries.filter((e) => ['conflict', 'review', 'error', 'missing'].includes(e.type)).length}.\n${detail}`,
    );
    buttons.push([
      ...(page > 0 ? [button('←', `sv:${id}:${page - 1}`)] : []),
      ...(page * 4 + 4 < changed.length ? [button('→', `sv:${id}:${page + 1}`)] : []),
    ]);
    if (changed.length) buttons.push([button('Применить проверенные изменения', 'sa:' + id)]);
    buttons.push([button('Отмена', 'menu')]);
    return this.send(
      actor,
      'Ошибки и неразобранные конфликты будут пропущены с сохранением блокировки исходящих записей.',
      buttons.filter((r) => r.length),
    );
  }
  async resolvePage(actor, id, index) {
    this.i.authorize(actor, 'admin');
    const d = this.s.getDraft(actor, id, this.i.now()),
      e = d.data.entries[index];
    assert(e && !e.identity, 'Требуется ручное восстановление ID');
    await this.sendChunks(
      actor,
      `Конфликт строки ${e.row}\n${(e.conflicts || []).map(fieldLabel).join(', ')}\n` +
        (e.changes || [])
          .map(
            (c) =>
              `${fieldLabel(c.field)}: исходное ${display(c.before)}, таблица ${display(c.sheet)}, бот ${display(c.bot)}`,
          )
          .join('\n'),
    );
    return this.send(
      actor,
      'Выберите целый согласованный вариант записи; бизнес-правила будут проверены при применении.',
      [
        [
          button('Из таблицы', `sx:${id}:${index}:sheet`),
          button('Из бота', `sx:${id}:${index}:bot`),
        ],
        [button('Исправить значения', `sx:${id}:${index}:custom`), button('Отмена', `sv:${id}:0`)],
      ],
    );
  }
  async colorPreview(actor) {
    this.i.authorize(actor, 'admin');
    assert(this.colors, 'Google Sheets не подключён');
    const p = await this.colors.preview(actor);
    await this.sendChunks(
      actor,
      `Раскраска: ${p.changed ? 'есть отличия' : 'изменений нет'}.\nРаспознано правил: ${p.recognized}, ожидается: ${p.expected}. Посторонних: ${p.foreign}; наследуемых Checked: ${p.legacyChecked.length}.\n${p.issues.join('\n')}\n${p.notes.join('\n')}\nИзменений данных оборудования не будет.`,
    );
    return this.send(actor, 'Применить показанные исправления?', [
      [button('Применить исправление', 'confirm:' + p.id), button('Отмена', 'menu')],
    ]);
  }
  async campaignList(actor) {
    const list = this.c.list(actor),
      admin = this.i.authorize(actor) === 'admin',
      buttons = list.map((c) => [
        button(c.name + ' · ' + (c.active ? 'активна' : 'проект'), 'queue:' + c.id),
      ]);
    if (admin) buttons.push([button('Создать кампанию', 'start:campaign')]);
    buttons.push([button('Меню', 'menu')]);
    return this.send(
      actor,
      'Кампании сбора. VIP учитывается на общих основаниях; исключения оформляются отдельно.',
      buttons,
    );
  }
  async campaignPreview(actor, id) {
    const p = this.c.preview(actor, id),
      d = this.s.draft(
        actor,
        'launch',
        {
          id,
          version: p.campaign.version,
          versions: Object.fromEntries(this.i.list(actor).map((r) => [r.id, r.version])),
        },
        this.i.now(),
      );
    await this.sendChunks(
      actor,
      `${p.campaign.name}: ${p.campaign.startsAt} — ${p.campaign.deadline}\n${p.campaign.location}, ${p.campaign.hours}\nОтветственные: ${p.campaign.responsibleIds.join(', ')}\nИнструкция: ${p.campaign.instruction}\nПодходящие выдачи: ${p.included.length}\n${p.included.map((x) => `${x.router.inventory} · ${x.loan.recipient.name} · ${x.loan.place.name}${x.router.vip ? ' · VIP' : ''}`).join('\n')}\nНа уточнение: ${p.clarify.length}\n${p.clarify.map((x) => `${x.router.inventory} · ${x.loan.recipient.name || 'неизвестно'}`).join('\n')}`,
    );
    const buttons = [
      [button('Подтвердить запуск', 'confirm:' + d), button('Настроить', 'ce:' + id)],
      [button('Включить выдачу явно', 'cin:' + id)],
      [button('Очередь', 'queue:' + id), button('Отмена', 'menu')],
    ];
    return this.send(
      actor,
      'Запускайте кампанию только с согласованными регламентом, датами и назначенными сотрудниками.',
      buttons,
    );
  }
  async queue(actor, id, page, filter = {}, nav = null) {
    const list = this.c.queue(actor, id, filter),
      stats = this.c.report(actor, id),
      part = list.slice(page * 4, page * 4 + 4),
      role = this.i.authorize(actor),
      draft = this.s.draft(
        actor,
        'queue',
        { campaignId: id, loanIds: list.map((x) => x.loan.id) },
        this.i.now(),
      );
    nav ??= this.s.draft(actor, 'queue_nav', { campaignId: id, filter }, this.i.now());
    await this.sendChunks(
      actor,
      `План: ${stats.plan}; принято: ${stats.returned}; осталось: ${stats.remaining}; просрочка: ${stats.overdue}; исключения: ${stats.excluded}; утрата: ${stats.lost}.\n` +
        part
          .map(
            (x) =>
              `${x.router.inventory} · ${x.loan.place?.name || 'место неизвестно'} · ${x.loan.recipient.name || 'неизвестно'}\nСрок: ${x.due}${x.overdue ? ' · ПРОСРОЧЕНО' : ''}${x.lost ? ' · УТРАТА' : ''}${x.excluded ? ' · ИСКЛЮЧЕНИЕ' : ''}${x.member.lastAction ? '\n' + x.member.lastAction.reason : ''}`,
          )
          .join('\n\n'),
    );
    const buttons = part.flatMap((x, j) => [
      [
        button('Карточка ' + x.router.inventory, 'card:' + x.router.id),
        ...(role !== 'observer'
          ? [
              button('Принят', `act:return:${x.router.id}`),
              button('Действие', `qa:${draft}:${page * 4 + j}`),
            ]
          : []),
      ],
    ]);
    buttons.push([
      ...(page > 0 ? [button('←', `qf:${nav}:${page - 1}`)] : []),
      ...(page * 4 + 4 < list.length ? [button('→', `qf:${nav}:${page + 1}`)] : []),
    ]);
    buttons.push([button('Фильтры очереди', 'qfilter:' + id)]);
    if (role === 'admin')
      buttons.push([button('Просмотр / запуск', 'cp:' + id), button('Настроить', 'ce:' + id)]);
    buttons.push([button('Меню', 'menu')]);
    return this.send(
      actor,
      '«Не застали» и обещание сдать не меняют статус и срок. Для продления откройте карточку (администратор).',
      buttons.filter((r) => r.length),
    );
  }
  async sendChunks(actor, text) {
    let remaining = String(text);
    while (remaining.length) {
      let n = Math.min(3500, remaining.length);
      if (n < remaining.length) {
        const cut = remaining.lastIndexOf('\n', n);
        if (cut > 1000) n = cut;
      }
      await this.send(actor, remaining.slice(0, n));
      remaining = remaining.slice(n);
    }
  }
}
