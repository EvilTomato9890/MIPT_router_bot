"""Commit replies with dialog state, then deliver outside the SQLite transaction."""

import json
from contextlib import contextmanager
from datetime import timedelta

from .util import DomainError, stable, stamp


class Delivery:
    def __init__(self, inventory, telegram):
        self.i, self.s, self.telegram = inventory, inventory.s, telegram
        self.actor = None

    @contextmanager
    def capture(self, actor):
        self.actor = actor
        try:
            yield
        finally:
            self.actor = None

    def _emit(self, method, args):
        if self.actor is None:
            return getattr(self.telegram, method)(*args)
        # Callback acknowledgements are transient and carry no accounting data.
        if method == "answer":
            return
        try:
            minimum = self.i.authorize(self.actor)
        except DomainError:
            minimum = "public"
        self.s.run(
            "INSERT INTO reply_outbox(actor,minimum_role,method,data,next_at) VALUES(?,?,?,?,?)",
            self.actor,
            minimum,
            method,
            stable(args),
            stamp(self.i.now()),
        )

    def send(self, *args):
        return self._emit("send", args)

    def file(self, *args):
        return self._emit("file", args)

    def answer(self, *args):
        return self._emit("answer", args)

    def flush(self, limit=20):
        blocked, attempted = set(), 0
        for row in self.s.all("SELECT * FROM reply_outbox ORDER BY id"):
            actor = row["actor"]
            if actor in blocked:
                continue
            if row["minimum_role"] != "public":
                try:
                    self.i.authorize(actor, row["minimum_role"])
                except DomainError:
                    self.s.run("DELETE FROM reply_outbox WHERE id=?", row["id"])
                    continue
            if row["next_at"] > stamp(self.i.now()):
                blocked.add(actor)
                continue
            if attempted >= limit:
                break
            attempted += 1
            try:
                getattr(self.telegram, row["method"])(*json.loads(row["data"]))
            except Exception:
                blocked.add(actor)
                retry = self.i.now() + timedelta(seconds=min(300, 2 ** min(row["attempts"] + 1, 8)))
                self.s.run(
                    "UPDATE reply_outbox SET attempts=attempts+1,next_at=? WHERE id=?",
                    stamp(retry),
                    row["id"],
                )
            else:
                self.s.run("DELETE FROM reply_outbox WHERE id=?", row["id"])
