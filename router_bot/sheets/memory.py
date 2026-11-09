from ..util import clone, equal, require
from .schema import HEADERS, row_values


class MemorySheets:
    """Synthetic adapter for tests and the explicit demo mode."""

    def __init__(self, rows=None):
        self.sheet = {
            "sheetId": 966826055,
            "title": "7ка",
            "locale": "en_GB",
            "timeZone": "Europe/Moscow",
            "rowCount": 996,
            "columnCount": 30,
            "headers": HEADERS[:],
            "rules": [],
            "rows": [
                {
                    "row": n + 2,
                    "values": row_values(v) if isinstance(v, list) else clone(v),
                    "formulas": [],
                }
                for n, v in enumerate(rows or [])
            ],
        }
        self.offline = False
        self.write_calls = 0
        self.before_write = None

    def read(self):
        require(not self.offline, "Таблица недоступна", "network")
        return clone(self.sheet)

    def write(self, writes):
        require(not self.offline, "Таблица недоступна", "network")
        if self.before_write:
            self.before_write()
        updated = clone(self.sheet)
        for w in writes:
            found = [
                r
                for r in updated["rows"]
                if str(r["values"].get("inventory", "")).casefold()
                == w["values"]["inventory"].casefold()
            ]
            require(len(found) <= 1, "Дубли инвентарного номера", "conflict")
            old = found[0] if found else None
            require(
                (old is None and w["previous"] is None)
                or (old and equal(old["values"], w["previous"])),
                "Таблица изменилась перед записью",
                "conflict",
            )
            for field in (old or {}).get("formulas", []):
                require(
                    equal(old["values"].get(field), w["values"].get(field)),
                    "Изменение формулы требует отдельного разбора",
                    "conflict",
                )
            if old:
                old["values"] = clone(w["values"])
            else:
                updated["rows"].append(
                    {
                        "row": max([1] + [r["row"] for r in updated["rows"]]) + 1,
                        "values": clone(w["values"]),
                        "formulas": [],
                    }
                )
        self.sheet, self.write_calls = updated, self.write_calls + 1

    def batch(self, requests):
        require(not self.offline, "Таблица недоступна", "network")
        for request in requests:
            if "deleteConditionalFormatRule" in request:
                self.sheet["rules"].pop(request["deleteConditionalFormatRule"]["index"])
            if "addConditionalFormatRule" in request:
                r = request["addConditionalFormatRule"]
                self.sheet["rules"].insert(r["index"], clone(r["rule"]))
