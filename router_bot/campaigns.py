import json
from datetime import timedelta

from .util import clone, digest, iso_date, local_clock, natural, require, stable, stamp, text, uuid


class Campaigns:
    def __init__(self, inventory, *, time="10:00"):
        self.i, self.s = inventory, inventory.s
        self.time = time

    def _get(self, identifier):
        row = self.s.get("SELECT * FROM campaigns WHERE id=?", identifier)
        require(row, "Кампания не найдена")
        return {**json.loads(row["data"]), "version": row["version"]}

    def _save(self, c):
        c["version"] = c.get("version", 0) + 1
        self.s.run(
            "INSERT INTO campaigns VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=excluded.version",
            c["id"],
            stable(c),
            c["version"],
        )

    def list(self, actor):
        self.i.authorize(actor)
        return [self._get(row["id"]) for row in self.s.all("SELECT id FROM campaigns")]

    def _activate(self, actor, campaign):
        # Previous years remain available as history; only one collection runs.
        for row in self.s.all("SELECT id FROM campaigns WHERE id<>?", campaign["id"]):
            old = self._get(row["id"])
            if old["active"]:
                previous = clone(old)
                old["active"] = False
                self._save(old)
                self.s.run(
                    "UPDATE reminders SET state='cancelled' WHERE campaign_id=? AND state='pending'",
                    old["id"],
                )
                self.s.event(actor, old["id"], "Предыдущий сбор завершён", previous, old)

    def simplify(self, actor):
        """Upgrade older databases without losing collection history."""
        self.i.authorize(actor, "admin")

        def operation(_):
            active = [c for c in self.list(actor) if c["active"]]
            if active:
                self._activate(actor, active[-1])
            return {"active": active[-1]["id"] if active else None}

        return self.s.atomic(actor, "single-collection-v1", {}, operation)

    def save(self, actor, key, data):
        self.i.authorize(actor, "admin")

        def operation(_):
            self.i.authorize(actor, "admin")
            old = self._get(data["id"]) if data.get("id") else None
            c = {**(old or {"id": uuid(), "active": False}), **clone(data)}
            require(text(c.get("name", "")), "Название кампании обязательно")
            require(
                c.get("admissionYear") or c.get("academicYear"), "Укажите набор или учебный год"
            )
            require(
                iso_date(c.get("deadline")) >= iso_date(c.get("startsAt")),
                "Срок раньше начала сбора",
            )
            require(
                isinstance(c.get("responsibleIds"), list) and c["responsibleIds"],
                "Назначьте ответственных",
            )
            for target in c["responsibleIds"]:
                self.i.authorize(target, "operator")
            for field in ("location", "hours", "instruction"):
                require(text(c.get(field, "")), "Укажите место, часы и инструкцию")
            if old and "expectedVersion" in data:
                require(old["version"] == data["expectedVersion"], "Кампания изменилась", "stale")
            if c["active"]:
                self._activate(actor, c)
            self._save(c)
            self.s.event(actor, c["id"], "Настройка кампании", old, c)
            return c

        return self.s.atomic(actor, key, {"campaign": data}, operation)

    @staticmethod
    def _eligible(loan, c):
        p = loan["recipient"]
        return (
            p["kind"] == "person"
            and loan.get("collectionEligible") is True
            and (
                (c.get("admissionYear") and p.get("admissionYear") == c["admissionYear"])
                or (c.get("academicYear") and p.get("academicYear") == c["academicYear"])
            )
        )

    def preview(self, actor, identifier):
        self.i.authorize(actor, "admin")
        c, included, clarify = self._get(identifier), [], []
        for r in self.i.list(actor):
            loan = r["loan"]
            if not loan:
                continue
            if self._eligible(loan, c):
                included.append({"router": r, "loan": loan})
            elif loan["recipient"]["kind"] == "unknown" or (
                loan["recipient"]["kind"] == "person"
                and (
                    not loan.get("collectionEligible")
                    or not (
                        loan["recipient"].get("admissionYear")
                        or loan["recipient"].get("academicYear")
                    )
                )
            ):
                clarify.append({"router": r, "loan": loan})
        return {"campaign": c, "included": included, "clarify": clarify}

    def launch(self, actor, key, identifier, expected):
        self.i.authorize(actor, "admin")

        def operation(_):
            self.i.authorize(actor, "admin")
            p = self.preview(actor, identifier)
            c = p["campaign"]
            require(c["version"] == expected, "Кампания изменилась", "stale")
            for entry in p["included"]:
                self.s.run(
                    "INSERT OR IGNORE INTO campaign_members VALUES(?,?,?)",
                    identifier,
                    entry["loan"]["id"],
                    stable({"source": "cohort", "at": stamp(self.i.now())}),
                )
            c["active"] = True
            self._activate(actor, c)
            self._save(c)
            self.s.event(
                actor,
                identifier,
                "Запуск кампании",
                None,
                {"count": len(p["included"]), "clarify": len(p["clarify"])},
            )
            return {"included": len(p["included"]), "clarify": len(p["clarify"])}

        return self.s.atomic(actor, key, {"launch": identifier, "expected": expected}, operation)

    def member(self, actor, key, identifier, loan_id, data):
        minimum = "admin" if data["action"] in {"include", "exclude"} else "operator"
        self.i.authorize(actor, minimum)

        def operation(group):
            self.i.authorize(actor, minimum)
            self._get(identifier)
            row = self.s.get("SELECT data FROM loans WHERE id=?", loan_id)
            require(row, "Выдача не найдена")
            loan = json.loads(row["data"])
            previous = self.s.get(
                "SELECT data FROM campaign_members WHERE campaign_id=? AND loan_id=?",
                identifier,
                loan_id,
            )
            require(loan["open"], "Выдача уже закрыта; обновите очередь")
            require(previous or data["action"] == "include", "Выдача не включена в кампанию")
            require(data.get("reason"), "Укажите примечание")
            m = json.loads(previous["data"]) if previous else {"source": "manual"}
            if data["action"] in {"include", "exclude"}:
                m.update(excluded=data["action"] == "exclude", reason=data["reason"])
            else:
                require(
                    data["action"] == "note",
                    "Приём оформляется физическим возвратом",
                )
                m["lastAction"] = {**data, "actor": actor, "at": stamp(self.i.now())}
            self.s.run(
                "INSERT OR REPLACE INTO campaign_members VALUES(?,?,?)",
                identifier,
                loan_id,
                stable(m),
            )
            self.s.event(
                actor,
                identifier,
                "Действие в очереди",
                json.loads(previous["data"]) if previous else None,
                {"loanId": loan_id, **m},
                data["reason"],
            )
            return m

        return self.s.atomic(
            actor, key, {"id": identifier, "loanId": loan_id, "data": data}, operation
        )

    def queue(self, actor, identifier, filters=None):
        self.i.authorize(actor)
        filters, c = filters or {}, self._get(identifier)
        today = local_clock(self.i.now())["day"]
        if c["active"]:
            for row in self.s.all("SELECT data FROM loans WHERE open=1"):
                loan = json.loads(row["data"])
                if self._eligible(loan, c):
                    self.s.run(
                        "INSERT OR IGNORE INTO campaign_members VALUES(?,?,?)",
                        identifier,
                        loan["id"],
                        stable({"source": "cohort", "at": stamp(self.i.now())}),
                    )
        result = []
        for row in self.s.all(
            "SELECT loan_id,data FROM campaign_members WHERE campaign_id=?", identifier
        ):
            loan = json.loads(
                self.s.get("SELECT data FROM loans WHERE id=?", row["loan_id"])["data"]
            )
            r, member = self.i._router(loan["routerId"]), json.loads(row["data"])
            excluded = bool(
                member.get("excluded")
                or (loan.get("exception") and loan["exception"]["reviewAt"] >= today)
            )
            due = (
                max(c["deadline"], loan["dueAt"])
                if loan.get("dueConfirmed") and loan.get("dueAt")
                else c["deadline"]
            )
            if not filters.get("includeReturned") and not loan["open"]:
                continue
            if filters.get("group") and loan["recipient"].get("group") != filters["group"]:
                continue
            if filters.get("place") and filters["place"] not in loan["place"]["name"]:
                continue
            result.append(
                {
                    "router": r,
                    "loan": loan,
                    "member": member,
                    "due": due,
                    "excluded": excluded,
                    "returned": bool(not loan["open"] and loan.get("returnedAt")),
                    "closedWithoutReturn": bool(not loan["open"] and not loan.get("returnedAt")),
                    "overdue": loan["open"] and due < today,
                    "lost": r["status"] == "lost",
                }
            )
        return sorted(
            result,
            key=lambda x: (
                x["excluded"],
                not x["overdue"],
                x["due"],
                natural(x["loan"]["place"].get("floor")),
                natural(x["loan"]["place"].get("section")),
                natural(x["loan"]["place"]["name"]),
                natural(x["router"]["inventory"]),
            ),
        )

    def report(self, actor, identifier):
        rows = self.queue(actor, identifier, {"includeReturned": True})
        return {
            "plan": len(rows),
            "returned": sum(x["returned"] for x in rows),
            "remaining": sum(x["loan"]["open"] and not x["excluded"] for x in rows),
            "closedWithoutReturn": sum(x["closedWithoutReturn"] for x in rows),
            "overdue": sum(x["overdue"] and not x["excluded"] for x in rows),
            "excluded": sum(x["excluded"] for x in rows),
            "lost": sum(x["lost"] and x["loan"]["open"] for x in rows),
        }

    def tick(self, send):
        now = self.i.now()
        clock = local_clock(now)
        day = clock["day"]
        sent, failed = 0, 0
        if clock["time"] < self.time:
            return {"sent": sent, "failed": failed}
        for row in self.s.all("SELECT id FROM campaigns"):
            c = self._get(row["id"])
            if not c["active"] or day < c["startsAt"]:
                continue
            admin = self.s.get("SELECT user_id FROM roles WHERE role='admin'")
            if not admin:
                continue
            queue = [x for x in self.queue(admin["user_id"], c["id"]) if not x["excluded"]]
            candidate = [[x["loan"]["id"], x["due"], day] for x in queue]
            signature, live = digest(candidate), set()
            for target in c["responsibleIds"]:
                try:
                    self.i.authorize(target, "operator")
                except Exception:
                    continue
                if not candidate:
                    continue
                key = f"{c['id']}:{target}:{signature}"
                live.add(key)
                if self.s.setting(f"notice:{c['id']}:{target}") == day:
                    continue
                self.s.run(
                    "INSERT INTO reminders VALUES(?,?,?,?,'pending',0,NULL) ON CONFLICT(key) DO UPDATE SET state=CASE WHEN reminders.state='cancelled' THEN 'pending' ELSE reminders.state END",
                    key,
                    c["id"],
                    target,
                    stamp(now),
                )
            for pending in self.s.all(
                "SELECT * FROM reminders WHERE campaign_id=? AND state='pending'", c["id"]
            ):
                if pending["key"] not in live:
                    self.s.run("UPDATE reminders SET state='cancelled' WHERE key=?", pending["key"])
                    continue
                if pending["due_at"] > stamp(now):
                    continue
                try:
                    self.i.authorize(pending["target"], "operator")
                    send(
                        pending["target"],
                        f"Сбор: {c['name']}\nОсталось: {len(queue)}; просрочено: {sum(x['overdue'] for x in queue)}; утрата: {sum(x['lost'] for x in queue)}.\n{c['location']}, {c['hours']}\n{c['instruction']}\nОткройте «Сбор» для очереди.",
                    )
                    with self.s.transaction():
                        self.s.run(
                            "UPDATE reminders SET state='sent',due_at=? WHERE key=?",
                            stamp(now),
                            pending["key"],
                        )
                        self.s.set(f"notice:{c['id']}:{pending['target']}", day)
                    sent += 1
                except Exception:
                    retry = now + timedelta(
                        seconds=min(3600, 30 * 2 ** min(pending["attempts"], 7))
                    )
                    self.s.run(
                        "UPDATE reminders SET attempts=attempts+1,due_at=?,last_error=? WHERE key=?",
                        stamp(retry),
                        "Доставка не подтверждена; будет повтор",
                        pending["key"],
                    )
                    failed += 1
        return {"sent": sent, "failed": failed}
