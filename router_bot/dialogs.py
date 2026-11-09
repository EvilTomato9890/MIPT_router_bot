"""Small data-driven Russian dialogs. No dialog changes accounting before confirmation."""

from dataclasses import dataclass
from typing import Callable

from .service import place
from .util import CONDITION, DomainError, contact, contact_text, iso_date, normalize_mac, require


@dataclass
class Field:
    key: str
    prompt: str
    parse: Callable
    choices: dict | None = None
    when: Callable | None = None


def string(key, prompt, optional=False, when=None):
    def parse(value):
        value = value.strip()
        if optional and value == "-":
            return ""
        require(
            0 < len(value) <= 1000,
            "Введите текст до 1000 символов" + (" или -" if optional else ""),
        )
        return value

    return Field(key, prompt, parse, when=when)


def choice(key, prompt, choices, when=None):
    def parse(value):
        require(value in choices, "Выберите вариант кнопкой")
        return value

    return Field(key, prompt, parse, choices, when)


def yes_no(key, prompt, when=None):
    field = choice(key, prompt, {"yes": "Да", "no": "Нет"}, when)
    base = field.parse
    field.parse = lambda value: base(value) == "yes"
    return field


def number(key, prompt, optional=False, when=None):
    def parse(value):
        if optional and value.strip() == "-":
            return None
        try:
            n = int(value)
        except ValueError:
            raise DomainError("Введите положительное целое число") from None
        require(n > 0, "Введите положительное целое число")
        return n

    return Field(key, prompt, parse, when=when)


def date_field(key, prompt, optional=False):
    return Field(
        key,
        prompt,
        lambda value: None if optional and value.strip() == "-" else iso_date(value.strip()),
    )


def is_person(data):
    return data.get("recipientKind") == "person"


def contact_fields():
    return [
        string(
            "contactInput", "Контакт: @telegram, ссылка t.me, телефон или всё вместе (или -)", True
        ),
        number(
            "userId",
            "Telegram ID пока неизвестен. Введите подтверждённый числовой ID или -",
            True,
            lambda d: not d.get("userId"),
        ),
    ]


def recipient_fields(extended=False):
    return [
        choice(
            "recipientKind", "Тип получателя", {"person": "Человек", "organization": "Организация"}
        ),
        string("recipientName", "ФИО или название организации"),
        string(
            "responsible",
            "Ответственный человек организации (или -)",
            True,
            lambda d: extended and not is_person(d),
        ),
        string("group", "Группа (или -)", True, lambda d: extended and is_person(d)),
        number(
            "admissionYear",
            "Год поступления для ежегодного сбора (или -, если выдача не участвует в сборе)",
            True,
            is_person,
        ),
        *contact_fields(),
    ]


def return_fields():
    return [
        string("inventory", "Сверьте и введите инвентарный номер"),
        yes_no("physical", "Роутер физически принят?"),
        string("storageName", "Место хранения"),
        choice("condition", "Результат проверки (без проверки — «Нужна проверка»)", CONDITION),
    ]


def fields(kind, data=None):
    data = data or {}
    reason = string("reason", "Причина / обстоятельства")
    match kind:
        case "add":
            return [
                string("inventory", "Инвентарный номер"),
                string("model", "Модель (или -)", True),
                string("serial", "Серийный номер (или -)", True),
                Field(
                    "mac", "MAC (или -)", lambda v: normalize_mac(None if v.strip() == "-" else v)
                ),
                choice("condition", "Техническое состояние", CONDITION),
                string("storageName", "Место хранения"),
            ]
        case "issue" | "transfer":
            return [
                *recipient_fields(),
                string("placeName", "Место использования: комната или название"),
                date_field("dueAt", "Подтверждённый срок возврата: ГГГГ-ММ-ДД"),
                *([reason] if kind == "transfer" else []),
            ]
        case "return" | "inventory_check":
            return [*return_fields(), reason]
        case "replace":
            return [
                string("newInventory", "Номер НОВОГО роутера"),
                *return_fields(),
                date_field("dueAt", "Срок нового роутера (подтверждённый срок старого сохранится)"),
                reason,
            ]
        case "extend":
            return [date_field("dueAt", "Новый срок возврата"), reason]
        case "lost" | "found":
            return [reason]
        case "resolve_found":
            return [
                choice(
                    "resolution",
                    "Решение по обнаружению",
                    {
                        "holder": "Остаётся у прежнего получателя",
                        "storage": "Физически принят на хранение",
                    },
                ),
                *[
                    Field(
                        f.key,
                        f.prompt,
                        f.parse,
                        f.choices,
                        lambda d: d.get("resolution") == "storage",
                    )
                    for f in return_fields()
                ],
                reason,
            ]
        case "condition":
            return [
                choice("condition", "Результат проверки / состояние ремонта", CONDITION),
                reason,
            ]
        case "write_off":
            return [
                reason,
                yes_no(
                    "resolveOutstanding", "Подтверждаете решение по возможной незакрытой выдаче?"
                ),
            ]
        case "vip":
            return [yes_no("vip", "Включить VIP?"), reason]
        case "exception":
            f = date_field("reviewAt", "Дата пересмотра исключения")
            f.when = lambda d: d.get("enabled")
            return [yes_no("enabled", "Исключить из сбора до даты пересмотра?"), f, reason]
        case "move":
            return [
                string("placeName", "Новое место"),
                string("floor", "Этаж (или -)", True),
                string("section", "Секция (или -)", True),
            ]
        case "note":
            return [string("note", "Новый комментарий (- для очистки)", True)]
        case "correct":
            return [string("eventId", "ID ошибочной операции из истории"), reason]
        case "loan_details":
            recipient = [f for f in recipient_fields(extended=True) if f.key != "recipientName"]
            return [
                *recipient,
                date_field("issuedAt", "Подтверждённая дата выдачи (или -, если неизвестна)", True),
                reason,
            ]
        case "role":
            return [
                number("target", "Числовой Telegram ID сотрудника"),
                choice(
                    "role",
                    "Новая роль",
                    {
                        "admin": "Администратор",
                        "operator": "Оператор",
                        "observer": "Наблюдатель",
                        "revoke": "Отозвать доступ",
                    },
                ),
            ]
        case "campaign":
            return [
                number("admissionYear", "Год поступления набора"),
                date_field("deadline", "Конечный срок"),
                string("responsibleIds", "Числовые ID ответственных через запятую"),
                string("location", "Где и когда принимать роутеры"),
            ]
        case "campaign_edit":
            return [
                string("responsibleIds", "Новые ID ответственных через запятую"),
                date_field("deadline", "Конечный срок"),
                string("location", "Где и когда принимать роутеры"),
                yes_no("active", "Сбор активен? (Нет — приостановить)"),
            ]
        case "campaign_include":
            return [string("loanInventory", "Номер роутера для явного включения"), reason]
        case "loan_report":
            return [date_field("from", "Начало периода"), date_field("to", "Конец периода")]
        case "queue_action":
            actions = {
                "note": "Записать примечание",
                "exclude": "Исключить из сбора (админ)",
                "include": "Вернуть в сбор (админ)",
            }
            return [
                choice("action", "Что сделано?", actions),
                reason,
            ]
        case "queue_filters":
            return [
                string("group", "Группа (или -)", True),
                string("place", "Место (или -)", True),
            ]
        case "filters":
            return [
                choice(
                    "report",
                    "Категория",
                    {
                        "all": "Все",
                        "available": "Доступные",
                        "overdue": "Просроченные",
                        "incomplete": "Неполные данные",
                        "check": "Проверка / ремонт",
                        "pending": "Ожидают таблицу",
                        "conflicts": "Конфликты",
                    },
                ),
                choice(
                    "status",
                    "Статус",
                    {
                        "all": "Любой",
                        "stored": "На хранении",
                        "issued": "Выдан",
                        "lost": "Утерян",
                        "unknown": "Неизвестно",
                        "written_off": "Списан",
                    },
                ),
                choice("condition", "Состояние", {"all": "Любое", **CONDITION}),
                choice("vip", "VIP", {"all": "Любой", "yes": "VIP", "no": "Без VIP"}),
                string("year", "Набор или учебный год (или -)", True),
                *fields("queue_filters"),
            ]
        case "resolve":
            return [reason]
        case "resolve_custom":

            def parser(field):
                def parse(value):
                    if value == "-":
                        return ""
                    if field == "checked":
                        require(value.casefold() in {"true", "false"}, "Введите TRUE или FALSE")
                        return value.casefold() == "true"
                    return value

                return parse

            return [
                *[
                    Field(
                        f,
                        f"Исправленное значение «{FIELD_LABELS.get(f, f)}» (- для очистки)",
                        parser(f),
                    )
                    for f in data.get("customFields", [])
                ],
                reason,
            ]
        case _:
            raise DomainError("Неизвестный диалог")


def contact_from(data):
    if "contactInput" in data:
        value = contact(data["contactInput"])
        value["userId"] = data.get("userId") or value["userId"]
        value["raw"] = contact_text(value)
        return value
    telegram = data.get("telegram", "").lstrip("@")
    return {
        "raw": " ".join(v for v in [data.get("phone"), "@" + telegram if telegram else ""] if v),
        "phone": data.get("phone") or None,
        "telegram": telegram or None,
        "userId": data.get("userId"),
    }


def command(kind, data, inventory, actor):
    d = data
    out = {"kind": kind, **{k: d[k] for k in ("routerId", "expected", "reason") if k in d}}
    if kind == "add":
        return {
            **out,
            **{k: d[k] for k in ("inventory", "model", "serial", "mac", "condition")},
            "storage": place(d["storageName"]),
        }
    if kind in {"issue", "transfer"}:
        return {
            **out,
            "recipient": {
                "kind": d["recipientKind"],
                "name": d["recipientName"],
                "group": d.get("group", ""),
                "admissionYear": d.get("admissionYear"),
                "academicYear": d.get("academicYear", ""),
                "responsible": d.get("responsible", ""),
                "contact": contact_from(d),
            },
            "place": place(d["placeName"]),
            "dueAt": d["dueAt"],
            "collectionEligible": d.get("collectionEligible", bool(d.get("admissionYear"))),
        }
    if kind in {"return", "replace", "resolve_found", "inventory_check"}:
        out.update(
            {k: d[k] for k in ("inventory", "physical", "resolution", "condition") if k in d}
        )
        out["storage"] = place(d.get("storageName", ""))
        if kind == "replace":
            r = inventory.get(actor, d["newInventory"])
            out.update(
                newRouterId=r["id"],
                expected={**d["expected"], r["id"]: r["version"]},
                dueAt=d["dueAt"],
            )
        return out
    if kind == "move":
        return {
            **out,
            "place": place(d["placeName"], floor=d.get("floor", ""), section=d.get("section", "")),
        }
    if kind == "loan_details":
        out.update(
            {
                k: d[k]
                for k in (
                    "recipientKind",
                    "group",
                    "admissionYear",
                    "academicYear",
                    "responsible",
                    "collectionEligible",
                    "issuedAt",
                )
                if k in d
            }
        )
        out["contact"] = contact_from(d)
        out["collectionEligible"] = d.get("collectionEligible", bool(d.get("admissionYear")))
        return out
    out.update(
        {
            k: d[k]
            for k in (
                "condition",
                "dueAt",
                "vip",
                "reviewAt",
                "enabled",
                "note",
                "eventId",
                "resolveOutstanding",
            )
            if k in d
        }
    )
    return out


LABELS = {
    "add": "Добавить",
    "issue": "Выдать",
    "return": "Принять",
    "extend": "Продлить срок",
    "transfer": "Передать",
    "replace": "Заменить",
    "move": "Изменить место",
    "lost": "Утрата",
    "found": "Обнаружен",
    "resolve_found": "Решение по обнаружению",
    "condition": "Проверка / ремонт",
    "write_off": "Списание",
    "vip": "VIP",
    "exception": "Исключение из сбора",
    "note": "Комментарий",
    "correct": "Исправить ошибку",
    "loan_details": "Уточнить выдачу",
    "inventory_check": "Инвентаризация",
    "role": "Доступ",
    "campaign": "Создать кампанию",
    "campaign_edit": "Настроить кампанию",
    "campaign_include": "Включить выдачу",
    "loan_report": "Выдачи за период",
    "queue_action": "Действие в очереди",
    "queue_filters": "Фильтры очереди",
    "filters": "Фильтры",
    "resolve": "Разрешить конфликт",
    "resolve_custom": "Исправить значения",
}
FIELD_LABELS = {
    "inventory": "Инвентарный номер",
    "model": "Модель",
    "serial": "Серийный номер",
    "mac": "MAC",
    "status": "Статус",
    "condition": "Состояние",
    "storageName": "Место хранения",
    "placeName": "Место использования",
    "place": "Место",
    "checked": "Наследуемое Checked",
    "owner": "Получатель",
    "rawContact": "Исходный контакт",
    "checkin": "Наследуемое Checkin",
    "checkout": "Наследуемое Checkout",
    "note": "Комментарий",
    "recipientKind": "Тип получателя",
    "recipientName": "Получатель",
    "responsible": "Ответственный от организации",
    "group": "Группа",
    "admissionYear": "Год поступления",
    "academicYear": "Учебный год",
    "collectionEligible": "Участие в сборе набора",
    "phone": "Телефон",
    "telegram": "Telegram",
    "userId": "Подтверждённый Telegram ID",
    "contactInput": "Контакт",
    "dueAt": "Срок возврата",
    "issuedAt": "Дата выдачи",
    "reason": "Причина",
    "physical": "Физически принят",
    "newInventory": "Номер нового роутера",
    "resolution": "Решение",
    "vip": "VIP",
    "enabled": "Исключение включено",
    "reviewAt": "Пересмотр исключения",
    "eventId": "ID исходной операции",
    "resolveOutstanding": "Решение по незакрытой выдаче подтверждено",
    "target": "Telegram ID сотрудника",
    "role": "Роль",
    "name": "Название",
    "startsAt": "Начало",
    "deadline": "Конечный срок",
    "responsibleIds": "ID ответственных",
    "location": "Место приёма",
    "hours": "Часы работы",
    "instruction": "Инструкция",
    "action": "Действие",
    "operatorId": "Назначенный оператор",
    "priority": "Порядок",
    "source": "Источник решения",
    "active": "Кампания активна",
    "floor": "Этаж",
    "section": "Секция",
    "loanInventory": "Роутер",
}
