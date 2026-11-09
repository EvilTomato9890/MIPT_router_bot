"""SQLite transactions and durable workflow state. Schema v1 stays compatible."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .util import digest, now_utc, redact, require, stable, stamp, uuid


class Store:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;"
        )
        migration = Path(__file__).with_name("migrations") / "001_initial.sql"
        self.db.executescript(migration.read_text(encoding="utf-8"))
        self.run("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", stamp())

    def get(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def run(self, sql, *args):
        return self.db.execute(sql, args)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def atomic(self, actor, key, payload, operation):
        require(isinstance(key, str) and 0 < len(key) <= 200, "Нужен ключ операции")
        with self.transaction():
            previous = self.get("SELECT * FROM requests WHERE key=?", key)
            signature = digest(payload)
            if previous:
                require(
                    previous["actor"] == actor and previous["signature"] == signature,
                    "Ключ уже использован для другой операции",
                )
                return json.loads(previous["result"])
            result = operation(uuid())
            self.run("INSERT INTO requests VALUES(?,?,?,?)", key, actor, signature, stable(result))
            return result

    def setting(self, key, fallback=None):
        row = self.get("SELECT value FROM settings WHERE key=?", key)
        return json.loads(row["value"]) if row else fallback

    def set(self, key, value):
        self.run(
            "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key,
            stable(value),
        )

    def event(
        self, actor, object_id, action, before, after, reason=None, correction_of=None, at=None
    ):
        event_id = uuid()
        self.run(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
            event_id,
            at or stamp(),
            actor,
            object_id,
            action,
            stable(redact(before)),
            stable(redact(after)),
            reason,
            correction_of,
        )
        return event_id

    def draft(self, user, kind, data, now=None):
        draft_id = uuid()
        self.run(
            "INSERT INTO drafts VALUES(?,?,?,?,?)",
            draft_id,
            user,
            kind,
            stable(data),
            stamp((now or now_utc()) + timedelta(minutes=30)),
        )
        return draft_id

    def get_draft(self, user, draft_id, now=None):
        row = self.get("SELECT * FROM drafts WHERE id=?", draft_id)
        require(
            row and row["user_id"] == user and row["expires_at"] > stamp(now),
            "Подтверждение устарело или принадлежит другому сотруднику",
        )
        return {**row, "data": json.loads(row["data"])}

    def activate_python(self):
        """Invalidate only unfinished dialogs when switching engines, once."""
        if self.setting("runtime_engine") != "python-v1":
            with self.transaction():
                for row in self.all("SELECT id,data FROM routers"):
                    data = json.loads(row["data"])
                    data.pop("kit", None)
                    if isinstance(data.get("storage"), dict):
                        data["storage"].pop("type", None)
                    self.run("UPDATE routers SET data=? WHERE id=?", stable(data), row["id"])
                for row in self.all("SELECT id,data FROM loans"):
                    data = json.loads(row["data"])
                    location = data.get("place") or {}
                    data.setdefault(
                        "collectionEligible",
                        location.get("type") == "room"
                        and location.get("certainty") == "confirmed"
                        and data.get("recipient", {}).get("kind") == "person",
                    )
                    location.pop("type", None)
                    for field in ("kitOut", "kitIn"):
                        data.pop(field, None)
                    self.run("UPDATE loans SET data=? WHERE id=?", stable(data), row["id"])
                fields = {
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
                }
                for row in self.all("SELECT router_id,data FROM snapshots"):
                    values = json.loads(row["data"])
                    self.run(
                        "UPDATE snapshots SET data=? WHERE router_id=?",
                        stable({k: v for k, v in values.items() if k in fields}),
                        row["router_id"],
                    )
                self.run("DELETE FROM sessions")
                self.run("DELETE FROM drafts")
                self.set("runtime_engine", "python-v1")

    def backup(self, target):
        target = Path(target).resolve()
        origin = self.get("PRAGMA database_list")["file"]
        require(not origin or Path(origin).resolve() != target, "Копия должна иметь другой путь")
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as connection:
            self.db.backup(connection)
            require(
                connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "Копия не прошла проверку",
            )

    def close(self):
        self.db.close()
