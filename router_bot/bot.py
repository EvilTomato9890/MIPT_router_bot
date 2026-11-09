"""Private-chat Telegram interface. Rights are rechecked on every view and commit."""

import json

from .delivery import Delivery
from .dialogs import FIELD_LABELS, LABELS, command, contact_from, fields
from .service import ADMIN_ACTIONS
from .sheets.schema import LINKED
from .util import (
    CONDITION,
    STATUS,
    DomainError,
    csv_cell,
    equal,
    local_clock,
    overdue,
    phone_number,
    require,
    stable,
    stamp,
    uuid,
    valid_id,
)

READ_WIZARDS = {"filters", "queue_filters"}
CONTACT_WIZARDS = {"issue", "transfer", "loan_details"}
ADMIN_WIZARDS = ADMIN_ACTIONS | {
    "role",
    "campaign",
    "campaign_edit",
    "campaign_include",
    "resolve",
    "resolve_custom",
}
VALUES = {
    **STATUS,
    **CONDITION,
    "person": "Человек",
    "organization": "Организация",
    "admin": "Администратор",
    "operator": "Оператор",
    "observer": "Наблюдатель",
    "revoke": "Отозвать доступ",
    "holder": "У прежнего получателя",
    "storage": "Принят на хранение",
    "sheet": "Таблица",
    "bot": "Бот",
    "custom": "Исправление",
    "legacy": "Наследуемая запись, не подтверждено",
    "confirmed": "Подтверждено",
    "absent": "Не указано",
    "not_home": "Не застали",
    "contacted": "Связались",
    "new_date": "Другая дата предложена",
    "problem": "Проблема",
    "next": "Следующий",
    "include": "Включить",
    "exclude": "Исключить",
    "assign": "Назначить оператора",
    "priority": "Порядок",
    "phone": "Телефон",
    "telegram_id": "Telegram ID",
}


def display(value):
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return ", ".join(display(v) for v in value)
    if isinstance(value, dict):
        return stable(value)
    return VALUES.get(value, str(value))


def button(label, callback):
    require(len(callback.encode("utf-8")) <= 64, "Недопустимая длина кнопки")
    return {"text": label, "callback_data": callback}


def rows(items, size=2):
    return [items[i : i + size] for i in range(0, len(items), size)]


class Bot:
    def __init__(self, inventory, telegram, *, sync=None, colors=None, campaigns, mode="disabled"):
        self.i, self.s, self.t = inventory, inventory.s, Delivery(inventory, telegram)
        self.sync, self.colors, self.c, self.mode = sync, colors, campaigns, mode

    def send(self, actor, message, buttons=None):
        self.i.authorize(actor)
        markup = {"inline_keyboard": [row for row in buttons if row]} if buttons else None
        return self.t.send(actor, message, markup)

    def send_chunks(self, actor, message):
        while message:
            size = min(3500, len(message))
            if size < len(message):
                cut = message.rfind("\n", 0, size)
                if cut > 1000:
                    size = cut
            self.send(actor, message[:size])
            message = message[size:]

    def _session(self, actor):
        row = self.s.get("SELECT data FROM sessions WHERE user_id=?", actor)
        session = json.loads(row["data"]) if row else None
        if session and session.get("kind") == "loan_report":
            self.s.run("DELETE FROM sessions WHERE user_id=?", actor)
            return None
        if (
            session
            and session.get("mode") in {"wizard", "confirm"}
            and session.get("dialogVersion") != 3
        ):
            data = session["data"]
            if session["kind"] in CONTACT_WIZARDS:
                saved = contact_from(data)
                try:
                    data["phone"] = phone_number(saved.get("phone"))
                except DomainError:
                    data.pop("phone", None)
                if saved.get("telegram"):
                    data["telegram"] = saved["telegram"]
                if valid_id(saved.get("userId")):
                    data["userId"] = saved["userId"]
                else:
                    data.pop("userId", None)
                data["contactMethod"] = "phone"
                if session.get("draftId"):
                    session["replacedDraft"] = session.pop("draftId")
                    self.s.run("DELETE FROM drafts WHERE id=?", session["replacedDraft"])
                session["mode"] = "wizard"
            session["dialogVersion"] = 3
            if session["mode"] == "wizard":
                session.update(step=0, visited=[], nonce=uuid()[:8])
                while session["step"] < len(fields(session["kind"], data)):
                    f = fields(session["kind"], data)[session["step"]]
                    if f.when and not f.when(data):
                        session["step"] += 1
                        continue
                    if f.key not in data:
                        break
                    session["visited"].append(session["step"])
                    session["step"] += 1
            self._save_session(actor, session)
        return session

    def _save_session(self, actor, data):
        self.s.run(
            "INSERT OR REPLACE INTO sessions VALUES(?,?)",
            actor,
            stable({**data, "updatedAt": stamp(self.i.now())}),
        )

    def _clear(self, actor, cancel=False):
        with self.s.transaction():
            if cancel:
                row = self.s.get("SELECT data FROM sessions WHERE user_id=?", actor)
                session = json.loads(row["data"]) if row else {}
                if session.get("draftId"):
                    self.s.run(
                        "DELETE FROM drafts WHERE id=? AND user_id=?", session["draftId"], actor
                    )
            self.s.run("DELETE FROM sessions WHERE user_id=?", actor)

    def _cancel_all(self, actor):
        with self.s.transaction():
            self.s.cancel_drafts(actor)
            self._clear(actor)

    def _offer_switch(self, actor, intent):
        session = self._session(actor)
        if not session or session["mode"] not in {"wizard", "confirm"}:
            return False
        identifier = self.s.draft(
            actor, "switch", {**intent, "sessionNonce": session["nonce"]}, self.i.now()
        )
        self.send(
            actor,
            "Есть незавершённая команда. Введённые ответы сохранены.",
            [
                [button("Продолжить", "resume")],
                [button("Отменить и начать новую", "switch:" + identifier)],
            ],
        )
        return True

    def resume(self, actor):
        session = self._session(actor)
        if not session:
            return self.send(actor, "Незавершённых команд нет.", [[button("Меню", "menu")]])
        if session["mode"] in {"wizard", "confirm"}:
            kind = session["kind"]
            self.i.authorize(
                actor,
                "observer"
                if kind in READ_WIZARDS
                else "admin"
                if kind in ADMIN_WIZARDS
                else "operator",
            )
        if session["mode"] == "confirm":
            # Refresh an expired confirmation from the retained answers. The
            # original versions still protect against intervening changes.
            return self.finish(actor, session)
        if session["mode"] == "wizard":
            return self.ask(actor)
        return self.send(actor, "Введите инвентарный номер или текст поиска.")

    def _shared_contact(self, actor, shared):
        session = self._session(actor)
        require(session and session["mode"] == "wizard", "Сначала начните ввод контакта")
        f = fields(session["kind"], session["data"])[session["step"]]
        require(
            f.key in {"phone", "userId"},
            "Контакт можно отправить на шаге контакта или Telegram ID",
        )
        # Sharing a contact supplies a real user ID, not a username guess.
        require(
            f.key == "phone" or shared.get("user_id"),
            "В этой карточке нет Telegram ID; введите числовой ID или выберите телефон",
        )
        if shared.get("user_id"):
            session["data"]["userId"] = shared["user_id"]
        elif f.key == "phone":
            session["data"].pop("userId", None)
        session["data"]["sharedContact"] = bool(shared.get("user_id")) and f.key == "phone"
        self._save_session(actor, session)
        return self.answer_wizard(
            actor,
            str(shared.get("phone_number") or "-") if f.key == "phone" else str(shared["user_id"]),
        )

    def handle(self, update):
        cb = update.get("callback_query")
        m = (cb or {}).get("message") or update.get("message")
        actor = ((cb or {}).get("from") or (m or {}).get("from") or {}).get("id")
        if not m or not actor:
            return
        if m.get("chat", {}).get("type") != "private" or m["chat"]["id"] != actor:
            if cb:
                self.t.answer(cb["id"], "Работайте с ботом в личном чате")
            return
        if cb:
            try:
                self.t.telegram.answer(cb["id"])
            except Exception:
                pass
        with self.s.transaction(), self.t.capture(actor):
            if self.s.get("SELECT 1 FROM processed_updates WHERE id=?", update["update_id"]):
                return True
            try:
                with self.s.transaction():
                    self.i.authorize(actor)
                    if cb:
                        self.callback(actor, cb.get("data", ""))
                    else:
                        shared = m.get("contact")
                        if shared:
                            self._shared_contact(actor, shared)
                        else:
                            message_text = str(m.get("text", ""))
                            mentions = [
                                e["user"]["id"]
                                for e in m.get("entities", [])
                                if e.get("type") == "text_mention" and e.get("user")
                            ]
                            session = self._session(actor)
                            if len(set(mentions)) == 1 and session and session["mode"] == "wizard":
                                f = fields(session["kind"], session["data"])[session["step"]]
                                if f.key == "userId":
                                    message_text = str(mentions[0])
                            self.message(actor, message_text)
            except DomainError as error:
                safe = str(error)
                if error.code == "forbidden":
                    self._cancel_all(actor)
                    safe = f"Нет доступа. Ваш Telegram ID: {actor}"
                self.t.send(actor, safe)
            self.s.run("INSERT INTO processed_updates VALUES(?)", update["update_id"])
        self.t.flush()
        return True

    def menu(self, actor):
        role = self.i.authorize(actor)
        buttons = [
            button("Найти роутер", "search"),
            button("Отчёты", "reports"),
            button("Сбор", "campaigns"),
        ]
        if role != "observer":
            buttons += [
                button("Добавить", "start:add"),
                button("Выдать", "select:issue"),
                button("Принять", "select:return"),
            ]
        if role == "admin":
            buttons.append(button("Настройки", "settings"))
        if self._session(actor):
            buttons.insert(0, button("Продолжить команду", "resume"))
        status = (
            "ДЕМОНСТРАЦИЯ: Google не подключён."
            if self.mode == "memory"
            else "Google не подключён; операции сохраняются в отдельной базе SQLite."
            if self.mode == "disabled"
            else "Обновления Google Sheets проверяются при синхронизации; очередь — в отчётах."
        )
        return self.send(
            actor, f"Учёт роутеров · общежитие № 7\nРоль: {display(role)}\n{status}", rows(buttons)
        )

    def message(self, actor, message):
        self.i.authorize(actor)
        if message in {"/cancel", "Отмена"}:
            self._cancel_all(actor)
            return self.menu(actor)
        if message in {"/start", "/menu"}:
            return self.menu(actor)
        if message == "/resume":
            return self.resume(actor)
        if message in {"/back", "Назад"}:
            return self.back(actor)
        routes = {
            "/sync": self.sync_preview,
            "/repair_colors": self.color_preview,
            "/settings": self.settings,
            "/reports": self.reports,
            "/collection": self.campaign_list,
            "/export": self.export,
        }
        if message in routes:
            return routes[message](actor)
        if message == "/help":
            return self.send(
                actor,
                "Меню: /menu\nПродолжить команду: /resume\nОтменить команду: /cancel\nПоиск: /find текст\nСписок: /list\nОтчёты: /reports\nЭкспорт: /export\nСбор: /collection\nАдминистратор: /settings, /sync, /repair_colors\nМеню сохраняет введённые ответы. Учёт меняется только после подтверждения.",
                [[button("Меню", "menu")]],
            )
        if message == "/provision":
            return self.send(
                actor,
                "Добавление столбцов отключено. Бот использует только существующие A:J; дополнительные данные и история хранятся в SQLite.",
            )
        if message == "/list":
            return self.show_list(actor)
        if message.startswith("/find "):
            return self.show_list(actor, {"q": message[6:].strip()})
        session = self._session(actor)
        if not session:
            return self.menu(actor)
        if session["mode"] == "confirm":
            return self.resume(actor)
        if session["mode"] == "search":
            self._clear(actor)
            return self.show_list(actor, {"q": message})
        if session["mode"] == "select":
            self.i.authorize(actor, "operator")
            r = self.i.get(actor, message.strip())
            return self.start(
                actor, session["kind"], {"routerId": r["id"], "expected": {r["id"]: r["version"]}}
            )
        return self.answer_wizard(actor, message)

    def callback(self, actor, callback):
        self.i.authorize(actor)
        parts = callback.split(":")
        action, a, b, c = (parts + [""] * 4)[:4]
        if action == "menu":
            return self.menu(actor)
        if action == "discard":
            self.s.cancel_preview(actor, a)
            return self.menu(actor)
        if action == "resume":
            return self.resume(actor)
        if action == "cancel":
            session = self._session(actor)
            require(session and a and a == session.get("nonce"), "Кнопка отмены устарела")
            self._clear(actor, cancel=True)
            return self.menu(actor)
        if action == "switch":
            draft = self.s.get_draft(actor, a, self.i.now())
            require(draft["kind"] == "switch", "Неверная команда")
            session = self._session(actor)
            require(
                session and session.get("nonce") == draft["data"]["sessionNonce"],
                "Кнопка смены команды устарела",
            )
            self._clear(actor, cancel=True)
            intent = draft["data"]
            if "kind" in intent:
                return self.start(actor, intent["kind"], intent.get("data"), force=True)
            return self.callback(actor, intent["callback"])
        if action in {"search", "select"} and self._offer_switch(actor, {"callback": callback}):
            return
        if action == "search":
            nonce = uuid()[:8]
            self._save_session(actor, {"mode": "search", "nonce": nonce})
            return self.send(
                actor,
                "Введите номер, MAC, модель, ФИО, группу или место.",
                [[button("Отмена", "cancel:" + nonce)]],
            )
        if action == "select":
            self.i.authorize(actor, "operator")
            nonce = uuid()[:8]
            self._save_session(actor, {"mode": "select", "kind": a, "nonce": nonce})
            return self.send(
                actor, "Введите инвентарный номер.", [[button("Отмена", "cancel:" + nonce)]]
            )
        if action == "start":
            return self.start(actor, a)
        if action == "act":
            r = self.i.get(actor, b)
            return self.start(actor, a, {"routerId": r["id"], "expected": {r["id"]: r["version"]}})
        if action == "answer":
            session = self._session(actor)
            require(
                session
                and session["mode"] == "wizard"
                and session["nonce"] == a
                and str(session["step"]) == b,
                "Кнопка от другого шага устарела",
            )
            return self.answer_wizard(actor, c)
        if action in {"contact_options", "contact_mode"}:
            session = self._session(actor)
            require(
                session
                and session["mode"] == "wizard"
                and session["nonce"] == a
                and str(session["step"]) == b,
                "Кнопка другого шага устарела",
            )
            self.i.authorize(actor, "admin" if session["kind"] == "loan_details" else "operator")
            all_fields = fields(session["kind"], session["data"])
            require(
                all_fields[session["step"]].key in {"phone", "userId"},
                "Выбор контакта доступен на шаге телефона или ID",
            )
            if action == "contact_options":
                return self.send(
                    actor,
                    "Вместо телефона можно указать Telegram ID получателя.",
                    [
                        [button("Использовать Telegram ID", f"contact_mode:{a}:{b}:id")],
                        [button("Использовать телефон", f"contact_mode:{a}:{b}:phone")],
                    ],
                )
            require(c in {"id", "phone"}, "Выберите телефон или Telegram ID")
            identifier = session["data"].get("userId")
            for key in ("phone", "userId", "contactInput", "telegram", "sharedContact"):
                session["data"].pop(key, None)
            session["data"]["contactMethod"] = "telegram_id" if c == "id" else "phone"
            if c == "id" and valid_id(identifier):
                session["data"]["userId"] = identifier
                self.send(actor, f"Используется Telegram ID: {identifier}")
            session["nonce"] = uuid()[:8]
            contact_start = next(n for n, f in enumerate(all_fields) if f.key == "phone")
            session["visited"] = [n for n in session["visited"] if n < contact_start]
            session["step"] = contact_start
            self._save_session(actor, session)
            return self.ask(actor)
        if action == "back":
            session = self._session(actor)
            require(session and (not a or a == session.get("nonce")), "Кнопка шага устарела")
            require(not b or b == str(session["step"]), "Кнопка другого шага устарела")
            return self.back(actor)
        if action == "confirm":
            return self.confirm(actor, a)
        if action == "card":
            return self.card(actor, a)
        if action == "extra":
            return self.card(actor, a, advanced=True)
        if action == "archive":
            return self.campaign_list(actor, archive=True)
        if action == "hist":
            return self.history(actor, a, int(b or 0))
        if action == "list":
            d = self.s.get_draft(actor, a, self.i.now())
            require(d["kind"] == "list", "Неверный список")
            return self.show_list(actor, d["data"], int(b or 0), a)
        if action == "report":
            return self.show_list(
                actor,
                {"vip": True} if a == "vip" else {"status": a} if a in STATUS else {"report": a},
            )
        if action in {"reports", "settings", "campaigns", "sync", "colors"}:
            return {
                "reports": self.reports,
                "settings": self.settings,
                "campaigns": self.campaign_list,
                "sync": self.sync_preview,
                "colors": self.color_preview,
            }[action](actor)
        if action == "sv":
            return self.sync_page(actor, a, int(b or 0))
        if action == "sa":
            self.i.authorize(actor, "admin")
            require(self.sync, "Google Sheets не подключён")
            d = self.s.get_draft(actor, a, self.i.now())
            result = self.sync.apply(actor, a, d["data"].get("resolutions", {}))
            return self.send(
                actor,
                f"Импорт завершён. Добавлено: {result['added']}; обновлено: {result['updated']}; без изменений: {result['unchanged']}; пропущено: {result['skipped']}; конфликтов: {result['conflicts']}.",
                [[button("Меню", "menu")]],
            )
        if action == "sr":
            return self.resolve_page(actor, a, int(b))
        if action == "sx":
            self.i.authorize(actor, "admin")
            d = self.s.get_draft(actor, a, self.i.now())
            e = d["data"]["entries"][int(b)]
            require(
                e["type"] == "conflict" and not e.get("identity"),
                "Идентичность требует ручного восстановления номера",
            )
            custom = (
                list(
                    dict.fromkeys(
                        f
                        for k in e["conflicts"]
                        for f in (sorted(LINKED) if k == "связанные поля выдачи" else [k])
                    )
                )
                if c == "custom"
                else []
            )
            return self.start(
                actor,
                "resolve_custom" if c == "custom" else "resolve",
                {"syncDraft": a, "entryIndex": int(b), "source": c, "customFields": custom},
            )
        if action == "cp":
            return self.campaign_preview(actor, a)
        if action in {"ce", "cin"}:
            return self.start(
                actor, "campaign_edit" if action == "ce" else "campaign_include", {"campaignId": a}
            )
        if action == "queue":
            return self.queue(actor, a, int(b or 0))
        if action == "qfilter":
            return self.start(actor, "queue_filters", {"campaignId": a})
        if action == "qf":
            d = self.s.get_draft(actor, a, self.i.now())
            require(d["kind"] == "queue_nav", "Просмотр очереди устарел")
            return self.queue(actor, d["data"]["campaignId"], int(b or 0), d["data"]["filter"], a)
        if action == "qa":
            self.i.authorize(actor, "operator")
            d = self.s.get_draft(actor, a, self.i.now())
            require(d["kind"] == "queue", "Очередь устарела")
            loan_id = d["data"]["loanIds"][int(b)]
            require(
                any(
                    x["loan"]["id"] == loan_id for x in self.c.queue(actor, d["data"]["campaignId"])
                ),
                "Очередь изменилась; выдача закрыта",
            )
            return self.start(
                actor, "queue_action", {"campaignId": d["data"]["campaignId"], "loanId": loan_id}
            )
        raise DomainError("Кнопка устарела. Откройте меню.")

    def start(self, actor, kind, data=None, *, force=False):
        if kind == "loan_report":
            return self.reports(actor)
        self.i.authorize(
            actor,
            "observer"
            if kind in READ_WIZARDS
            else "admin"
            if kind in ADMIN_WIZARDS
            else "operator",
        )
        if not force and self._offer_switch(actor, {"kind": kind, "data": data or {}}):
            return
        data = {**(data or {})}
        if kind == "replace":
            loan = self.i.get(actor, data["routerId"])["loan"]
            if loan and loan.get("dueConfirmed") and loan.get("dueAt"):
                data["dueAt"] = loan["dueAt"]
        self._save_session(
            actor,
            {
                "mode": "wizard",
                "kind": kind,
                "data": {
                    **({"contactMethod": "phone"} if kind in CONTACT_WIZARDS else {}),
                    **(data or {}),
                },
                "step": 0,
                "visited": [],
                "nonce": uuid()[:8],
                "dialogVersion": 3,
            },
        )
        if kind in {"return", "replace", "resolve_found", "inventory_check"}:
            self.send(
                actor,
                "При приёме: сверить номер → подтвердить физическое наличие → осмотреть и проверить работоспособность → указать место хранения. Без проверки выбирайте «Нужна проверка».",
            )
        return self.ask(actor)

    def ask(self, actor):
        session = self._session(actor)
        require(session and session["mode"] == "wizard", "Начните диалог заново")
        all_fields = fields(session["kind"], session["data"])
        while session["step"] < len(all_fields):
            current = all_fields[session["step"]]
            if not (current.when and not current.when(session["data"])):
                if current.key not in session["data"]:
                    break
                if session["step"] not in session["visited"]:
                    session["visited"].append(session["step"])
            session["step"] += 1
        self._save_session(actor, session)
        if session["step"] >= len(all_fields):
            return self.finish(actor, session)
        f = all_fields[session["step"]]
        buttons = rows(
            [
                button(label, f"answer:{session['nonce']}:{session['step']}:{value}")
                for value, label in (f.choices or {}).items()
            ]
        )
        if f.key in {"phone", "userId"}:
            buttons.append(
                [button("Дополнительно", f"contact_options:{session['nonce']}:{session['step']}")]
            )
        buttons.append(
            [
                button("Назад", f"back:{session['nonce']}:{session['step']}"),
                button("Отмена", "cancel:" + session["nonce"]),
            ]
        )
        return self.send(
            actor,
            f"{LABELS[session['kind']]} · шаг {len(session['visited']) + 1}\n{f.prompt}",
            buttons,
        )

    def answer_wizard(self, actor, value):
        session = self._session(actor)
        require(session and session["mode"] == "wizard", "Начните диалог заново")
        kind = session["kind"]
        self.i.authorize(
            actor,
            "observer"
            if kind in READ_WIZARDS
            else "admin"
            if kind in ADMIN_WIZARDS
            else "operator",
        )
        all_fields = fields(kind, session["data"])
        require(session["step"] < len(all_fields), "Диалог уже завершён")
        f = all_fields[session["step"]]
        try:
            session["data"][f.key] = f.parse(value)
        except (ValueError, OverflowError):
            raise DomainError("Неверный формат значения") from None
        if f.key == "phone":
            if not session["data"].pop("sharedContact", False):
                session["data"].pop("userId", None)
                session["data"].pop("telegram", None)
            session["data"].pop("contactInput", None)
        session["visited"].append(session["step"])
        session["step"] += 1
        self._save_session(actor, session)
        return self.ask(actor)

    def back(self, actor):
        session = self._session(actor)
        require(session and session["mode"] in {"wizard", "confirm"}, "Начните диалог заново")
        if session["mode"] == "confirm":
            self.s.run("DELETE FROM drafts WHERE id=?", session["draftId"])
            session["mode"] = "wizard"
        if not session["visited"]:
            self._clear(actor)
            return self.menu(actor)
        session["step"] = session["visited"].pop()
        for f in fields(session["kind"], session["data"])[session["step"] :]:
            session["data"].pop(f.key, None)
        self._save_session(actor, session)
        return self.ask(actor)

    def finish(self, actor, session):
        kind, data = session["kind"], session["data"]
        if kind in {"filters", "queue_filters"}:
            filters = {
                k: v for k, v in data.items() if k != "campaignId" and v not in (None, "", "all")
            }
            if "vip" in filters:
                filters["vip"] = filters["vip"] == "yes"
            self._clear(actor)
            return (
                self.queue(actor, data["campaignId"], 0, filters)
                if kind == "queue_filters"
                else self.show_list(actor, filters)
            )
        special = {
            "role",
            "campaign",
            "campaign_edit",
            "campaign_include",
            "queue_action",
            "resolve",
            "resolve_custom",
        }
        prepared = (
            {"kind": kind, "data": data}
            if kind in special
            else {"kind": "operation", "command": command(kind, data, self.i, actor)}
        )
        if kind in {"campaign", "campaign_edit"}:
            old = self.c._get(data["campaignId"]) if kind == "campaign_edit" else {}
            try:
                responsible = [int(v.strip()) for v in data["responsibleIds"].split(",")]
            except ValueError:
                raise DomainError("ID ответственных должны быть числами") from None
            prepared = {
                "kind": "campaign",
                "data": {
                    **old,
                    **(
                        {
                            "name": f"Сбор набора {data['admissionYear']}",
                            "startsAt": local_clock(self.i.now())["day"],
                            "hours": "По договорённости",
                            "instruction": "Сверьте номер и оформите физический приём в боте.",
                        }
                        if not old
                        else {}
                    ),
                    **data,
                    "responsibleIds": responsible,
                    **({"expectedVersion": old["version"]} if old else {}),
                },
            }
        if kind == "campaign_include":
            r = self.i.get(actor, data["loanInventory"])
            require(r["loan"], "У роутера нет действующей выдачи")
            prepared = {
                "kind": "queue_action",
                "data": {
                    "campaignId": data["campaignId"],
                    "loanId": r["loan"]["id"],
                    "action": "include",
                    "reason": data["reason"],
                },
            }
        if kind == "queue_action" and data["action"] in {"exclude", "include"}:
            self.i.authorize(actor, "admin")
        warning = ""
        if kind == "correct" and not self.s.get(
            "SELECT 1 FROM event_states WHERE event_id=?", data["eventId"]
        ):
            warning = "У старой операции нет снимка контактов. Откат сохранит текущие контакты; прежние при необходимости уточните отдельно."
        if kind in {"issue", "transfer"}:
            warning = "\n".join(
                self.i.warnings(
                    actor, prepared["command"]["recipient"], prepared["command"]["place"]
                )
            )
        identifier = self.s.draft(actor, "confirm", prepared, self.i.now())
        if session.get("draftId"):
            self.s.run("DELETE FROM drafts WHERE id=?", session["draftId"])
        self._save_session(actor, {**session, "mode": "confirm", "draftId": identifier})
        hidden = {
            "expected",
            "routerId",
            "customFields",
            "syncDraft",
            "entryIndex",
            "campaignId",
            "loanId",
            "dialogVersion",
            "contactInput",
        }
        lines = [
            f"{FIELD_LABELS.get(k, k)}: {display(v)}" for k, v in data.items() if k not in hidden
        ]
        if data.get("routerId"):
            lines.insert(0, "Роутер: " + self.i.get(actor, data["routerId"])["inventory"])
        if kind == "correct":
            event = self.s.get(
                "SELECT * FROM events WHERE id=? AND object_id=?", data["eventId"], data["routerId"]
            )
            if event:
                for linked in self.i._correction_events(event):
                    if linked["object_id"] != data["routerId"]:
                        lines.append(
                            "Связанный откат: "
                            + self.i.get(actor, linked["object_id"])["inventory"]
                        )
        summary = LABELS[kind] + " — проверьте итог\n" + "\n".join(lines)
        if warning:
            summary += (
                "\nВозможные другие выдачи человеку / в то же место (совпадение имени не доказывает личность):\n"
                if kind in {"issue", "transfer"}
                else "\n"
            ) + warning
        self.send_chunks(actor, summary)
        return self.send(
            actor,
            "Применить показанные изменения?",
            [
                [button("Подтвердить", "confirm:" + identifier)],
                [
                    button("Назад", f"back:{session['nonce']}:{session['step']}"),
                    button("Отмена", "cancel:" + session["nonce"]),
                ],
            ],
        )

    def confirm(self, actor, identifier):
        self.i.authorize(actor)
        session = self._session(actor)
        if session and session.get("replacedDraft") == identifier and session["mode"] == "wizard":
            return self.ask(actor)
        draft = self.s.get_draft(actor, identifier, self.i.now())
        if draft["kind"] == "colors":
            self.i.authorize(actor, "admin")
            require(self.colors, "Google Sheets не подключён")
            r = self.colors.apply(actor, identifier)
            return self.send(
                actor,
                f"Раскраска {'восстановлена' if r['changed'] else 'уже верна'}. Правил Checked сохранено: {r['checkedPreserved']}.",
            )
        if draft["kind"] == "launch":
            self.i.authorize(actor, "admin")
            p = draft["data"]
            require(
                equal(p["versions"], {r["id"]: r["version"] for r in self.i.list(actor)}),
                "Список выдач изменился; повторите просмотр кампании",
            )
            r = self.c.launch(actor, "launch:" + identifier, p["id"], p["version"])
            return self.send(
                actor,
                f"Кампания запущена. Включено: {r['included']}; на уточнение: {r['clarify']}.",
            )
        require(draft["kind"] == "confirm", "Неверное подтверждение")
        p, key = draft["data"], "dialog:" + identifier
        if p["kind"] == "operation":
            cmd = p["command"]
            if cmd["kind"] in CONTACT_WIZARDS and not self.s.get(
                "SELECT 1 FROM requests WHERE key=?", key
            ):
                details = (
                    (cmd.get("recipient") or {}).get("contact")
                    if cmd["kind"] in {"issue", "transfer"}
                    else cmd.get("contact")
                )
                require(
                    (details or {}).get("method") in {"phone", "telegram_id"},
                    "Укажите телефон или выберите Telegram ID через «Дополнительно». Продолжите команду: /resume",
                )
            result = self.i.execute(actor, key, p["command"])
            self._complete(actor, identifier)
            pending = self.s.get("SELECT state FROM outbox WHERE router_id=?", result["routerId"])
            self.send(
                actor,
                "Операция сохранена, обновление таблицы ожидается."
                if pending
                else "Операция сохранена, обновление таблицы подтверждено.",
            )
            return self.card(actor, result["routerId"])
        data = p["data"]
        if p["kind"] == "role":
            self.i.set_role(
                actor,
                data["target"],
                None if data["role"] == "revoke" else data["role"],
                "role:" + identifier,
            )
            self._complete(actor, identifier)
            return self.t.send(actor, "Права обновлены. Отзыв действует сразу.")
        if p["kind"] == "campaign":
            result = self.c.save(actor, "campaign:" + identifier, data)
            self._complete(actor, identifier)
            return self.campaign_preview(actor, result["id"])
        if p["kind"] == "queue_action":
            self.c.member(actor, "queue:" + identifier, data["campaignId"], data["loanId"], data)
            self._complete(actor, identifier)
            return self.send(
                actor,
                "Действие записано. Срок меняет только администратор; приём — только после физического возврата.",
                [[button("Очередь", "queue:" + data["campaignId"])]],
            )
        if p["kind"] in {"resolve", "resolve_custom"}:
            self.i.authorize(actor, "admin")
            target = self.s.get_draft(actor, data["syncDraft"], self.i.now())
            entry = target["data"]["entries"][data["entryIndex"]]
            target["data"].setdefault("resolutions", {})[entry["routerId"]] = {
                "source": data["source"],
                "reason": data["reason"],
                "values": {f: data[f] for f in data.get("customFields", [])},
            }
            self.s.run("UPDATE drafts SET data=? WHERE id=?", stable(target["data"]), target["id"])
            self._complete(actor, identifier)
            return self.send(
                actor,
                "Решение сохранено в просмотре. Примените проверенный набор.",
                [[button("К просмотру", "sv:" + target["id"] + ":0")]],
            )
        raise DomainError("Неизвестная операция")

    def _complete(self, actor, identifier):
        session = self._session(actor)
        if session and session.get("draftId") == identifier:
            self._clear(actor)

    def show_list(self, actor, filters=None, page=0, identifier=None):
        all_rows = self.i.list(actor, filters)
        identifier = identifier or self.s.draft(actor, "list", filters or {}, self.i.now())
        page = max(0, min(page, max(0, (len(all_rows) - 1) // 6)))
        part = all_rows[page * 6 : page * 6 + 6]
        buttons = [
            [
                button(
                    f"{r['inventory']} · {STATUS[r['status']]}" + (" · VIP" if r["vip"] else ""),
                    "card:" + r["id"],
                )
            ]
            for r in part
        ]
        buttons.append(
            ([button("←", f"list:{identifier}:{page - 1}")] if page else [])
            + (
                [button("→", f"list:{identifier}:{page + 1}")]
                if page * 6 + 6 < len(all_rows)
                else []
            )
        )
        buttons.append([button("Фильтры", "start:filters"), button("Меню", "menu")])
        return self.send(actor, f"Найдено: {len(all_rows)}. Страница {page + 1}.", buttons)

    def card(self, actor, identifier, advanced=False):
        r = self.i.get(actor, identifier)
        role, loan = self.i.authorize(actor), r["loan"]
        loc = (loan or r).get("place" if loan else "storage") or {}
        lines = [
            f"Роутер {r['inventory']}",
            f"ID: {r['id']}",
            f"Модель: {r.get('model') or '—'}; серийный: {r.get('serial') or '—'}; MAC: {r.get('mac') or '—'}",
            f"Статус: {STATUS[r['status']]}" + (" · VIP: " + r["vipComment"] if r["vip"] else ""),
            f"Состояние: {CONDITION[r['condition']]}",
            f"Место: {loc.get('name') or 'не указано'} ({display(loc.get('certainty', 'unknown'))})",
        ]
        if loan:
            p, contact = loan["recipient"], loan["recipient"].get("contact") or {}
            lines += [
                f"Получатель: {p.get('name') or 'неизвестен'} ({display(p['kind'])})",
                f"Группа: {p.get('group') or '—'}; набор: {p.get('admissionYear') or p.get('academicYear') or 'не подтверждён'}",
                f"Контакты: {contact.get('phone') or '—'}; @{contact.get('telegram') or '—'}; подтверждённый ID: {contact.get('userId') or '—'}",
                f"Выдан: {loan.get('issuedAt') or 'дата неизвестна'}; срок: {loan['dueAt'] if loan.get('dueConfirmed') and loan.get('dueAt') else 'не определён'}"
                + (" · ПРОСРОЧЕНО" if overdue(loan, self.i.now()) else ""),
                "Участие в сборе набора: " + display(loan.get("collectionEligible", False)),
            ]
            if loan.get("exception"):
                lines.append(
                    f"Исключение: {loan['exception']['reason']}; пересмотр {loan['exception']['reviewAt']}"
                )
        elif r.get("legacy", {}).get("owner"):
            lines.append("Наследуемый Owner (не подтверждает выдачу): " + r["legacy"]["owner"])
        lines += [
            "Комментарий: " + (r.get("note") or "—"),
            "Checked (смысл не подтверждён): " + display(r.get("legacy", {}).get("checked")),
            f"Версия {r['version']}; обновлено {r['updatedAt']}",
        ]
        pending = self.s.get("SELECT state,last_error FROM outbox WHERE router_id=?", r["id"])
        if pending:
            lines.append(
                "Таблица: "
                + ("конфликт" if pending["state"] == "blocked" else "обновление ожидается")
            )
        buttons = [button("История", "hist:" + r["id"] + ":0")]
        if role != "observer":
            kinds = ["move"] if advanced else ["condition", "note"]
            if r["status"] == "stored" and not advanced:
                kinds.append("issue")
            if r["status"] in {"stored", "issued", "unknown"} and not advanced:
                kinds.append("lost")
            if loan:
                if not advanced:
                    kinds.append("return")
                if r["status"] == "issued" and advanced:
                    kinds += ["transfer", "replace"]
            if r["status"] == "lost" and not advanced:
                kinds.append("found")
            if role == "admin" and advanced:
                kinds += ["vip", "correct"]
                if r["status"] != "written_off":
                    kinds.append("write_off")
                if loan:
                    kinds += ["extend", "exception", "loan_details"]
                if r["status"] == "unknown":
                    kinds.append("inventory_check")
                if r["status"] == "lost" and r.get("found"):
                    kinds.append("resolve_found")
            buttons += [button(LABELS[k], f"act:{k}:{r['id']}") for k in kinds]
            buttons.append(
                button(
                    "К основным действиям" if advanced else "Дополнительно",
                    ("card:" if advanced else "extra:") + r["id"],
                )
            )
        self.send_chunks(actor, "\n".join(lines))
        return self.send(actor, "Действия", rows(buttons) + [[button("Меню", "menu")]])

    def history(self, actor, identifier, page=0):
        events, loans = self.i.history(actor, identifier), self.i.loans(actor, identifier)
        lines = [f"История · событий {len(events)}, выдач {len(loans)}"]
        for e in events[page * 5 : page * 5 + 5]:
            lines.append(
                f"{e['at']} · {LABELS.get(e['action'], e['action'])}\nID: {e['id']}; сотрудник {e['actor']}\nПричина: {e['reason'] or '—'}"
                + ("\nИсправляет: " + e["correction_of"] if e["correction_of"] else "")
            )
        for loan in loans:
            lines.append(
                f"{loan['id']}: {loan['recipient'].get('name') or 'неизвестно'}; выдача {loan.get('issuedAt') or '?'}; срок {loan.get('dueAt') or '?'}; фактический возврат {loan.get('returnedAt') or '—'}; {'открыта' if loan['open'] else 'закрыта'}"
            )
        self.send_chunks(actor, "\n\n".join(lines))
        nav = (
            ([button("←", f"hist:{identifier}:{page - 1}")] if page else [])
            + ([button("→", f"hist:{identifier}:{page + 1}")] if page * 5 + 5 < len(events) else [])
            + [button("Карточка", "card:" + identifier)]
        )
        return self.send(actor, "Навигация", [nav])

    def reports(self, actor):
        stats = self.i.stats(actor)
        categories = {
            "available": "Доступные",
            "issued": "Выданные",
            "overdue": "Просроченные",
            "lost": "Утерянные",
            "unknown": "Неизвестные",
            "incomplete": "Нужно уточнить",
            "check": "Проверка / ремонт",
            "vip": "VIP",
            "pending": "Ожидают таблицу",
            "conflicts": "Конфликты",
        }
        message = (
            f"Роутеров в учёте: {stats['physical']}.\n"
            + "\n".join(f"{STATUS[s]}: {n}" for s, n in stats["statuses"].items())
            + f"\nVIP: {stats['vip']} (пересекающийся признак)."
        )
        return self.send(
            actor,
            message,
            rows([button(v, "report:" + k) for k, v in categories.items()])
            + [
                [button("Фильтры", "start:filters")],
                [button("Меню", "menu")],
            ],
        )

    def export(self, actor):
        data = [
            [
                "Инвентарный номер",
                "Модель",
                "MAC",
                "Статус",
                "Состояние",
                "VIP",
                "Место",
                "Получатель",
                "Срок",
            ]
        ]
        for r in self.i.list(actor):
            loan = r["loan"] or {}
            loc = loan.get("place") or r.get("storage") or {}
            data.append(
                [
                    r["inventory"],
                    r.get("model"),
                    r.get("mac"),
                    STATUS[r["status"]],
                    CONDITION[r["condition"]],
                    r["vip"],
                    loc.get("name"),
                    loan.get("recipient", {}).get("name"),
                    loan.get("dueAt") if loan.get("dueConfirmed") else "Срок не определён",
                ]
            )
        self.i.authorize(actor)
        return self.t.file(
            actor,
            "routers.csv",
            "\ufeff" + "\r\n".join(";".join(csv_cell(v) for v in row) for row in data),
        )

    def settings(self, actor):
        self.i.authorize(actor, "admin")
        roles = "\n".join(f"{r['user_id']} · {display(r['role'])}" for r in self.i.roles(actor))
        conflicts = self.s.get("SELECT count(*) AS n FROM conflicts")["n"]
        return self.send(
            actor,
            f"Последний импорт: {self.s.setting('last_sync_at', 'ещё не выполнялся')}. Нерешённых расхождений: {conflicts}.\nДоступ:\n{roles}\nИстория хранится отдельно в SQLite. Таблица: только A:J.",
            [
                [button("Доступ", "start:role")],
                [button("Синхронизировать из таблицы", "sync")],
                [button("Восстановить раскраску", "colors")],
                [button("Настроить сбор", "start:campaign"), button("Сбор", "campaigns")],
                [button("Меню", "menu")],
            ],
        )

    def sync_preview(self, actor):
        self.i.authorize(actor, "admin")
        require(self.sync, "Google Sheets не подключён. Настройте .env по инструкции.")
        p = self.sync.preview(actor)
        return self.sync_page(actor, p["id"])

    def sync_page(self, actor, identifier, page=0):
        self.i.authorize(actor, "admin")
        d = self.s.get_draft(actor, identifier, self.i.now())
        require(d["kind"] == "sync", "Неверное подтверждение")
        entries = d["data"]["entries"]
        changed = [(i, e) for i, e in enumerate(entries) if e["type"] != "same"]
        buttons, lines = (
            [],
            [
                "/sync · " + ("Предварительный просмотр" if changed else "Изменений нет"),
                f"Новые: {sum(e['type'] == 'new' for e in entries)}; изменения: {sum(e['type'] == 'change' for e in entries)}; расхождения / уточнения: {sum(e['type'] in {'conflict', 'review', 'error', 'missing'} for e in entries)}.",
            ],
        )
        labels = {
            "new": "Новый",
            "change": "Изменения",
            "conflict": "Конфликт",
            "review": "Уточнение",
            "error": "Ошибка",
            "missing": "Исчезла",
        }
        for index, e in changed[page * 4 : page * 4 + 4]:
            lines.append(
                f"Строка {e.get('row', 'исчезла')} · {labels[e['type']]}\n{e.get('message') or ''}"
            )
            lines += [
                f"{FIELD_LABELS.get(c['field'], c['field'])}: {display(c['before'])} → {display(c['sheet'])} (бот: {display(c['bot'])})"
                for c in e.get("changes", [])
            ]
            if e["type"] == "new":
                lines += [f"{FIELD_LABELS.get(k, k)}: {display(v)}" for k, v in e["values"].items()]
                lines.append("Неполнота: " + "; ".join(e.get("incomplete", [])))
            if e["type"] == "conflict" and not e.get("identity"):
                buttons.append([button(f"Разобрать строку {e['row']}", f"sr:{identifier}:{index}")])
        self.send_chunks(actor, "\n".join(lines))
        buttons.append(
            ([button("←", f"sv:{identifier}:{page - 1}")] if page else [])
            + ([button("→", f"sv:{identifier}:{page + 1}")] if page * 4 + 4 < len(changed) else [])
        )
        if changed:
            buttons.append([button("Применить проверенные изменения", "sa:" + identifier)])
        buttons.append([button("Отмена", "discard:" + identifier)])
        return self.send(
            actor,
            "Ошибки и неразобранные конфликты пропускаются с сохранением блокировки исходящей записи.",
            buttons,
        )

    def resolve_page(self, actor, identifier, index):
        self.i.authorize(actor, "admin")
        d = self.s.get_draft(actor, identifier, self.i.now())
        e = d["data"]["entries"][index]
        require(not e.get("identity"), "Восстановите прежний инвентарный номер в таблице")
        self.send_chunks(
            actor,
            "Конфликт строки "
            + str(e["row"])
            + "\n"
            + "\n".join(
                f"{FIELD_LABELS.get(c['field'], c['field'])}: исходное {display(c['before'])}, таблица {display(c['sheet'])}, бот {display(c['bot'])}"
                for c in e.get("changes", [])
            ),
        )
        return self.send(
            actor,
            "Выберите согласованный вариант; бизнес-правила будут проверены.",
            [
                [
                    button("Из таблицы", f"sx:{identifier}:{index}:sheet"),
                    button("Из бота", f"sx:{identifier}:{index}:bot"),
                ],
                [
                    button("Исправить значения", f"sx:{identifier}:{index}:custom"),
                    button("Отмена", f"sv:{identifier}:0"),
                ],
            ],
        )

    def color_preview(self, actor):
        self.i.authorize(actor, "admin")
        require(self.colors, "Google Sheets не подключён")
        p = self.colors.preview(actor)
        self.send_chunks(
            actor,
            f"Раскраска: {'есть отличия' if p['changed'] else 'изменений нет'}.\nРаспознано: {p['recognized']}, ожидается: {p['expected']}. Посторонних: {p['foreign']}; Checked: {len(p['legacyChecked'])}.\n"
            + "\n".join(p["issues"] + p["notes"]),
        )
        return self.send(
            actor,
            "Применить показанные исправления?",
            [[button("Применить", "confirm:" + p["id"]), button("Отмена", "discard:" + p["id"])]],
        )

    def campaign_list(self, actor, archive=False):
        campaigns = self.c.list(actor)
        if not archive:
            active = [c for c in campaigns if c["active"]]
            campaigns = active or campaigns[-1:]
        buttons = [
            [button(c["name"] + (" · активна" if c["active"] else " · проект"), "queue:" + c["id"])]
            for c in campaigns
        ]
        if self.i.authorize(actor) == "admin":
            buttons.append([button("Настроить новый сбор", "start:campaign")])
        if not archive and len(self.c.list(actor)) > len(campaigns):
            buttons.append([button("Предыдущие сборы", "archive")])
        return self.send(
            actor,
            "Ежегодный сбор. Одновременно работает один сбор; ответственные получают одну сводку в день.",
            buttons + [[button("Меню", "menu")]],
        )

    def campaign_preview(self, actor, identifier):
        p = self.c.preview(actor, identifier)
        c = p["campaign"]
        d = self.s.draft(
            actor,
            "launch",
            {
                "id": identifier,
                "version": c["version"],
                "versions": {r["id"]: r["version"] for r in self.i.list(actor)},
            },
            self.i.now(),
        )
        text = (
            f"{c['name']}: {c['startsAt']} — {c['deadline']}\n{c['location']}, {c['hours']}\nОтветственные: {', '.join(str(v) for v in c['responsibleIds'])}\nИнструкция: {c['instruction']}\nПодходящих выдач: {len(p['included'])}\n"
            + "\n".join(
                f"{x['router']['inventory']} · {x['loan']['recipient'].get('name')} · {x['loan']['place']['name']}"
                for x in p["included"]
            )
            + f"\nНа уточнение: {len(p['clarify'])}\n"
            + "\n".join(x["router"]["inventory"] for x in p["clarify"])
        )
        self.send_chunks(actor, text)
        return self.send(
            actor,
            "При запуске предыдущий активный сбор будет завершён. Его история сохранится.",
            [
                [
                    button("Подтвердить запуск", "confirm:" + d),
                    button("Настроить", "ce:" + identifier),
                ],
                [button("Включить выдачу явно", "cin:" + identifier)],
                [button("Очередь", "queue:" + identifier), button("Отмена", "discard:" + d)],
            ],
        )

    def queue(self, actor, identifier, page=0, filters=None, nav=None):
        filters = filters or {}
        queue, stats, role = (
            self.c.queue(actor, identifier, filters),
            self.c.report(actor, identifier),
            self.i.authorize(actor),
        )
        draft = self.s.draft(
            actor,
            "queue",
            {"campaignId": identifier, "loanIds": [x["loan"]["id"] for x in queue]},
            self.i.now(),
        )
        nav = nav or self.s.draft(
            actor, "queue_nav", {"campaignId": identifier, "filter": filters}, self.i.now()
        )
        part = queue[page * 4 : page * 4 + 4]
        lines = [
            f"Выдач в сборе: {stats['plan']}; физически принято: {stats['returned']}; закрыто без приёма: {stats['closedWithoutReturn']}; осталось: {stats['remaining']}; просрочки: {stats['overdue']}; исключения: {stats['excluded']}; утрата: {stats['lost']}."
        ]
        buttons = []
        for index, x in enumerate(part):
            lines.append(
                f"{x['router']['inventory']} · {x['loan']['place']['name']} · {x['loan']['recipient'].get('name') or 'неизвестно'}\nСрок: {x['due']}"
                + (" · ПРОСРОЧЕНО" if x["overdue"] else "")
                + (" · УТРАТА" if x["lost"] else "")
                + (" · ИСКЛЮЧЕНИЕ" if x["excluded"] else "")
                + (
                    "\n" + x["member"]["lastAction"]["reason"]
                    if x["member"].get("lastAction")
                    else ""
                )
            )
            actions = [button("Карточка " + x["router"]["inventory"], "card:" + x["router"]["id"])]
            if role != "observer":
                actions += [
                    button("Принят", "act:return:" + x["router"]["id"]),
                    button("Примечание / исключение", f"qa:{draft}:{page * 4 + index}"),
                ]
            buttons.append(actions)
        self.send_chunks(actor, "\n\n".join(lines))
        buttons.append(
            ([button("←", f"qf:{nav}:{page - 1}")] if page else [])
            + ([button("→", f"qf:{nav}:{page + 1}")] if page * 4 + 4 < len(queue) else [])
        )
        buttons.append([button("Фильтры очереди", "qfilter:" + identifier)])
        if role == "admin":
            buttons.append(
                [
                    button("Просмотр / запуск", "cp:" + identifier),
                    button("Настроить", "ce:" + identifier),
                ]
            )
        return self.send(
            actor,
            "Примечания не меняют срок и не подтверждают приём.",
            buttons + [[button("Меню", "menu")]],
        )
