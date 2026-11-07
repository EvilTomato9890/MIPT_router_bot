import { assert, isoDate, contact, normalizeMac, CONDITION, localClock } from './util.mjs';
import { place } from './service.mjs';
import { fieldLabel } from './ru.mjs';

const str = (key, prompt, { optional = false, choices, when } = {}) => ({
  key,
  prompt,
  choices,
  when,
  parse: (v) => {
    v = v.trim();
    if (optional && v === '-') return '';
    assert(
      v.length > 0 && v.length <= 1000,
      'Введите текст до 1000 символов' + (optional ? ' или -' : ''),
    );
    return v;
  },
});
const choice = (key, prompt, values, when) => ({
  key,
  prompt,
  choices: Object.entries(values).map(([value, label]) => ({ value, label })),
  when,
  parse: (v) => {
    assert(v in values, 'Выберите вариант кнопкой');
    return v;
  },
});
const date = (key, prompt, optional = false) => ({
  key,
  prompt,
  parse: (v) => (optional && v === '-' ? null : isoDate(v.trim())),
});
const yesNo = (key, prompt, unknown = false, when) => ({
  ...choice(
    key,
    prompt,
    { yes: 'Да', no: 'Нет', ...(unknown ? { unknown: 'Неизвестно' } : {}) },
    when,
  ),
  parse: (v) => {
    assert(['yes', 'no', ...(unknown ? ['unknown'] : [])].includes(v), 'Выберите вариант кнопкой');
    return v === 'unknown' ? null : v === 'yes';
  },
});
const num = (key, prompt, optional = false, when) => ({
  key,
  prompt,
  when,
  parse: (v) => {
    if (optional && v === '-') return null;
    const n = Number(v);
    assert(Number.isSafeInteger(n) && n > 0, 'Введите положительное целое число');
    return n;
  },
});
const isPerson = (d) => d.recipientKind === 'person';
const kit = () => [
  yesNo('power', 'Блок питания в комплекте?', true),
  yesNo('cable', 'Кабель в комплекте?', true),
  str('other', 'Прочие принадлежности / состояние комплекта (или -)', { optional: true }),
];
const storage = () => [
  str('storageName', 'Место хранения'),
  choice('storageType', 'Тип места хранения', {
    warehouse: 'Склад',
    room: 'Комната',
    common: 'Общее помещение',
    other: 'Другое',
  }),
];
const recipient = () => [
  choice('recipientKind', 'Кому выдаётся роутер?', {
    person: 'Человек',
    organization: 'Организация',
  }),
  str('recipientName', 'ФИО или название организации'),
  str('responsible', 'Ответственный человек организации (или -)', {
    optional: true,
    when: (d) => d.recipientKind === 'organization',
  }),
  str('group', 'Учебная группа (или -)', { optional: true, when: isPerson }),
  num('admissionYear', 'Подтверждённый год поступления (или -)', true, isPerson),
  str('academicYear', 'Учебный год, например 2026/2027 (или -)', {
    optional: true,
    when: isPerson,
  }),
  str('placeName', 'Место использования: номер комнаты или название'),
  choice('placeType', 'Тип места', {
    room: 'Комната',
    common: 'Общее помещение',
    warehouse: 'Склад',
    other: 'Другое',
  }),
  str('phone', 'Телефон (или -)', { optional: true }),
  str('telegram', 'Telegram username, без @ (или -)', { optional: true }),
  num(
    'userId',
    'Подтверждённый числовой Telegram ID получателя (или -; по username не угадывать)',
    true,
  ),
  date('dueAt', 'Подтверждённый срок возврата: ГГГГ-ММ-ДД'),
];

export function fields(kind, data = {}) {
  const reason = str('reason', 'Причина / обстоятельства');
  switch (kind) {
    case 'add':
      return [
        str('inventory', 'Инвентарный номер'),
        str('model', 'Модель (или -)', { optional: true }),
        str('serial', 'Серийный номер (или -)', { optional: true }),
        {
          ...str('mac', 'MAC (или -)', { optional: true }),
          parse: (v) => normalizeMac(v === '-' ? null : v),
        },
        choice('condition', 'Техническое состояние', CONDITION),
        ...storage(),
        ...kit(),
      ];
    case 'issue':
      return [...recipient(), yesNo('kitChecked', 'Комплект в карточке сверен?')];
    case 'transfer':
      return [...recipient(), reason];
    case 'return':
      return [
        str('inventory', 'Сверьте и введите инвентарный номер принимаемого роутера'),
        yesNo('physical', 'Роутер физически принят?'),
        ...storage(),
        choice('condition', 'Результат проверки', CONDITION),
        ...kit(),
        str('reason', 'Повреждения / замечания (или -)', { optional: true }),
      ];
    case 'replace':
      return [
        str('newInventory', 'Инвентарный номер НОВОГО роутера'),
        str('inventory', 'Сверьте номер СТАРОГО принимаемого роутера'),
        yesNo('physical', 'Старый роутер физически принят?'),
        ...storage(),
        choice('condition', 'Состояние старого роутера при приёме', CONDITION),
        ...kit(),
        date('dueAt', 'Срок нового роутера (если у старого подтверждённый срок, он сохранится)'),
        reason,
      ];
    case 'extend':
      return [date('dueAt', 'Новый срок: ГГГГ-ММ-ДД'), reason];
    case 'lost':
    case 'found':
      return [reason];
    case 'resolve_found':
      return [
        choice('resolution', 'Решение по обнаружению', {
          holder: 'Остаётся у прежнего получателя',
          storage: 'Физически принят на хранение',
        }),
        ...fields('return').map((f) => ({ ...f, when: (d) => d.resolution === 'storage' })),
        reason,
      ];
    case 'condition':
      return [
        choice('condition', 'Результат проверки / состояние ремонта', CONDITION),
        ...kit(),
        reason,
      ];
    case 'write_off':
      return [
        reason,
        yesNo('resolveOutstanding', 'Подтверждаете решение по возможной незакрытой выдаче?'),
      ];
    case 'vip':
      return [yesNo('vip', 'Включить VIP?'), reason];
    case 'exception':
      return [
        yesNo('enabled', 'Исключить из сбора до даты пересмотра?'),
        { ...date('reviewAt', 'Дата пересмотра исключения'), when: (d) => d.enabled },
        reason,
      ];
    case 'move':
      return [
        str('placeName', 'Новое место'),
        choice('placeType', 'Тип места', {
          room: 'Комната',
          common: 'Общее помещение',
          warehouse: 'Склад',
          other: 'Другое',
        }),
        str('floor', 'Этаж (или -)', { optional: true }),
        str('section', 'Секция (или -)', { optional: true }),
      ];
    case 'note':
      return [str('note', 'Новый комментарий (или - для очистки)', { optional: true })];
    case 'correct':
      return [str('eventId', 'ID ошибочной операции из истории'), reason];
    case 'role':
      return [
        num('target', 'Числовой Telegram ID сотрудника'),
        choice('role', 'Новая роль', {
          admin: 'Администратор',
          operator: 'Оператор',
          observer: 'Наблюдатель',
          revoke: 'Отозвать доступ',
        }),
      ];
    case 'campaign':
      return [
        str('name', 'Название кампании'),
        num('admissionYear', 'Набор: год поступления (или -)', true),
        str('academicYear', 'Учебный год (или -; нужен хотя бы один критерий)', { optional: true }),
        date('startsAt', 'Дата начала сбора'),
        date('deadline', 'Конечный срок кампании'),
        str('responsibleIds', 'Telegram ID ответственных через запятую'),
        str('location', 'Место приёма'),
        str('hours', 'Часы работы'),
        str('instruction', 'Инструкция по сдаче'),
      ];
    case 'campaign_edit':
      return [
        str('responsibleIds', 'Новые Telegram ID ответственных через запятую'),
        date('deadline', 'Конечный срок кампании'),
        str('hours', 'Часы работы'),
        str('instruction', 'Инструкция'),
        yesNo('active', 'Кампания активна? (Нет — приостановить напоминания)'),
      ];
    case 'campaign_include':
      return [str('loanInventory', 'Инвентарный номер для явного включения в кампанию'), reason];
    case 'loan_report':
      return [
        date('from', 'Выдачи за период: начало ГГГГ-ММ-ДД'),
        date('to', 'Выдачи за период: конец ГГГГ-ММ-ДД'),
      ];
    case 'queue_filters':
      return [
        str('group', 'Группа (или -)', { optional: true }),
        str('place', 'Место (или -)', { optional: true }),
        num('operator', 'Ответственный user_id (или -)', true),
      ];
    case 'queue_action':
      return [
        choice('action', 'Что сделано?', {
          not_home: 'Не застали',
          contacted: 'Связались',
          new_date: 'Согласована другая дата (передать админу)',
          problem: 'Проблема',
          next: 'Следующий',
          include: 'Включить явно (админ)',
          exclude: 'Исключить из этой кампании (админ)',
          assign: 'Назначить оператора (админ)',
          priority: 'Изменить порядок (админ)',
        }),
        { ...num('operatorId', 'ID назначенного оператора'), when: (d) => d.action === 'assign' },
        {
          key: 'priority',
          prompt: 'Порядок (число; меньше — раньше)',
          when: (d) => d.action === 'priority',
          parse: (v) => {
            assert(Number.isFinite(Number(v)), 'Введите число');
            return Number(v);
          },
        },
        reason,
      ];
    case 'filters':
      return [
        choice('report', 'Категория отчёта', {
          all: 'Все',
          available: 'Доступные',
          overdue: 'Просроченные',
          incomplete: 'Неполные данные',
          check: 'Проверка / ремонт',
          missing_kit: 'Недостающий комплект',
          pending: 'Ожидают таблицу',
          conflicts: 'Конфликты',
        }),
        choice('status', 'Статус', {
          all: 'Любой',
          stored: 'На хранении',
          issued: 'Выдан',
          lost: 'Утерян',
          unknown: 'Неизвестно',
          written_off: 'Списан',
        }),
        choice('condition', 'Состояние', { all: 'Любое', ...CONDITION }),
        choice('vip', 'VIP', { all: 'Любой', yes: 'VIP', no: 'Без VIP' }),
        str('year', 'Набор или учебный год (или -)', { optional: true }),
        str('group', 'Группа (или -)', { optional: true }),
        str('place', 'Место (или -)', { optional: true }),
        num('operator', 'Ответственный user_id (или -)', true),
      ];
    case 'resolve':
      return [str('reason', 'Причина выбранного разрешения конфликта')];
    case 'resolve_custom':
      return [
        ...(data.customFields || []).map((field) => ({
          key: field,
          prompt: `Исправленное значение «${fieldLabel(field)}» (- для очистки; логические поля TRUE/FALSE)`,
          parse: (v) => {
            if (v === '-') return '';
            if (['vip', 'checked', 'dueConfirmed', 'power', 'cable'].includes(field)) {
              assert(/^(true|false)$/i.test(v), 'Введите TRUE или FALSE');
              return v.toLowerCase() === 'true';
            }
            return v;
          },
        })),
        reason,
      ];
    default:
      assert(false, 'Неизвестный диалог');
  }
}

export function command(kind, data, inventory, actor) {
  const d = structuredClone(data),
    out = { kind, routerId: d.routerId, expected: d.expected, reason: d.reason || undefined };
  if (kind === 'add')
    return {
      ...out,
      inventory: d.inventory,
      model: d.model,
      serial: d.serial,
      mac: d.mac,
      condition: d.condition,
      storage: place(d.storageName, d.storageType),
      kit: { power: d.power, cable: d.cable, other: d.other || '' },
    };
  if (['issue', 'transfer'].includes(kind)) {
    if (kind === 'issue') assert(d.kitChecked === true, 'Перед выдачей сверьте комплект');
    const c = {
      raw: [d.phone, d.telegram ? '@' + d.telegram.replace(/^@/, '') : '']
        .filter(Boolean)
        .join(' '),
      phone: d.phone || null,
      telegram: d.telegram?.replace(/^@/, '') || null,
      userId: d.userId || null,
    };
    return {
      ...out,
      recipient: {
        kind: d.recipientKind,
        name: d.recipientName,
        group: d.group || '',
        admissionYear: d.admissionYear || null,
        academicYear: d.academicYear || '',
        responsible: d.responsible || '',
        contact: c,
      },
      place: place(d.placeName, d.placeType),
      dueAt: d.dueAt,
    };
  }
  if (['return', 'replace', 'resolve_found'].includes(kind)) {
    Object.assign(out, {
      resolution: d.resolution,
      inventory: d.inventory,
      physical: d.physical,
      storage: place(d.storageName || '', d.storageType || 'other'),
      condition: d.condition || 'needs_check',
      kit: { power: d.power ?? null, cable: d.cable ?? null, other: d.other || '' },
    });
    if (kind === 'replace') {
      const next = inventory.get(actor, d.newInventory);
      out.newRouterId = next.id;
      out.expected = { ...d.expected, [next.id]: next.version };
      out.dueAt = d.dueAt;
    }
    return out;
  }
  if (kind === 'condition')
    return {
      ...out,
      condition: d.condition,
      kit: { power: d.power, cable: d.cable, other: d.other || '' },
    };
  if (kind === 'move')
    return { ...out, place: place(d.placeName, d.placeType, 'confirmed', d.floor, d.section) };
  return {
    ...out,
    ...Object.fromEntries(
      ['dueAt', 'vip', 'reviewAt', 'enabled', 'note', 'eventId', 'resolveOutstanding']
        .filter((k) => k in d)
        .map((k) => [k, d[k]]),
    ),
  };
}

export const LABELS = {
  add: 'Добавить',
  issue: 'Выдать',
  return: 'Принять',
  extend: 'Продлить срок',
  transfer: 'Передать',
  replace: 'Заменить',
  move: 'Изменить место',
  lost: 'Утрата',
  found: 'Обнаружен',
  resolve_found: 'Решение по обнаружению',
  condition: 'Проверка / ремонт',
  write_off: 'Списание',
  vip: 'VIP',
  exception: 'Исключение из сбора',
  note: 'Комментарий',
  correct: 'Исправить ошибку',
  role: 'Доступ',
  campaign: 'Создать кампанию',
  campaign_edit: 'Настроить кампанию',
  campaign_include: 'Включить выдачу',
  loan_report: 'Выдачи за период',
  queue_action: 'Действие в очереди',
  queue_filters: 'Фильтры очереди',
  filters: 'Фильтры',
  resolve: 'Разрешить конфликт',
  resolve_custom: 'Исправить значения',
};
