"""Keep explicit contact data without inferring IDs from usernames or past loans."""

from .util import clone, contact_text, require, valid_id


def normalize(value):
    result = clone(value or {})
    identifier = result.get("userId")
    require(identifier is None or valid_id(identifier), "Неверный Telegram ID")
    result["userId"] = identifier
    result["raw"] = contact_text(result)
    return result
