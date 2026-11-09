"""Three-way reconciliation without adding identity columns to Google Sheets."""

import json
from collections import Counter
from datetime import timedelta

from ..contacts import normalize as normalize_contact
from ..util import (
    DomainError,
    clone,
    contact,
    digest,
    equal,
    local_clock,
    normalize_mac,
    require,
    stable,
    stamp,
    uuid,
)
from .schema import FIELDS, INTERNAL, decode, merge_three, projection, validate_flat, validate_sheet


class Synchronizer:
    def __init__(self, inventory, adapter):
        self.i, self.s, self.a = inventory, inventory.s, adapter

    def _project(self, r):
        return projection(r, self.i._loan(r["id"]), self.i.now())

    def _digest(self, sheet):
        return digest(
            {
                "headers": sheet["headers"][:10],
                "rows": [
                    {
                        "row": row["row"],
                        "values": row["values"],
                        "formulas": row.get("formulas", []),
                    }
                    for row in sheet["rows"]
                ],
                "sheetId": sheet["sheetId"],
                "title": sheet["title"],
            }
        )

    def preview(self, actor):
        self.i.authorize(actor, "admin")
        sheet = self.a.read()
        self.i.authorize(actor, "admin")
        validate_sheet(sheet)
        routers, entries, seen = self.i.list(actor), [], set()
        by_inventory = {r["inventory"].casefold(): r for r in routers}
        for row in sheet["rows"]:
            v = row["values"]
            inv = str(v.get("inventory", "")).strip()
            if not inv:
                if any(k != "checked" and value not in ("", None) for k, value in v.items()):
                    entries.append(
                        {
                            "type": "error",
                            "row": row["row"],
                            "message": "Строка без инвентарного номера; требуется уточнение",
                        }
                    )
                continue
            r = by_inventory.get(inv.casefold())
            if r:
                seen.add(r["id"])
            try:
                validate_flat(v)
                mac = normalize_mac(v.get("mac"))
                require(
                    not any(
                        x["id"] != (r or {}).get("id") and mac and x.get("mac") == mac
                        for x in routers
                    ),
                    "Возможный дубликат MAC",
                )
                if not r:
                    entries.append(
                        {
                            "type": "new",
                            "row": row["row"],
                            "values": clone(v),
                            "incomplete": self._incomplete(decode(v)),
                        }
                    )
                    continue
                require(r["inventory"] == inv, "Изменение постоянного номера требует разбора")
                saved = self.s.get("SELECT data FROM snapshots WHERE router_id=?", r["id"])
                if not saved:
                    bot = self._project(r)
                    differences = [f for f in FIELDS if not equal(v.get(f, ""), bot.get(f, ""))]
                    entries.append(
                        {
                            "type": "conflict",
                            "routerId": r["id"],
                            "row": row["row"],
                            "values": clone(v),
                            "bot": bot,
                            "merged": bot,
                            "conflicts": differences or ["note"],
                            "changes": [
                                {
                                    "field": f,
                                    "before": "Снимок отсутствует",
                                    "sheet": v.get(f, ""),
                                    "bot": bot.get(f, ""),
                                }
                                for f in differences
                            ],
                            "message": "Нет согласованного снимка; явно выберите проверенные данные таблицы или бота с причиной",
                        }
                    )
                    continue
                base, bot = json.loads(saved["data"]), self._project(r)
                merged = merge_three(base, v, bot)
                needs = self._transition_needs(r, merged["merged"])
                resume = self.s.get("SELECT 1 FROM conflicts WHERE router_id=?", r["id"])
                kind = (
                    "conflict"
                    if merged["conflicts"]
                    else "review"
                    if needs
                    else "change"
                    if merged["changes"] or resume
                    else "same"
                )
                entries.append(
                    {
                        "type": kind,
                        "routerId": r["id"],
                        "row": row["row"],
                        "version": r["version"],
                        "values": clone(v),
                        "base": base,
                        "bot": bot,
                        **merged,
                        "message": needs
                        or ("Расхождение устранено; отправка возобновится" if resume else None),
                    }
                )
            except DomainError as error:
                entries.append(
                    {
                        "type": "error",
                        "routerId": (r or {}).get("id"),
                        "row": row["row"],
                        "message": str(error),
                    }
                )
        counts = Counter(
            str(row["values"].get("inventory", "")).strip().casefold()
            for row in sheet["rows"]
            if str(row["values"].get("inventory", "")).strip()
        )
        mac_counts = Counter(
            normalize_mac(row["values"].get("mac"))
            for row in sheet["rows"]
            if self._valid_mac(row["values"].get("mac"))
        )
        for entry in entries:
            row = next((x for x in sheet["rows"] if x["row"] == entry.get("row")), None)
            if row and (
                counts[str(row["values"].get("inventory", "")).strip().casefold()] > 1
                or (
                    self._valid_mac(row["values"].get("mac"))
                    and mac_counts[normalize_mac(row["values"]["mac"])] > 1
                )
            ):
                entry.update(
                    type="error",
                    message="Дубли инвентарного номера / MAC: пропущены все спорные строки",
                )
        missing = [
            r
            for r in routers
            if r["id"] not in seen
            and self.s.get("SELECT 1 FROM snapshots WHERE router_id=?", r["id"])
        ]
        for r in missing:
            entries.append(
                {
                    "type": "missing",
                    "routerId": r["id"],
                    "version": r["version"],
                    "message": "Строка исчезла: история сохранена. Восстановите прежний Inv. №.",
                }
            )
        # With no identity column, a removal plus addition can be a renumbering.
        if missing:
            for entry in entries:
                if entry["type"] == "new":
                    entry.update(
                        type="review",
                        identity=True,
                        message="Есть исчезнувшие номера: сначала исключите переименование существующего роутера. Новые записи не создаются наугад.",
                    )
        p = {
            "digest": self._digest(sheet),
            "versions": {r["id"]: r["version"] for r in routers},
            "entries": entries,
            "at": stamp(self.i.now()),
        }
        return {"id": self.s.draft(actor, "sync", p, self.i.now()), **p}

    @staticmethod
    def _valid_mac(value):
        try:
            return bool(normalize_mac(value))
        except DomainError:
            return False

    @staticmethod
    def _incomplete(d):
        result = ["техническое состояние не проверено"]
        if d["status"] == "unknown":
            result.append("неизвестный статус")
        if d["status"] in {"issued", "lost"}:
            result += ["дата выдачи неизвестна", "срок не определён", "набор не подтверждён"]
            if not d["recipient"]["name"]:
                result.append("получатель неизвестен")
            if not d["recipient"]["contact"]["raw"]:
                result.append("нет контакта")
        return result

    def _transition_needs(self, r, v):
        status = INTERNAL[str(v["status"]).strip()]
        loan = self.i._loan(r["id"])
        if r["status"] == "written_off" and status != "written_off":
            return "Списанное оборудование требует отдельного решения администратора"
        if r["status"] == "lost" and status != "lost":
            return "Обнаружение и физический приём оформите в карточке"
        if status == "lost" and r["status"] != "lost" and not loan and v.get("owner"):
            return "Уточните сведения о прежнем получателе до оформления новой утраты"
        if status == "issued" and r["status"] != "issued":
            return "Для выдачи нужны сведения из карточки: получатель, дата, срок и проверенная исправность"
        if status == "stored" and loan:
            return "Возврат требует физического приёма через карточку"
        if status == "written_off" and r["status"] != "written_off":
            return "Списание с причиной оформите в карточке"
        if status == "unknown" and loan:
            return "Нельзя потерять текущую выдачу при смене статуса"
        if loan and str(v.get("owner", "")) != loan["recipient"].get("name", ""):
            return "Смену получателя оформите связанной передачей в карточке; история прежнего сохранится"
        return None

    def _block(self, router_id, details):
        self.s.run("INSERT OR REPLACE INTO conflicts VALUES(?,?)", router_id, stable(details))
        self.s.run("UPDATE outbox SET state='blocked' WHERE router_id=?", router_id)

    def apply(self, actor, draft_id, resolutions=None):
        self.i.authorize(actor, "admin")
        resolutions = resolutions or {}
        draft = self.s.get_draft(actor, draft_id, self.i.now())
        require(draft["kind"] == "sync", "Неверное подтверждение")
        p, sheet = draft["data"], self.a.read()
        self.i.authorize(actor, "admin")
        validate_sheet(sheet)
        require(
            self._digest(sheet) == p["digest"],
            "Таблица изменилась после просмотра. Выполните /sync заново.",
            "stale",
        )

        def operation(group):
            self.i.authorize(actor, "admin")
            current = {r["id"]: r["version"] for r in self.i.list(actor)}
            require(
                equal(current, p["versions"]),
                "База изменилась после просмотра. Выполните /sync заново.",
                "stale",
            )
            result = dict(added=0, updated=0, unchanged=0, skipped=0, conflicts=0)
            blocked_groups = set()
            for e in p["entries"]:
                if e["type"] in {"error", "review", "missing", "conflict"} and not resolutions.get(
                    e.get("routerId")
                ):
                    out = self.s.get(
                        "SELECT group_id FROM outbox WHERE router_id=?", e.get("routerId")
                    )
                    if out:
                        blocked_groups.add(out["group_id"])
            for e in p["entries"]:
                router_id = e.get("routerId")
                pending = self.s.get("SELECT group_id FROM outbox WHERE router_id=?", router_id)
                if (
                    e["type"] in {"error", "review", "missing"}
                    or e.get("identity")
                    or (pending and pending["group_id"] in blocked_groups)
                ):
                    if router_id:
                        self._block(router_id, e)
                    result["skipped"] += 1
                    continue
                if e["type"] == "same":
                    if equal(e["values"], self._project(self.i._router(router_id))):
                        self.s.run("DELETE FROM outbox WHERE router_id=?", router_id)
                        self.s.run("DELETE FROM conflicts WHERE router_id=?", router_id)
                    result["unchanged"] += 1
                    continue
                values = e.get("merged") or e["values"]
                if e["type"] == "conflict":
                    choice = resolutions.get(router_id)
                    if not choice:
                        self._block(router_id, e)
                        result["conflicts"] += 1
                        continue
                    require(
                        choice.get("source") in {"sheet", "bot", "custom"} and choice.get("reason"),
                        "Для разбора нужен источник и причина",
                    )
                    if choice["source"] == "bot":
                        self.s.run(
                            "INSERT OR REPLACE INTO snapshots VALUES(?,?)",
                            router_id,
                            stable(e["values"]),
                        )
                        self.s.run("DELETE FROM conflicts WHERE router_id=?", router_id)
                        self.i._queue(self.i._router(router_id), group)
                        self.s.event(
                            actor,
                            router_id,
                            "Разрешение конфликта",
                            e["values"],
                            e["bot"],
                            choice["reason"],
                        )
                        result["updated"] += 1
                        continue
                    values = (
                        {**e["bot"], **e["values"]}
                        if choice["source"] == "sheet"
                        else {**e["merged"], **choice.get("values", {})}
                    )
                if router_id:
                    require(
                        not self._transition_needs(self.i._router(router_id), values),
                        self._transition_needs(self.i._router(router_id), values)
                        or "Неполный переход",
                    )
                self._import(
                    actor, values, e, group, (resolutions.get(router_id) or {}).get("reason")
                )
                result["added" if e["type"] == "new" else "updated"] += 1
            self.s.set("last_sync_at", stamp(self.i.now()))
            return result

        return self.s.atomic(
            actor, "sync:" + draft_id, {"draftId": draft_id, "resolutions": resolutions}, operation
        )

    def _import(self, actor, values, entry, group, reason=None):
        d = decode(values)
        if d["legacy"].get("contact"):
            d["legacy"]["contact"] = normalize_contact(d["legacy"]["contact"])
        old_id = entry.get("routerId")
        r = self.i._router(old_id) if old_id else self.i._new({**d, "id": uuid()})
        before = self.i._bundle(r) if old_id else None
        loan = self.i._loan(r["id"])
        if not old_id:
            r["status"] = "stored"
            if d["status"] == "issued" or (d["status"] == "lost" and d["recipient"]["name"]):
                self.i._issue(r, d, actor, legacy=True)
        elif loan:
            # Metadata absent from A:J stays in SQLite. Empty I explicitly clears contacts.
            if str(values.get("rawContact", "")) != (loan["recipient"].get("contact") or {}).get(
                "raw", ""
            ):
                parsed = contact(values.get("rawContact", ""))
                loan["recipient"]["contact"] = normalize_contact(parsed)
            if values.get("place", "") != loan["place"].get("name", ""):
                loan["place"] = clone(d["place"])
            self.i._put_loan(loan)
        r.update(
            inventory=d["inventory"],
            mac=d["mac"],
            note=d["note"],
            legacy=d["legacy"],
            status=d["status"],
        )
        r["storage"] = None if self.i._loan(r["id"]) else clone(d["storage"])
        if d["status"] == "lost" and (before or {}).get("router", {}).get("status") != "lost":
            r["loss"] = {
                "reason": reason or "Импорт статуса; обстоятельства уточнить",
                "at": stamp(self.i.now()),
                "actor": actor,
            }
        self.i._validate(r)
        self.i._save(r, group)
        self.s.run("INSERT OR REPLACE INTO snapshots VALUES(?,?)", r["id"], stable(entry["values"]))
        self.s.run("DELETE FROM conflicts WHERE router_id=?", r["id"])
        self.s.event(
            actor,
            r["id"],
            "Импорт из таблицы",
            before,
            self.i._bundle(r),
            reason or "Подтверждён импорт; автор исходной правки неизвестен",
        )

    def refresh_day(self):
        """Queue overdue display changes without changing the base accounting status."""
        day = local_clock(self.i.now())["day"]
        if self.s.setting("sheets_display_day") == day:
            return
        with self.s.transaction():
            for row in self.s.all("SELECT data FROM routers WHERE status='issued'"):
                r = json.loads(row["data"])
                snap = self.s.get("SELECT data FROM snapshots WHERE router_id=?", r["id"])
                if snap and not equal(
                    self._project(r)["status"], json.loads(snap["data"])["status"]
                ):
                    self.i._queue(r, uuid())
            self.s.set("sheets_display_day", day)

    def flush(self, *, limit=None, should_stop=None):
        result = dict(sent=0, blocked=0, failed=0)
        groups = self.s.all(
            "SELECT DISTINCT group_id FROM outbox WHERE next_at<=?", stamp(self.i.now())
        )
        for group in groups[:limit]:
            if should_stop and should_stop():
                break
            jobs = self.s.all("SELECT * FROM outbox WHERE group_id=?", group["group_id"])
            if any(j["state"] == "blocked" for j in jobs):
                result["blocked"] += len(jobs)
                continue
            try:
                sheet = self.a.read()
                if should_stop and should_stop():
                    break
                validate_sheet(sheet)
                writes = []
                for job in jobs:
                    r = self.i._router(job["router_id"])
                    snapshot = self.s.get("SELECT data FROM snapshots WHERE router_id=?", r["id"])
                    base = json.loads(snapshot["data"]) if snapshot else None
                    matches = [
                        row
                        for row in sheet["rows"]
                        if str(row["values"].get("inventory", "")).casefold()
                        == r["inventory"].casefold()
                    ]
                    require(len(matches) <= 1, "Дубли инвентарного номера", "conflict")
                    remote = matches[0] if matches else None
                    require(
                        remote if base else not remote,
                        "Строка удалена / номер изменён: выполните /sync"
                        if base
                        else "Номер уже есть в таблице: выполните /sync",
                        "conflict",
                    )
                    bot = self._project(r)
                    require(
                        not (
                            remote
                            and base
                            and not equal(remote["values"], base)
                            and not equal(remote["values"], bot)
                        ),
                        "Непринятые ручные правки: выполните /sync",
                        "conflict",
                    )
                    require(
                        r["version"] == job["version"],
                        "Версия исходящего задания устарела",
                        "stale",
                    )
                    writes.append(
                        {
                            "routerId": r["id"],
                            "version": r["version"],
                            "values": bot,
                            "row": (remote or {}).get("row"),
                            "previous": (remote or {}).get("values"),
                            "formulas": (remote or {}).get("formulas", []),
                        }
                    )
                self.a.write(writes)
                readback = self.a.read()
                validate_sheet(readback)
                for w in writes:
                    require(
                        any(equal(row["values"], w["values"]) for row in readback["rows"]),
                        "Не удалось подтвердить запись таблицы",
                        "conflict",
                    )
                with self.s.transaction():
                    for w in writes:
                        self.s.run(
                            "INSERT OR REPLACE INTO snapshots VALUES(?,?)",
                            w["routerId"],
                            stable(w["values"]),
                        )
                        self.s.run(
                            "DELETE FROM outbox WHERE router_id=? AND version=?",
                            w["routerId"],
                            w["version"],
                        )
                        self.s.run("DELETE FROM conflicts WHERE router_id=?", w["routerId"])
                result["sent"] += len(writes)
            except Exception as error:
                conflict = isinstance(error, DomainError) and error.code in {"conflict", "schema"}
                message = str(error) if conflict else "Таблица недоступна; будет повтор"
                with self.s.transaction():
                    for job in jobs:
                        retry = self.i.now() + timedelta(
                            seconds=min(3600, 2 ** min(job["attempts"] + 1, 12))
                        )
                        self.s.run(
                            "UPDATE outbox SET attempts=attempts+1,next_at=?,last_error=?,state=? WHERE router_id=?",
                            stamp(retry),
                            message,
                            "blocked" if conflict else "pending",
                            job["router_id"],
                        )
                        if conflict:
                            self._block(job["router_id"], {"type": "conflict", "message": message})
                result["blocked" if conflict else "failed"] += len(jobs)
        return result
