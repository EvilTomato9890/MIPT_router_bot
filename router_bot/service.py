"""Domain operations. No Telegram or Sheets calls are made inside transactions."""

import json
import unicodedata
from datetime import timedelta

from .contacts import normalize as normalize_contact
from .util import (
    CONDITION,
    STATUS,
    clone,
    iso_date,
    local_clock,
    natural,
    normalize_mac,
    normalize_serial,
    now_utc,
    optional,
    overdue,
    phone_number,
    require,
    stable,
    stamp,
    text,
    uuid,
    valid_id,
)

ADMIN_ACTIONS = {
    "vip",
    "extend",
    "write_off",
    "exception",
    "correct",
    "resolve_found",
    "loan_details",
    "inventory_check",
}


def place(name="", certainty="confirmed", floor="", section=""):
    return {"name": name, "certainty": certainty, "floor": floor, "section": section}


class Inventory:
    def __init__(self, store, *, now=now_utc, bootstrap_admin_id=None):
        self.s, self.now = store, now
        if bootstrap_admin_id and not store.setting("bootstrapped"):
            require(valid_id(bootstrap_admin_id), "Неверный ID администратора")
            with store.transaction():
                store.run("INSERT OR IGNORE INTO roles VALUES(?,?)", bootstrap_admin_id, "admin")
                store.set("bootstrapped", True)

    def authorize(self, actor, minimum="observer"):
        require(valid_id(actor), "Нет доступа", "forbidden")
        row = self.s.get("SELECT role FROM roles WHERE user_id=?", actor)
        ranks = {"observer": 0, "operator": 1, "admin": 2}
        require(row and ranks[row["role"]] >= ranks[minimum], "Нет доступа", "forbidden")
        return row["role"]

    def set_role(self, actor, target, role, key):
        self.authorize(actor, "admin")
        require(valid_id(target), "Укажите постоянный числовой Telegram ID")
        require(role in (None, "admin", "operator", "observer"), "Неизвестная роль")

        def operation(_):
            self.authorize(actor, "admin")
            old = self.s.get("SELECT role FROM roles WHERE user_id=?", target)
            require(
                not (
                    old
                    and old["role"] == "admin"
                    and role != "admin"
                    and self.s.get("SELECT count(*) AS n FROM roles WHERE role='admin'")["n"] <= 1
                ),
                "Нельзя удалить последнего администратора",
            )
            self.s.run("DELETE FROM roles WHERE user_id=?", target)
            if role:
                self.s.run("INSERT INTO roles VALUES(?,?)", target, role)
            self.s.run("DELETE FROM sessions WHERE user_id=?", target)
            self.s.cancel_drafts(target)
            self.s.event(
                actor, str(target), "Изменение доступа", old["role"] if old else None, role
            )
            return {"target": target, "role": role}

        return self.s.atomic(actor, key, {"target": target, "role": role}, operation)

    def roles(self, actor):
        self.authorize(actor, "admin")
        return self.s.all("SELECT * FROM roles ORDER BY user_id")

    def _router(self, identifier):
        row = self.s.get(
            "SELECT data FROM routers WHERE id=? OR inventory=?", identifier, identifier
        )
        require(row, "Роутер не найден")
        return json.loads(row["data"])

    def _loan(self, router_id):
        row = self.s.get("SELECT data FROM loans WHERE router_id=? AND open=1", router_id)
        return json.loads(row["data"]) if row else None

    def _loans(self, router_id):
        return [
            json.loads(row["data"])
            for row in self.s.all(
                "SELECT data FROM loans WHERE router_id=? ORDER BY rowid", router_id
            )
        ]

    def get(self, actor, identifier):
        self.authorize(actor)
        router = self._router(identifier)
        return {**router, "loan": self._loan(router["id"])}

    def loans(self, actor, identifier):
        self.authorize(actor)
        return self._loans(self._router(identifier)["id"])

    def history(self, actor, identifier):
        self.authorize(actor)
        return self.s.all(
            "SELECT * FROM events WHERE object_id=? ORDER BY rowid", self._router(identifier)["id"]
        )

    def list(self, actor, filters=None):
        self.authorize(actor)
        filters = filters or {}
        result = []
        for row in self.s.all("SELECT data FROM routers"):
            r = json.loads(row["data"])
            loan = self._loan(r["id"])
            p = loan["recipient"] if loan else {}
            loc = (loan or r).get("place" if loan else "storage") or {}
            query = str(filters.get("q", "")).casefold()
            values = [
                r["inventory"],
                r.get("serial"),
                r.get("model"),
                r.get("mac"),
                (r.get("mac") or "").replace(":", ""),
                p.get("name"),
                p.get("group"),
                loc.get("name"),
            ]
            if query and not any(query in str(v or "").casefold() for v in values):
                continue
            if any(filters.get(k) and r[k] != filters[k] for k in ("status", "condition")):
                continue
            if "vip" in filters and r["vip"] is not filters["vip"]:
                continue
            if filters.get("year") and str(p.get("admissionYear") or p.get("academicYear")) != str(
                filters["year"]
            ):
                continue
            if filters.get("group") and p.get("group") != filters["group"]:
                continue
            if filters.get("place") and filters["place"] not in loc.get("name", ""):
                continue
            if filters.get("operator") and (loan or {}).get("assignedOperator") != int(
                filters["operator"]
            ):
                continue
            report = filters.get("report")
            if report in {"undefined_due", "unknown_holder", "unknown_cohort"}:
                report = "incomplete"
            predicates = {
                "available": r["status"] == "stored" and r["condition"] == "working",
                "overdue": overdue(loan, self.now()),
                "incomplete": r["status"] == "unknown"
                or bool(
                    loan
                    and (
                        not loan.get("dueConfirmed")
                        or not loan.get("dueAt")
                        or not p.get("name")
                        or p.get("kind") == "unknown"
                        or not (
                            (p.get("contact") or {}).get("phone")
                            or valid_id((p.get("contact") or {}).get("userId"))
                        )
                        or (
                            p.get("kind") == "person"
                            and loan.get("collectionEligible") is True
                            and not p.get("admissionYear")
                            and not p.get("academicYear")
                        )
                    )
                ),
                "check": r["condition"] != "working",
            }
            if report in ("pending", "conflicts"):
                table = "outbox" if report == "pending" else "conflicts"
                if not self.s.get(f"SELECT 1 FROM {table} WHERE router_id=?", r["id"]):
                    continue
            elif report in predicates and not predicates[report]:
                continue
            result.append({**r, "loan": loan})
        return sorted(result, key=lambda r: natural(r["inventory"]))

    def warnings(self, actor, recipient, use_place):
        matches = []
        for r in self.list(actor, {"status": "issued"}):
            p, loc = r["loan"]["recipient"], r["loan"]["place"]
            user_id = (recipient.get("contact") or {}).get("userId")
            if (
                (recipient.get("id") and p["id"] == recipient["id"])
                or (user_id and (p.get("contact") or {}).get("userId") == user_id)
                or (p.get("name") and p["name"] == recipient.get("name"))
                or (use_place.get("name") and use_place["name"] == loc["name"])
            ):
                matches.append(
                    f"{r['inventory']}: {p.get('name') or 'неизвестно'}, {loc.get('name') or 'место не определено'}"
                )
        return matches

    def _queue(self, r, group):
        previous = self.s.get("SELECT group_id FROM outbox WHERE router_id=?", r["id"])
        if previous:
            self.s.run("UPDATE outbox SET group_id=? WHERE group_id=?", group, previous["group_id"])
        at = stamp(self.now())
        self.s.run(
            "INSERT INTO outbox VALUES(?,?,?,'pending',0,?,?,NULL) ON CONFLICT(router_id) DO UPDATE SET version=excluded.version,group_id=excluded.group_id,state='pending',next_at=excluded.next_at",
            r["id"],
            group,
            r["version"],
            at,
            at,
        )

    def _save(self, r, group):
        others = self.s.all(
            "SELECT id,inventory,serial_norm,mac_norm FROM routers WHERE id<>?", r["id"]
        )
        inv = unicodedata.normalize("NFKC", r["inventory"]).upper()
        require(
            not any(
                unicodedata.normalize("NFKC", row["inventory"]).upper() == inv for row in others
            ),
            "Инвентарный номер уже существует",
        )
        for field, value in (
            ("serial_norm", normalize_serial(r.get("serial"))),
            ("mac_norm", normalize_mac(r.get("mac"))),
        ):
            same = next((x for x in others if value and x[field] == value), None)
            require(
                not same,
                f"Возможный дубликат MAC / серийного номера: {(same or {}).get('inventory', '')}",
            )
        r["version"] += 1
        r["updatedAt"] = stamp(self.now())
        self.s.run(
            "INSERT INTO routers VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET inventory=excluded.inventory,serial_norm=excluded.serial_norm,mac_norm=excluded.mac_norm,status=excluded.status,condition=excluded.condition,version=excluded.version,data=excluded.data",
            r["id"],
            r["inventory"],
            normalize_serial(r.get("serial")),
            normalize_mac(r.get("mac")),
            r["status"],
            r["condition"],
            r["version"],
            stable(r),
        )
        self._queue(r, group)

    def _put_loan(self, loan):
        loan.pop("kitOut", None)
        loan.pop("kitIn", None)
        if loan.get("place"):
            loan["place"].pop("type", None)
        self.s.run(
            "INSERT INTO loans VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET open=excluded.open,data=excluded.data",
            loan["id"],
            loan["routerId"],
            int(loan["open"]),
            stable(loan),
        )

    def _new(self, data):
        at, inv = stamp(self.now()), text(data.get("inventory", ""), 100)
        require(inv, "Укажите инвентарный номер")
        return {
            "id": data.get("id") or uuid(),
            "inventory": inv,
            "model": optional(data.get("model")),
            "serial": optional(data.get("serial")),
            "mac": normalize_mac(data.get("mac")),
            "status": "stored",
            "condition": data.get("condition") or "needs_check",
            "vip": False,
            "vipComment": "",
            "storage": clone(data.get("storage") or place("", "absent")),
            "note": text(data.get("note", "")),
            "legacy": {},
            "createdAt": at,
            "updatedAt": at,
            "version": 0,
        }

    def _recipient(self, p, legacy=False):
        require(
            p
            and p.get("kind")
            in ({"person", "organization", "unknown"} if legacy else {"person", "organization"}),
            "Выберите тип получателя",
        )
        r = clone(p)
        r["id"] = r.get("id") or uuid()
        r["name"] = text(r.get("name", ""))
        require(legacy or r["name"], "Укажите получателя")
        if r["kind"] == "organization":
            require(
                not r.get("admissionYear") and not r.get("academicYear"),
                "Для организации набор студентов неприменим",
            )
        if r.get("admissionYear"):
            require(
                type(r["admissionYear"]) is int and 1990 <= r["admissionYear"] <= 2200,
                "Неверный год поступления",
            )
        if (r.get("contact") or {}).get("userId"):
            require(valid_id(r["contact"]["userId"]), "Неверный подтверждённый Telegram ID")
        if r.get("contact"):
            r["contact"] = normalize_contact(r["contact"])
        if not legacy:
            details = r.get("contact") or {}
            method = details.get("method") or (
                "telegram_id"
                if valid_id(details.get("userId")) and not details.get("phone")
                else "phone"
            )
            require(method in {"phone", "telegram_id"}, "Выберите телефон или Telegram ID")
            if method == "phone":
                phone_number(details.get("phone"))
            else:
                require(
                    valid_id(details.get("userId")), "Укажите числовой Telegram ID вместо телефона"
                )
        return r

    def _validate(self, r):
        require(
            r["status"] in STATUS and r["condition"] in CONDITION,
            "Неизвестный статус или состояние",
        )
        require(r["id"] and r["inventory"], "Пустой номер или ID")
        normalize_mac(r.get("mac"))
        loc = r.get("storage")
        if loc:
            require(
                "type" not in loc
                and loc.get("certainty") in {"absent", "unknown", "legacy", "confirmed"},
                "Неверное место",
            )
        loan = self._loan(r["id"])
        require(
            not (r["status"] == "issued" and not loan),
            "Выданный роутер должен иметь действующую выдачу",
        )
        require(
            not (r["status"] in {"stored", "unknown", "written_off"} and loan),
            "Статус несовместим с действующей выдачей",
        )

    def _issue(self, r, d, actor, *, legacy=False, issued_at=None, linked_loan_id=None):
        require(
            r["status"] == "stored" and (legacy or r["condition"] == "working"),
            "Выдать можно только свободный исправный роутер",
        )
        require(not self._loan(r["id"]), "У роутера уже есть действующая выдача")
        recipient = self._recipient(d.get("recipient"), legacy)
        require(legacy or (d.get("place") or {}).get("name"), "Укажите место использования")
        loan = {
            "id": uuid(),
            "routerId": r["id"],
            "open": True,
            "recipient": recipient,
            "place": clone(d.get("place") or place("", "absent")),
            "issuedAt": issued_at
            if legacy
            else iso_date(d.get("issuedAt") or local_clock(self.now())["day"]),
            "dueAt": iso_date(d.get("dueAt"), True),
            "dueConfirmed": bool(d.get("dueConfirmed")) if legacy else True,
            "issuer": None if legacy else actor,
            "conditionOut": None if legacy else r["condition"],
            "returnedAt": None,
            "receiver": None,
            "conditionIn": None,
            "closedKind": None,
            "linkedLoanId": linked_loan_id,
            "assignedOperator": d.get("assignedOperator") or actor,
            "exception": None,
            "collectionEligible": bool(d.get("collectionEligible", False))
            and recipient["kind"] == "person",
            "incomplete": ["legacy"] if legacy else [],
        }
        require(legacy or loan["dueAt"], "Укажите подтверждённый срок")
        if loan["dueAt"] and loan["issuedAt"]:
            inherited = (
                self.s.get("SELECT data FROM loans WHERE id=?", linked_loan_id)
                if linked_loan_id
                else None
            )
            previous = json.loads(inherited["data"]) if inherited else {}
            require(
                loan["dueAt"] >= loan["issuedAt"]
                or (
                    previous.get("closedKind") == "replacement"
                    and previous.get("dueConfirmed")
                    and previous.get("dueAt") == loan["dueAt"]
                ),
                "Срок раньше даты выдачи",
            )
        self._put_loan(loan)
        r.update(status="issued", storage=None)
        return loan

    def _return(self, r, d, actor, *, closed_kind="return"):
        loan = self._loan(r["id"])
        require(loan, "Нет действующей выдачи")
        require(d.get("physical") is True, "Подтвердите физический приём роутера")
        require(d.get("inventory") == r["inventory"], "Сверьте инвентарный номер")
        require((d.get("storage") or {}).get("name"), "Укажите место хранения")
        loan.update(
            open=False,
            returnedAt=iso_date(d.get("returnedAt") or local_clock(self.now())["day"]),
            receiver=actor,
            conditionIn=d.get("condition") or "needs_check",
            closedKind=closed_kind,
        )
        self._put_loan(loan)
        r.update(status="stored", condition=loan["conditionIn"], storage=clone(d["storage"]))
        r["legacy"].update(owner="", contact=None)
        return loan

    def _bundle(self, r):
        return {"router": clone(r), "loans": self._loans(r["id"])}

    def _correction_events(self, event):
        group = self.s.get("SELECT operation_id FROM event_groups WHERE event_id=?", event["id"])
        return (
            self.s.all(
                "SELECT e.* FROM events e JOIN event_groups g ON e.id=g.event_id WHERE g.operation_id=?",
                group["operation_id"],
            )
            if group
            else [event]
        )

    def _restore(self, r, event, at):
        latest = self.s.get(
            "SELECT id FROM events WHERE object_id=? ORDER BY rowid DESC LIMIT 1", r["id"]
        )
        require(latest["id"] == event["id"], "Есть зависимые события: слепой откат запрещён")
        require(
            event["action"] != "add",
            "Добавление сохраняется в истории; ошибочную карточку спишите с причиной",
        )
        snapshot = self.s.get("SELECT before_json FROM event_states WHERE event_id=?", event["id"])
        prior = json.loads((snapshot or event)["before_json"])
        require(prior and prior.get("router"), "Нет предыдущего состояния")
        ids = {loan["id"] for loan in prior["loans"]}
        for loan in self._loans(r["id"]):
            if loan["id"] not in ids:
                loan.update(open=False, closedKind="correction", closedAt=at)
                self._put_loan(loan)
        for loan in prior["loans"]:
            current = self.s.get("SELECT data FROM loans WHERE id=?", loan["id"])
            saved = json.loads(current["data"]) if current else {}
            # Old audit snapshots keep the old model; restored live records do not.
            loan.pop("kitOut", None)
            loan.pop("kitIn", None)
            if loan.get("place"):
                loan["place"].pop("type", None)
            loan.setdefault("collectionEligible", saved.get("collectionEligible", False))
            if saved.get("contactsPurgedAt"):
                loan["recipient"]["contact"] = None
                loan["contactsPurgedAt"] = saved["contactsPurgedAt"]
            elif (
                not snapshot
                and "contact" not in loan["recipient"]
                and (saved.get("recipient") or {}).get("contact")
            ):
                loan["recipient"]["contact"] = saved["recipient"]["contact"]
            self._put_loan(loan)
        if not snapshot and "contact" not in prior["router"].get("legacy", {}):
            prior["router"].setdefault("legacy", {})["contact"] = r.get("legacy", {}).get("contact")
        restored = {**prior["router"], "version": r["version"]}
        restored.pop("kit", None)
        if restored.get("storage"):
            restored["storage"].pop("type", None)
        return restored

    def execute(self, actor, key, command):
        minimum = "admin" if command["kind"] in ADMIN_ACTIONS else "operator"
        self.authorize(actor, minimum)

        def operation(group):
            self.authorize(actor, minimum)
            d, at = clone(command), stamp(self.now())
            for identifier, version in d.get("expected", {}).items():
                require(
                    self._router(identifier)["version"] == version,
                    "Карточка изменилась. Сверьте её и начните операцию заново (/cancel).",
                    "stale",
                )
            r = self._new(d) if d["kind"] == "add" else self._router(d["routerId"])
            before, other, extra_before = self._bundle(r), None, None
            extra_correction = None
            reason, correction = text(d["reason"]) if d.get("reason") else None, None
            match d["kind"]:
                case "add":
                    require(
                        not self.s.get("SELECT 1 FROM routers WHERE id=?", r["id"]),
                        "ID уже существует",
                    )
                case "issue":
                    self._issue(r, d, actor)
                case "return":
                    self._return(r, d, actor)
                case "extend":
                    require(reason, "Укажите причину продления")
                    loan = self._loan(r["id"])
                    require(loan, "Нет действующей выдачи")
                    due = iso_date(d.get("dueAt"))
                    require(
                        not loan["dueAt"] or due > loan["dueAt"],
                        "Новый срок должен быть позже предыдущего",
                    )
                    loan.update(dueAt=due, dueConfirmed=True)
                    self._put_loan(loan)
                case "transfer":
                    require(reason, "Укажите причину передачи")
                    require(
                        r["status"] == "issued" and r["condition"] == "working",
                        "Передавать можно исправный выданный роутер",
                    )
                    loan = self._loan(r["id"])
                    loan.update(open=False, closedKind="transfer", closedAt=at, receiver=actor)
                    self._put_loan(loan)
                    r["status"] = "stored"
                    new = self._issue(
                        r,
                        d,
                        actor,
                        legacy=True,
                        issued_at=iso_date(d.get("issuedAt") or local_clock(self.now())["day"]),
                        linked_loan_id=loan["id"],
                    )
                    new["recipient"] = self._recipient(d["recipient"])
                    require(
                        new["dueAt"] and d["place"].get("name"), "Для передачи нужны срок и место"
                    )
                    new.update(
                        dueConfirmed=True, issuer=actor, incomplete=[], conditionOut=r["condition"]
                    )
                    self._put_loan(new)
                    loan["linkedLoanId"] = new["id"]
                    self._put_loan(loan)
                case "replace":
                    require(reason, "Укажите причину замены")
                    other = self._router(d["newRouterId"])
                    require(other["id"] != r["id"], "Нужен другой роутер")
                    extra_before, old = self._bundle(other), self._loan(r["id"])
                    require(old, "Нет действующей выдачи")
                    require(
                        other["status"] == "stored"
                        and other["condition"] == "working"
                        and not self._loan(other["id"]),
                        "Новый роутер недоступен",
                    )
                    closed = self._return(r, d, actor, closed_kind="replacement")
                    new = self._issue(
                        other,
                        {
                            **d,
                            "recipient": old["recipient"],
                            "place": old["place"],
                            "dueAt": old["dueAt"]
                            if old.get("dueConfirmed") and old.get("dueAt")
                            else d.get("dueAt"),
                            "collectionEligible": old.get("collectionEligible", False),
                        },
                        actor,
                        linked_loan_id=closed["id"],
                    )
                    closed["linkedLoanId"] = new["id"]
                    self._put_loan(closed)
                case "move":
                    require((d.get("place") or {}).get("name"), "Укажите место")
                    loan = self._loan(r["id"])
                    if loan:
                        loan["place"] = clone(d["place"])
                        self._put_loan(loan)
                    else:
                        r["storage"] = clone(d["place"])
                case "lost":
                    require(reason, "Укажите обстоятельства утраты")
                    require(
                        r["status"] in {"issued", "stored", "unknown"},
                        "Утрата уже отмечена или оборудование списано",
                    )
                    r.update(status="lost", loss={"reason": reason, "at": at, "actor": actor})
                    r.pop("found", None)
                case "found":
                    require(
                        r["status"] == "lost" and reason,
                        "Укажите, где и у кого обнаружен утерянный роутер",
                    )
                    r["found"] = {"reason": reason, "at": at, "actor": actor}
                case "resolve_found":
                    require(
                        r["status"] == "lost" and r.get("found") and reason,
                        "Сначала зафиксируйте обнаружение и причину решения",
                    )
                    require(
                        r["found"].get("at")
                        and r["found"]["at"] >= (r.get("loss") or {}).get("at", ""),
                        "Обнаружение относится к прежней утрате. Зафиксируйте новое обнаружение.",
                    )
                    if d.get("resolution") == "holder":
                        require(self._loan(r["id"]), "Нет прежней выдачи")
                        r["status"] = "issued"
                    elif self._loan(r["id"]):
                        self._return(r, d, actor)
                    else:
                        require(
                            d.get("physical") is True
                            and d.get("inventory") == r["inventory"]
                            and (d.get("storage") or {}).get("name"),
                            "Нужны физический приём, номер и место",
                        )
                        r.update(
                            status="stored",
                            condition=d.get("condition") or "needs_check",
                            storage=clone(d["storage"]),
                        )
                case "condition":
                    require(
                        reason and d.get("condition") in CONDITION,
                        "Укажите состояние и результат проверки",
                    )
                    r["condition"] = d["condition"]
                case "write_off":
                    require(reason, "Укажите причину списания")
                    loan = self._loan(r["id"])
                    if loan:
                        require(
                            d.get("resolveOutstanding") is True,
                            "Отдельно подтвердите решение по незакрытой выдаче",
                        )
                        loan.update(open=False, closedKind="written_off", closedAt=at)
                        self._put_loan(loan)
                    r["status"] = "written_off"
                case "vip":
                    require(reason and type(d.get("vip")) is bool, "Укажите VIP и смысл отметки")
                    r.update(vip=d["vip"], vipComment=reason)
                case "exception":
                    loan = self._loan(r["id"])
                    require(loan and reason, "Нет выдачи или не указана причина")
                    loan["exception"] = (
                        None
                        if d.get("enabled") is False
                        else {
                            "reason": reason,
                            "reviewAt": iso_date(d.get("reviewAt")),
                            "actor": actor,
                        }
                    )
                    self._put_loan(loan)
                case "note":
                    r["note"] = text(d.get("note", ""))
                case "inventory_check":
                    require(
                        r["status"] == "unknown" and not self._loan(r["id"]),
                        "Инвентаризация доступна для неизвестного состояния без выдачи",
                    )
                    require(
                        d.get("physical") is True
                        and d.get("inventory") == r["inventory"]
                        and (d.get("storage") or {}).get("name")
                        and reason,
                        "Подтвердите номер, физическое наличие, место и причину",
                    )
                    r.update(
                        status="stored",
                        condition=d.get("condition") or "needs_check",
                        storage=clone(d["storage"]),
                    )
                    r["legacy"].update(owner="", contact=None)
                case "loan_details":
                    loan = self._loan(r["id"])
                    require(loan and reason, "Нужны действующая выдача и причина уточнения")
                    p = {
                        **loan["recipient"],
                        "kind": d["recipientKind"],
                        "group": d.get("group", ""),
                        "admissionYear": d.get("admissionYear"),
                        "academicYear": d.get("academicYear", ""),
                        "responsible": d.get("responsible", ""),
                        "contact": d["contact"],
                    }
                    loan["recipient"] = self._recipient(p)
                    loan["issuedAt"] = iso_date(d.get("issuedAt"), True)
                    loan["collectionEligible"] = (
                        bool(d.get("collectionEligible")) and p["kind"] == "person"
                    )
                    if loan.get("issuedAt") and loan.get("dueAt"):
                        require(loan["dueAt"] >= loan["issuedAt"], "Срок раньше даты выдачи")
                    self._put_loan(loan)
                case "correct":
                    require(reason, "Укажите причину исправления")
                    event = self.s.get(
                        "SELECT * FROM events WHERE id=? AND object_id=?", d.get("eventId"), r["id"]
                    )
                    require(event, "Исходная операция не найдена")
                    related = self._correction_events(event)
                    require(
                        event["action"] != "replace" or len(related) == 2,
                        "У старой замены нет связанных снимков; исправьте её предметными действиями",
                    )
                    require(len(related) <= 2, "Связанная операция требует отдельного разбора")
                    for linked in related:
                        if linked["object_id"] != r["id"]:
                            other = self._router(linked["object_id"])
                            extra_before = self._bundle(other)
                            other = self._restore(other, linked, at)
                            extra_correction = linked["id"]
                    r = self._restore(r, event, at)
                    correction = event["id"]
                case _:
                    require(False, "Неизвестная операция")
            self._validate(r)
            self._save(r, group)
            event_id = self.s.event(
                actor,
                r["id"],
                d["kind"],
                before,
                self._bundle(r),
                reason,
                correction,
                at,
                operation_id=group,
            )
            if other:
                self._validate(other)
                self._save(other, group)
                self.s.event(
                    actor,
                    other["id"],
                    d["kind"],
                    extra_before,
                    self._bundle(other),
                    reason,
                    correction_of=extra_correction,
                    at=at,
                    operation_id=group,
                )
            return {
                "routerId": r["id"],
                "version": r["version"],
                "eventId": event_id,
                "sheets": "pending",
            }

        return self.s.atomic(actor, key, command, operation)

    def purge_contacts(self, actor, days, key):
        self.authorize(actor, "admin")
        require(type(days) is int and days > 0, "Срок хранения должен быть положительным")

        def operation(_):
            self.authorize(actor, "admin")
            cutoff, count = (self.now() - timedelta(days=days)).date().isoformat(), 0
            for row in self.s.all("SELECT data FROM loans WHERE open=0"):
                loan = json.loads(row["data"])
                closed = loan.get("returnedAt") or loan.get("closedAt")
                if closed and closed[:10] < cutoff and loan["recipient"].get("contact"):
                    loan["recipient"]["contact"] = None
                    loan["contactsPurgedAt"] = stamp(self.now())
                    self._put_loan(loan)
                    for state in self.s.all("SELECT event_id,before_json FROM event_states"):
                        before = json.loads(state["before_json"])
                        for previous in before.get("loans", []):
                            if previous["id"] == loan["id"]:
                                previous["recipient"]["contact"] = None
                                previous["contactsPurgedAt"] = loan["contactsPurgedAt"]
                        self.s.run(
                            "UPDATE event_states SET before_json=? WHERE event_id=?",
                            stable(before),
                            state["event_id"],
                        )
                    count += 1
            self.s.event(actor, "privacy", "Удаление контактов", None, {"count": count})
            return {"count": count}

        return self.s.atomic(actor, key, {"purge": days}, operation)

    def stats(self, actor):
        rows = self.list(actor)
        return {
            "physical": len(rows),
            "statuses": {s: sum(r["status"] == s for r in rows) for s in STATUS},
            "vip": sum(r["vip"] for r in rows),
            "loans": self.s.get("SELECT count(*) AS n FROM loans")["n"],
        }
