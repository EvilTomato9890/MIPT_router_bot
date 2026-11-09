"""Resolve only identities actually supplied by Telegram or confirmed by staff."""

import json
import re

from .util import clone, contact_text, require, stamp, valid_id


def observe(store, user, now):
    if not user or not valid_id(user.get("id")) or user.get("is_bot"):
        return
    username = user.get("username") or None
    if username and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{4,31}", username):
        username = None
    if username:
        store.run(
            "UPDATE telegram_users SET username=NULL WHERE username=? AND user_id<>?",
            username,
            user["id"],
        )
    store.run(
        "INSERT INTO telegram_users VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
        "username=excluded.username,observed_at=excluded.observed_at",
        user["id"],
        username,
        stamp(now),
    )


def resolve(store, value):
    result = clone(value or {})
    username, identifier = result.get("telegram"), result.get("userId")
    require(identifier is None or valid_id(identifier), "Неверный Telegram ID")
    known = (
        store.get("SELECT user_id FROM telegram_users WHERE username=?", username)
        if username
        else None
    )
    if known:
        require(
            not identifier or identifier == known["user_id"],
            "Telegram ID не соответствует известному @нику",
        )
        identifier = known["user_id"]
    elif username and not identifier:
        candidates = set()
        for row in store.all("SELECT data FROM loans"):
            loan = json.loads(row["data"])
            saved = loan["recipient"].get("contact") or {}
            if (saved.get("telegram") or "").casefold() != username.casefold():
                continue
            candidate = saved.get("userId")
            observed = store.get("SELECT username FROM telegram_users WHERE user_id=?", candidate)
            if valid_id(candidate) and (
                not observed or (observed["username"] or "").casefold() == username.casefold()
            ):
                candidates.add(candidate)
        if len(candidates) == 1:
            identifier = candidates.pop()
    result["userId"] = identifier
    result["raw"] = contact_text(result)
    return result
