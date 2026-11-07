import { assert, contact, normalizeMac, optional, isoDate, equal } from '../util.mjs';
import { place, KIT } from '../service.mjs';

export const LEGACY_HEADERS = [
  'Inv. №',
  ' MAC Address',
  'Place',
  'Status',
  'Checked',
  'Owner',
  'Checkin ',
  'Checkout',
  'Сontacts',
  'Комментарий',
];
export const LEGACY_FIELDS = [
  'inventory',
  'mac',
  'place',
  'status',
  'checked',
  'owner',
  'checkin',
  'checkout',
  'rawContact',
  'note',
];
export const EXTRA_FIELDS = [
  'id',
  'model',
  'serial',
  'condition',
  'vip',
  'vipComment',
  'phone',
  'telegram',
  'userId',
  'recipientKind',
  'recipientId',
  'group',
  'admissionYear',
  'academicYear',
  'issuedAt',
  'dueAt',
  'dueConfirmed',
  'returnedAt',
  'placeType',
  'placeCertainty',
  'floor',
  'section',
  'power',
  'cable',
  'other',
  'responsible',
  'assignedOperator',
];
export const EXTRA_HEADERS = [
  'router_id',
  'Модель',
  'Серийный номер',
  'Техническое состояние',
  'VIP',
  'Смысл VIP',
  'Телефон',
  'Telegram username',
  'Подтверждённый Telegram ID',
  'Тип получателя',
  'recipient_id',
  'Группа',
  'Год поступления',
  'Учебный год',
  'Дата выдачи (подтверждена)',
  'Срок возврата (план)',
  'Срок подтверждён',
  'Фактический возврат',
  'Тип места',
  'Достоверность места',
  'Этаж',
  'Секция',
  'Блок питания',
  'Кабель',
  'Прочий комплект',
  'Ответственный от организации',
  'Оператор user_id',
];
export const FIELDS = [...LEGACY_FIELDS, ...EXTRA_FIELDS];
export const LINKED = new Set([
  'status',
  'owner',
  'place',
  'recipientKind',
  'recipientId',
  'group',
  'admissionYear',
  'academicYear',
  'issuedAt',
  'dueAt',
  'dueConfirmed',
  'returnedAt',
  'placeType',
  'placeCertainty',
  'floor',
  'section',
  'responsible',
]);
export const CONTACT_FIELDS = new Set(['rawContact', 'phone', 'telegram', 'userId']);
export const EXTERNAL = {
  stored: 'Свободен',
  issued: 'Выдан',
  lost: 'Утерян',
  unknown: 'Неизвестно',
  written_off: 'Списан',
};
export const INTERNAL = {
  Свободен: 'stored',
  'На хранении': 'stored',
  Выдан: 'issued',
  Просрочен: 'issued',
  Утерян: 'lost',
  '?': 'unknown',
  Неизвестно: 'unknown',
  Списан: 'written_off',
};
export const TECH = {
  Исправен: 'working',
  'Нужна проверка': 'needs_check',
  Неисправен: 'broken',
  'В ремонте': 'repair',
};
export const TECH_OUT = Object.fromEntries(Object.entries(TECH).map(([k, v]) => [v, k]));
export function column(index) {
  let n = index + 1,
    s = '';
  while (n > 0) {
    n--;
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26);
  }
  return s;
}
export const LAST_COLUMN = column(14 + EXTRA_FIELDS.length - 1);
export function rowValues(cells, hasExtras = true) {
  const v = Object.fromEntries(LEGACY_FIELDS.map((f, i) => [f, cells[i] ?? '']));
  if (hasExtras) EXTRA_FIELDS.forEach((f, i) => (v[f] = cells[14 + i] ?? ''));
  for (const f of ['checked', 'vip', 'dueConfirmed', 'power', 'cable'])
    if (f in v && typeof v[f] === 'string' && /^(true|false)$/i.test(v[f]))
      v[f] = v[f].toLowerCase() === 'true';
  return v;
}
export function validateSheet(sheet, { requireExtras = false } = {}) {
  assert(sheet.title && Number.isInteger(sheet.sheetId), 'Неизвестный лист');
  assert(
    LEGACY_HEADERS.every((h, i) => sheet.headers[i] === h),
    'Структура A:J изменилась. Запись остановлена.',
    'schema',
  );
  const extra = EXTRA_HEADERS.every((h, i) => sheet.headers[14 + i] === h);
  const emptyExtras = sheet.headers.slice(14, 14 + EXTRA_FIELDS.length).every((v) => !v);
  assert(
    extra || emptyExtras,
    'Служебные колонки заняты или изменены. Требуется согласовать схему.',
    'schema',
  );
  assert(
    !requireExtras || extra,
    'Сначала согласуйте и подключите служебные колонки O:' + LAST_COLUMN,
    'schema',
  );
  return extra;
}
export function projection(r, loan, lastLoan = null) {
  const l = loan,
    p = l?.recipient || { name: r.legacy?.owner || '', contact: r.legacy?.contact || {} },
    loc = l?.place || r.storage || place('', 'other', 'absent'),
    c = p.contact || {};
  return {
    inventory: r.inventory,
    mac: r.mac || '',
    place: loc.name || '',
    status: r.status === 'unknown' && r.legacy?.status === '?' ? '?' : EXTERNAL[r.status],
    checked: r.legacy?.checked ?? '',
    owner: p.name || '',
    checkin: r.legacy?.checkin ?? '',
    checkout: r.legacy?.checkout ?? '',
    rawContact: c.raw || '',
    note: r.note || '',
    id: r.id,
    model: r.model || '',
    serial: r.serial || '',
    condition: TECH_OUT[r.condition],
    vip: Boolean(r.vip),
    vipComment: r.vipComment || '',
    phone: c.phone || '',
    telegram: c.telegram || '',
    userId: c.userId || '',
    recipientKind: p.kind || '',
    recipientId: p.id || '',
    group: p.group || '',
    admissionYear: p.admissionYear || '',
    academicYear: p.academicYear || '',
    issuedAt: l?.issuedAt || '',
    dueAt: l?.dueAt || '',
    dueConfirmed: l?.dueConfirmed || false,
    returnedAt: !l ? lastLoan?.returnedAt || '' : '',
    placeType: loc.type || 'other',
    placeCertainty: loc.certainty || 'unknown',
    floor: loc.floor || '',
    section: loc.section || '',
    power: r.kit?.power ?? '',
    cable: r.kit?.cable ?? '',
    other: r.kit?.other || '',
    responsible: p.responsible || '',
    assignedOperator: l?.assignedOperator || '',
  };
}
export function validateFlat(v) {
  assert(String(v.inventory || '').trim(), 'Пустой инвентарный номер');
  assert(INTERNAL[String(v.status).trim()], 'Неизвестный статус в таблице');
  normalizeMac(v.mac);
  if (v.condition) assert(TECH[v.condition], 'Неизвестное техническое состояние');
  if (v.recipientKind)
    assert(
      ['person', 'organization', 'unknown'].includes(v.recipientKind),
      'Неверный тип получателя',
    );
  for (const f of ['issuedAt', 'dueAt', 'returnedAt']) if (v[f]) isoDate(String(v[f]));
  for (const f of ['checked', 'vip', 'dueConfirmed', 'power', 'cable'])
    if (v[f] !== undefined && v[f] !== '')
      assert(typeof v[f] === 'boolean', `Поле ${f}: TRUE, FALSE или пусто`);
  if (v.placeType)
    assert(['room', 'common', 'warehouse', 'other'].includes(v.placeType), 'Неверный тип места');
  if (v.placeCertainty)
    assert(
      ['absent', 'unknown', 'legacy', 'confirmed'].includes(v.placeCertainty),
      'Неверная достоверность места',
    );
  if (v.userId)
    assert(Number.isSafeInteger(Number(v.userId)) && Number(v.userId) > 0, 'Неверный Telegram ID');
  if (v.recipientKind === 'organization')
    assert(!v.admissionYear && !v.academicYear, 'Организация не имеет набора студентов');
}
export function decode(v, { initial = false } = {}) {
  validateFlat(v);
  const defaultCertainty = !v.place
    ? 'absent'
    : /^(\?|неизвестно)$/i.test(String(v.place).trim())
      ? 'unknown'
      : 'legacy';
  const p = place(
    String(v.place || ''),
    v.placeType || 'other',
    v.placeCertainty || defaultCertainty,
    String(v.floor || ''),
    String(v.section || ''),
  );
  const c = contact(v.rawContact || '');
  for (const f of ['phone', 'telegram', 'userId'])
    if (f in v && !(initial && v[f] === ''))
      c[f] = v[f] === '' ? null : f === 'userId' ? Number(v[f]) : String(v[f]);
  // Only this known organisation is classified; ambiguous legacy holders need review.
  const kind =
    v.recipientKind || (String(v.owner).trim() === 'Студсовет ФПМИ' ? 'organization' : 'unknown');
  const status = INTERNAL[String(v.status).trim()],
    legacyHolder = !['issued', 'lost'].includes(status);
  return {
    inventory: String(v.inventory).trim(),
    model: optional(v.model),
    serial: optional(v.serial),
    mac: normalizeMac(v.mac),
    status,
    condition: TECH[v.condition] || 'needs_check',
    vip: v.vip === true,
    vipComment: String(v.vipComment || ''),
    note: String(v.note || ''),
    legacy: {
      checked: v.checked === '' ? null : (v.checked ?? null),
      checkin: v.checkin ?? null,
      checkout: v.checkout ?? null,
      status: String(v.status).trim(),
      owner: legacyHolder ? String(v.owner || '') : '',
      contact: legacyHolder ? c : null,
    },
    storage: p,
    kit: {
      power: v.power === '' || v.power === undefined ? null : v.power,
      cable: v.cable === '' || v.cable === undefined ? null : v.cable,
      other: String(v.other || ''),
    },
    recipient: {
      id: v.recipientId || null,
      kind,
      name: String(v.owner || ''),
      contact: c,
      group: String(v.group || ''),
      admissionYear: v.admissionYear ? Number(v.admissionYear) : null,
      academicYear: String(v.academicYear || ''),
      responsible: String(v.responsible || ''),
    },
    place: p,
    issuedAt: v.issuedAt ? String(v.issuedAt) : null,
    dueAt: v.dueAt ? String(v.dueAt) : null,
    dueConfirmed: v.dueConfirmed === true,
    returnedAt: v.returnedAt ? String(v.returnedAt) : null,
    assignedOperator: v.assignedOperator ? Number(v.assignedOperator) : null,
  };
}
export function mergeThree(base, sheet, bot) {
  const merged = { ...bot },
    conflicts = [],
    changes = [];
  let remoteLinked = false,
    localLinked = false;
  for (const f of Object.keys(sheet)) {
    const a = base[f] ?? '',
      b = sheet[f],
      c = bot[f] ?? '';
    const remote = !equal(a, b),
      local = !equal(a, c);
    if (remote) {
      changes.push({ field: f, before: a, sheet: b, bot: c });
      if (LINKED.has(f)) remoteLinked = true;
    }
    if (local && LINKED.has(f)) localLinked = true;
    if (remote && local && !equal(b, c)) conflicts.push(f);
    else if (remote) merged[f] = b;
  }
  if (
    remoteLinked &&
    localLinked &&
    [...LINKED].some((f) => f in sheet && !equal(sheet[f], bot[f]))
  )
    conflicts.push('связанные поля выдачи');
  if (
    [...CONTACT_FIELDS].some((f) => f in sheet && !equal(base[f] ?? '', sheet[f])) &&
    [...CONTACT_FIELDS].some((f) => !equal(base[f] ?? '', bot[f] ?? '')) &&
    [...CONTACT_FIELDS].some((f) => f in sheet && !equal(sheet[f], bot[f] ?? ''))
  )
    conflicts.push('связанные контактные поля');
  return { merged, conflicts: [...new Set(conflicts)], changes };
}
