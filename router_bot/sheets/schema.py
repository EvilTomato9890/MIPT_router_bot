from ..service import place
from ..util import clone, contact, contact_text, equal, normalize_mac, overdue, require

HEADERS = [
    "Inv. №",
    " MAC Address",
    "Place",
    "Status",
    "Checked",
    "Owner",
    "Checkin ",
    "Checkout",
    "Сontacts",
    "Комментарий",
]
FIELDS = [
    "inventory",
    "mac",
    "place",
    "status",
    "checked",
    "owner",
    "checkin",
    "checkout",
    "rawContact",
    "note",
]
LINKED = {"status", "owner", "place"}
EXTERNAL = {
    "stored": "Свободен",
    "issued": "Выдан",
    "lost": "Утерян",
    "unknown": "Неизвестно",
    "written_off": "Списан",
}
INTERNAL = {
    "Свободен": "stored",
    "Выдан": "issued",
    "Просрочен": "issued",
    "Утерян": "lost",
    "?": "unknown",
    "Неизвестно": "unknown",
    "Списан": "written_off",
}


def row_values(cells):
    values = {
        field: cells[i] if i < len(cells) and cells[i] is not None else ""
        for i, field in enumerate(FIELDS)
    }
    if isinstance(values["checked"], str) and values["checked"].casefold() in {"true", "false"}:
        values["checked"] = values["checked"].casefold() == "true"
    return values


def validate_sheet(sheet):
    require(sheet.get("title") and type(sheet.get("sheetId")) is int, "Неизвестный лист", "schema")
    require(
        sheet.get("headers", [])[:10] == HEADERS,
        "Структура A:J изменилась. Запись остановлена.",
        "schema",
    )


def projection(router, loan, now):
    recipient = (
        loan["recipient"]
        if loan
        else {
            "name": router.get("legacy", {}).get("owner", ""),
            "contact": router.get("legacy", {}).get("contact"),
        }
    )
    loc = (loan or router).get("place" if loan else "storage") or {}
    legacy = router.get("legacy", {})
    status = EXTERNAL[router["status"]]
    if router["status"] == "unknown" and legacy.get("status") == "?":
        status = "?"
    if router["status"] == "issued" and (
        overdue(loan, now) or (not loan.get("dueConfirmed") and legacy.get("status") == "Просрочен")
    ):
        status = "Просрочен"
    return {
        "inventory": router["inventory"],
        "mac": router.get("mac") or "",
        "place": loc.get("name") or "",
        "status": status,
        "checked": legacy.get("checked") if legacy.get("checked") is not None else "",
        "owner": recipient.get("name") or "",
        "checkin": legacy.get("checkin") if legacy.get("checkin") is not None else "",
        "checkout": legacy.get("checkout") if legacy.get("checkout") is not None else "",
        "rawContact": contact_text(recipient.get("contact")),
        "note": router.get("note") or "",
    }


def validate_flat(values):
    require(str(values.get("inventory", "")).strip(), "Пустой инвентарный номер")
    require(str(values.get("status", "")).strip() in INTERNAL, "Неизвестный статус в таблице")
    normalize_mac(values.get("mac"))
    require(
        values.get("checked") in (None, "") or type(values["checked"]) is bool,
        "Checked: TRUE, FALSE или пусто",
    )


def decode(values):
    validate_flat(values)
    status = INTERNAL[str(values["status"]).strip()]
    location = str(values.get("place") or "")
    certainty = (
        "absent"
        if not location
        else "unknown"
        if location.strip().casefold() in {"?", "неизвестно"}
        else "legacy"
    )
    raw = contact(values.get("rawContact", ""))
    owner = str(values.get("owner") or "")
    kind = "organization" if owner.strip() == "Студсовет ФПМИ" else "unknown"
    return {
        "inventory": str(values["inventory"]).strip(),
        "mac": normalize_mac(values.get("mac")),
        "status": status,
        "storage": place(location, certainty),
        "place": place(location, certainty),
        "recipient": {
            "id": None,
            "kind": kind,
            "name": owner,
            "contact": raw,
            "group": "",
            "admissionYear": None,
            "academicYear": "",
            "responsible": "",
        },
        "issuedAt": None,
        "dueAt": None,
        "dueConfirmed": False,
        "collectionEligible": False,
        "note": str(values.get("note") or ""),
        "legacy": {
            "checked": values.get("checked") if values.get("checked") != "" else None,
            "checkin": values.get("checkin", ""),
            "checkout": values.get("checkout", ""),
            "status": str(values["status"]).strip(),
            "owner": owner if status not in {"issued", "lost"} else "",
            "contact": raw if status not in {"issued", "lost"} else None,
        },
    }


def merge_three(base, sheet, bot):
    merged, conflicts, changes = clone(bot), [], []
    remote_linked, local_linked = False, False
    for field in sheet:
        a, b, c = base.get(field, ""), sheet[field], bot.get(field, "")
        remote, local = not equal(a, b), not equal(a, c)
        if remote:
            changes.append({"field": field, "before": a, "sheet": b, "bot": c})
            remote_linked |= field in LINKED
        local_linked |= local and field in LINKED
        if remote and local and not equal(b, c):
            conflicts.append(field)
        elif remote:
            merged[field] = b
    if (
        remote_linked
        and local_linked
        and any(f in sheet and not equal(sheet[f], bot[f]) for f in LINKED)
    ):
        conflicts.append("связанные поля выдачи")
    return {"merged": merged, "conflicts": list(dict.fromkeys(conflicts)), "changes": changes}
