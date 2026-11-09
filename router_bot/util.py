"""Validation, serialization and dates shared by all application layers."""

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from datetime import UTC, date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

clone = deepcopy
STATUS = {
    "stored": "На хранении",
    "issued": "Выдан",
    "lost": "Утерян",
    "unknown": "Неизвестно",
    "written_off": "Списан",
}
CONDITION = {
    "working": "Исправен",
    "needs_check": "Нужна проверка",
    "broken": "Неисправен",
    "repair": "В ремонте",
}


class DomainError(Exception):
    def __init__(self, message, code="validation"):
        super().__init__(message)
        self.code = code


def require(condition, message, code="validation"):
    if not condition:
        raise DomainError(message, code)


def uuid():
    return str(uuid4())


def now_utc():
    return datetime.now(UTC)


def stamp(now=None):
    return (
        (now or now_utc()).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def parse_stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stable(value):
    # Same canonical field ordering / UTF-8 as the previous JavaScript engine.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(stable(value).encode("utf-8")).hexdigest()


def equal(a, b):
    return stable(a) == stable(b)


def text(value, maximum=1000):
    require(
        isinstance(value, str) and len(value) <= maximum, f"Ожидается текст до {maximum} символов"
    )
    return value.strip()


def optional(value):
    if value is None or re.fullmatch(r"(?:нет|н/д|n/a|unknown|[-—?])?", str(value).strip(), re.I):
        return None
    return text(str(value))


def normalize_serial(value):
    value = optional(value)
    return re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", value).upper()) if value else None


def normalize_mac(value):
    if not optional(value):
        return None
    value = re.sub(r"[:.\s-]", "", str(value)).upper()
    require(re.fullmatch(r"[0-9A-F]{12}", value), "MAC должен содержать 12 шестнадцатеричных цифр")
    return ":".join(value[i : i + 2] for i in range(0, 12, 2))


def iso_date(value, allow_empty=False):
    if allow_empty and not value:
        return None
    try:
        require(
            isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Дата: ГГГГ-ММ-ДД"
        )
        require(date.fromisoformat(value).isoformat() == value, "Дата: ГГГГ-ММ-ДД")
    except ValueError:
        raise DomainError("Дата: ГГГГ-ММ-ДД") from None
    return value


def phone_number(value):
    value = str(value or "").strip()
    digits = re.sub(r"[ ()-]", "", value)
    require(
        re.fullmatch(r"\+?\d{10,15}", digits),
        "Введите телефон из 10–15 цифр, например +79991234567. Вместо телефона можно выбрать Telegram ID через «Дополнительно».",
    )
    return digits


def contact(raw=""):
    raw = str(raw or "")
    username = re.search(r"(?:@|t\.me/)([a-zA-Z][a-zA-Z0-9_]{4,31})", raw)
    identifier = re.search(r"tg://user\?id=(\d+)", raw)
    # A numeric Telegram identifier must never be mistaken for a telephone.
    phone_text = re.sub(r"tg://user\?id=\d+", "", raw)
    phone = re.search(r"\+?\d[\d ()-]{8,}\d", phone_text)
    return {
        "raw": raw,
        "phone": phone[0].strip() if phone else None,
        "telegram": username[1] if username else None,
        "userId": int(identifier[1]) if identifier and valid_id(int(identifier[1])) else None,
    }


def contact_text(value):
    value = value or {}
    raw = value.get("raw") or " ".join(
        v
        for v in (value.get("phone"), "@" + value["telegram"] if value.get("telegram") else "")
        if v
    )
    identifier = value.get("userId")
    if valid_id(identifier) and not re.search(r"tg://user\?id=" + str(identifier) + r"\b", raw):
        raw = re.sub(r"\s*tg://user\?id=\d+", "", raw).strip()
        raw = (raw + " tg://user?id=" + str(identifier)).strip()
    return raw


def local_clock(now=None):
    value = (now or now_utc()).astimezone(ZoneInfo("Europe/Moscow"))
    return {"day": value.strftime("%Y-%m-%d"), "time": value.strftime("%H:%M")}


def overdue(loan, now=None):
    return bool(
        loan
        and loan.get("open")
        and loan.get("dueConfirmed")
        and loan.get("dueAt")
        and loan["dueAt"] < local_clock(now)["day"]
    )


def natural(value):
    return tuple(
        (0, int(p)) if p.isdigit() else (1, p.casefold())
        for p in re.split(r"(\d+)", str(value or ""))
    )


def csv_cell(value):
    value = "" if value is None else str(value)
    if re.match(r"^[\s\ufeff]*[=+@-]", value):
        value = "'" + value
    return '"' + value.replace('"', '""') + '"'


def redact(value):
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {
            k: redact(v)
            for k, v in value.items()
            if k not in {"contact", "rawContact", "phone", "telegram", "userId"}
        }
    return value


def valid_id(value):
    return type(value) is int and 0 < value <= 2**53 - 1
