import { createHash, randomUUID } from 'node:crypto';

export const uuid = randomUUID;
export const clone = (value) => structuredClone(value);
export const stable = (value) =>
  JSON.stringify(value, function (k, v) {
    return v && !Array.isArray(v) && typeof v === 'object'
      ? Object.fromEntries(
          Object.keys(v)
            .sort()
            .map((key) => [key, v[key]]),
        )
      : v;
  });
export const equal = (a, b) => stable(a) === stable(b);
export const hash = (value) => createHash('sha256').update(stable(value)).digest('hex');
export class DomainError extends Error {
  constructor(message, code = 'validation') {
    super(message);
    this.name = 'DomainError';
    this.code = code;
  }
}
export function assert(ok, message, code) {
  if (!ok) throw new DomainError(message, code);
}
export const STATUS = {
  stored: 'На хранении',
  issued: 'Выдан',
  lost: 'Утерян',
  unknown: 'Неизвестно',
  written_off: 'Списан',
};
export const CONDITION = {
  working: 'Исправен',
  needs_check: 'Нужна проверка',
  broken: 'Неисправен',
  repair: 'В ремонте',
};
export function text(value, max = 1000) {
  assert(typeof value === 'string' && value.length <= max, `Ожидается текст до ${max} символов`);
  return value.trim();
}
export function optional(value) {
  if (
    value === null ||
    value === undefined ||
    /^(?:нет|н\/д|n\/a|unknown|[-—?])?$/i.test(String(value).trim())
  )
    return null;
  return text(String(value));
}
export const normalizeSerial = (value) =>
  optional(value)?.normalize('NFKC').toUpperCase().replace(/[\s-]/g, '') || null;
export function normalizeMac(value) {
  if (!optional(value)) return null;
  const mac = String(value)
    .replace(/[:.\s-]/g, '')
    .toUpperCase();
  assert(/^[\dA-F]{12}$/.test(mac), 'MAC должен содержать 12 шестнадцатеричных цифр');
  return mac.match(/../g).join(':');
}
export function isoDate(value, allowEmpty = false) {
  if (allowEmpty && !value) return null;
  assert(
    typeof value === 'string' &&
      /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      !Number.isNaN(Date.parse(value)) &&
      new Date(value).toISOString().slice(0, 10) === value,
    'Дата: ГГГГ-ММ-ДД',
  );
  return value;
}
export function contact(raw = '') {
  raw = String(raw);
  const username = raw.match(/(?:@|t\.me\/)([a-zA-Z][\w]{4,31})/);
  const phone = raw.match(/\+?\d[\d ()-]{8,}\d/);
  return {
    raw,
    phone: phone ? phone[0].trim() : null,
    telegram: username ? username[1] : null,
    userId: null,
  };
}
export function localClock(now = new Date(), zone = 'Europe/Moscow') {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(now);
  return { day: parts.slice(0, 10), time: parts.slice(11, 16) };
}
export const dayDiff = (a, b) => Math.round((Date.parse(a) - Date.parse(b)) / 86400000);
export const overdue = (loan, now = new Date()) =>
  Boolean(loan?.open && loan.dueConfirmed && loan.dueAt && loan.dueAt < localClock(now).day);
export const csvCell = (value) =>
  '"' +
  String(value ?? '')
    .replace(/^[\s\uFEFF]*[=+@-]/, (m) => "'" + m)
    .replaceAll('"', '""') +
  '"';
export function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.entries(value)
        .filter(([k]) => !['contact', 'rawContact', 'phone', 'telegram', 'userId'].includes(k))
        .map(([k, v]) => [k, redact(v)]),
    );
  return value;
}
