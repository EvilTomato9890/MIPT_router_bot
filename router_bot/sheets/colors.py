import json
import re

from ..util import clone, digest, equal, require
from .schema import validate_sheet

MARKER = "routerbot:status:v2"
PALETTE = {
    "Просрочен": [0.95686275, 0.78039217, 0.7647059],
    "Утерян": [0.63529414, 0.76862746, 0.7882353],
    "Свободен": [0.7176471, 0.88235295, 0.8039216],
    "Выдан": [1, 0.8980392, 0.6],
    "Неизвестно": [0.86, 0.86, 0.86],
    "Списан": [0.76, 0.69, 0.83],
}


def expected_rules(sheet):
    result = []
    for label, color in PALETTE.items():
        condition = (
            'OR(TRIM($D2)="?",TRIM($D2)="Неизвестно")'
            if label == "Неизвестно"
            else f'TRIM($D2)="{label}"'
        )
        formula = f'=AND(N("{MARKER}")=0,LEN(TRIM(TO_TEXT($A2)))>0,{condition})'
        result.append(
            {
                "ranges": [
                    {
                        "sheetId": sheet["sheetId"],
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 10,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": formula}],
                    },
                    "format": {
                        "backgroundColor": dict(zip(("red", "green", "blue"), color)),
                        "textFormat": {"foregroundColor": {"red": 0.1, "green": 0.1, "blue": 0.1}},
                    },
                },
            }
        )
    return result


def formula(rule):
    return (rule.get("booleanRule", {}).get("condition", {}).get("values") or [{}])[0].get(
        "userEnteredValue", ""
    )


def managed(rule, sheet_id):
    f = formula(rule)
    if "routerbot:status:" in f:
        return True
    return bool(
        re.fullmatch(r'=\$D\d+\s*=\s*"(?:Свободен|Выдан|Просрочен|Утерян|\?|Неизвестно|Списан)"', f)
        and rule.get("ranges")
        and all(
            r["sheetId"] == sheet_id
            and r.get("startColumnIndex", 0) == 0
            and r.get("endColumnIndex") == 10
            for r in rule["ranges"]
        )
        and (
            rule["booleanRule"].get("format", {}).get("backgroundColor")
            or rule["booleanRule"].get("format", {}).get("backgroundColorStyle")
        )
    )


def canonical(rule):
    value = clone(rule)
    fmt = value.get("booleanRule", {}).get("format", {})
    if "backgroundColorStyle" in fmt:
        fmt["backgroundColor"] = fmt.pop("backgroundColorStyle")["rgbColor"]
    font = fmt.get("textFormat", {})
    if "foregroundColorStyle" in font:
        font["foregroundColor"] = font.pop("foregroundColorStyle")["rgbColor"]
    for color in (fmt.get("backgroundColor"), font.get("foregroundColor")):
        if color is not None:
            for channel in ("red", "green", "blue"):
                color[channel] = round(float(color.get(channel, 0)), 5)
    for target in value.get("ranges", []):
        target.setdefault("startColumnIndex", 0)
        target.setdefault("startRowIndex", 0)
    return value


def color_plan(sheet):
    validate_sheet(sheet)
    require(sheet["locale"] in {"en_GB", "en_US"}, "Другая локаль требует проверки формул на копии")
    rules, expected = sheet.get("rules", []), expected_rules(sheet)
    indices = [i for i, rule in enumerate(rules) if managed(rule, sheet["sheetId"])]
    correct = len(indices) == len(expected) and all(
        i == j and equal(canonical(rules[i]), canonical(expected[j])) for j, i in enumerate(indices)
    )
    requests, issues = [], []
    if not correct:
        requests += [
            {"deleteConditionalFormatRule": {"sheetId": sheet["sheetId"], "index": i}}
            for i in reversed(indices)
        ]
        requests += [
            {"addConditionalFormatRule": {"rule": rule, "index": i}}
            for i, rule in enumerate(expected)
        ]
        for index, label in enumerate(PALETTE):
            matches = [
                i
                for i in indices
                if f'"{label}"' in formula(rules[i])
                or (label == "Неизвестно" and '"?"' in formula(rules[i]))
            ]
            if not matches:
                issues.append(f"{label}: правило отсутствует")
            if len(matches) > 1:
                issues.append(f"{label}: дублируется")
            for i in matches:
                if i != index or not equal(canonical(rules[i]), canonical(expected[index])):
                    issues.append(f"{label}: исправить формулу, диапазон, цвет или приоритет")
    return {
        "digest": digest(
            {"rules": rules, "headers": sheet["headers"][:10], "locale": sheet["locale"]}
        ),
        "requests": requests,
        "issues": issues,
        "recognized": len(indices),
        "foreign": len(rules) - len(indices),
        "expected": len(expected),
        "legacyChecked": [r for r in rules if re.search(r"\$E\d+", formula(r))],
        "changed": bool(requests),
        "notes": [
            "Только A2:J, без фиксированного конца и без строк с пустым номером.",
            "Checked и посторонние правила сохраняются.",
            "Цвет зависит от Status. Подтверждённую просрочку вычисляет бот в SQLite и отправляет в D.",
            "Столбцы и данные оборудования не добавляются и не изменяются этой командой.",
        ],
    }


class ColorRepair:
    def __init__(self, inventory, adapter):
        self.i, self.a = inventory, adapter

    def preview(self, actor):
        self.i.authorize(actor, "admin")
        plan = color_plan(self.a.read())
        self.i.authorize(actor, "admin")
        return {"id": self.i.s.draft(actor, "colors", plan, self.i.now()), **plan}

    def apply(self, actor, draft_id):
        self.i.authorize(actor, "admin")
        draft = self.i.s.get_draft(actor, draft_id, self.i.now())
        require(draft["kind"] == "colors", "Неверное подтверждение")
        previous = self.i.s.get(
            "SELECT actor,result FROM requests WHERE key=?", "colors:" + draft_id
        )
        if previous:
            require(previous["actor"] == actor, "Нет доступа", "forbidden")
            return json.loads(previous["result"])
        sheet = self.a.read()
        fresh = color_plan(sheet)
        self.i.authorize(actor, "admin")
        require(
            fresh["digest"] == draft["data"]["digest"],
            "Правила изменились после просмотра; повторите /repair_colors",
            "stale",
        )
        self.a.batch(fresh["requests"])
        require(not color_plan(self.a.read())["changed"], "Правила не прошли проверку после записи")

        def operation(_):
            self.i.authorize(actor, "admin")
            self.i.s.event(
                actor,
                "sheet:" + str(sheet["sheetId"]),
                "Восстановление раскраски",
                None,
                {"recognized": fresh["recognized"], "expected": fresh["expected"]},
            )
            return {"changed": fresh["changed"], "checkedPreserved": len(fresh["legacyChecked"])}

        return self.i.s.atomic(actor, "colors:" + draft_id, {"color": draft_id}, operation)
