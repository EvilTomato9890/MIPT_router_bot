from urllib.parse import parse_qs, urlparse

import pytest

from router_bot.sheets.colors import color_plan, expected_rules
from router_bot.sheets.google import GoogleSheets
from router_bot.sheets.schema import FIELDS, HEADERS
from router_bot.util import DomainError, clone


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class Wire:
    def __init__(self):
        self.calls = []
        self.cells = [HEADERS[:], ["G-1", "", "101", "Свободен", False, "", "", "", "", ""]]
        self.formulas = {}

    def request(self, method, url, *, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": clone(json)})
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "includeGridData" in query:
            values = [
                {
                    "values": [
                        {
                            "userEnteredValue": {"formulaValue": self.formulas[(ri, ci)]}
                            if (ri, ci) in self.formulas
                            else {"stringValue": str(value)}
                        }
                        for ci, value in enumerate(row)
                    ]
                }
                for ri, row in enumerate(self.cells)
            ]
            return Response(
                {"sheets": [{"data": [{"startRow": 0, "startColumn": 0, "rowData": values}]}]}
            )
        if parsed.path.endswith("/values:batchGet"):
            return Response({"valueRanges": [{"values": clone(self.cells)}]})
        if parsed.path.endswith("/values:batchUpdate"):
            assert json["valueInputOption"] == "RAW"
            import re

            for data in json["data"]:
                match = re.search(r"!([A-J])(\d+)$", data["range"])
                assert match, "Запись должна ограничиваться A:J"
                column, row = ord(match[1]) - 65, int(match[2]) - 1
                while len(self.cells) <= row:
                    self.cells.append([""] * 10)
                self.cells[row][column] = data["values"][0][0]
            return Response({})
        if parsed.path.endswith(":batchUpdate"):
            return Response({})
        return Response(
            {
                "properties": {"locale": "en_GB", "timeZone": "Europe/Moscow"},
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 966826055,
                            "title": "7ка",
                            "gridProperties": {"rowCount": 996, "columnCount": 30},
                        },
                        "conditionalFormats": [],
                    },
                    {
                        "properties": {
                            "sheetId": 0,
                            "title": "2ка",
                            "gridProperties": {"rowCount": 1000, "columnCount": 26},
                        }
                    },
                ],
            }
        )


def adapter():
    wire = Wire()
    return GoogleSheets(
        spreadsheet_id="test-id", sheet_id=966826055, title="7ка", client=wire
    ), wire


def test_google_transport_reads_and_writes_only_existing_a_to_j():
    a, wire = adapter()
    sheet = a.read()
    assert sheet["headers"] == HEADERS and sheet["headers"][8][0] == "С"
    old = sheet["rows"][0]
    values = {**old["values"], "note": '=IMPORTXML("https://example.test","//x")'}
    a.write(
        [
            {
                "routerId": "internal-only",
                "row": 2,
                "values": values,
                "previous": old["values"],
                "formulas": [],
            }
        ]
    )
    writes = [call for call in wire.calls if call["method"] == "POST"]
    assert len(writes) == 1 and writes[0]["data"]["valueInputOption"] == "RAW"
    assert writes[0]["data"]["data"] == [{"range": "'7ка'!J2", "values": [[values["note"]]]}]
    for call in wire.calls:
        query = parse_qs(urlparse(call["url"]).query)
        if "ranges" in query:
            assert query["ranges"] == ["'7ка'!A1:J996"]
    assert not hasattr(a, "provision")


def test_google_real_formula_preserved_literal_equal_sign_is_text():
    a, wire = adapter()
    wire.cells[1][9] = "Вычисленное значение"
    wire.formulas[(1, 9)] = "=B2"
    sheet = a.read()
    assert sheet["rows"][0]["formulas"] == ["note"]
    old = sheet["rows"][0]["values"]
    with pytest.raises(DomainError, match="формул"):
        a.write(
            [
                {
                    "routerId": "test",
                    "row": 2,
                    "previous": old,
                    "values": {**old, "note": "Перезаписать"},
                }
            ]
        )
    assert not any(call["method"] == "POST" for call in wire.calls)
    wire.formulas.clear()
    wire.cells[1][9] = "=буквальный текст"
    assert a.read()["rows"][0]["formulas"] == []


def test_google_late_manual_edit_blocks_write():
    a, wire = adapter()
    sheet = a.read()
    old = sheet["rows"][0]["values"]
    wire.cells[1][9] = "Поздняя ручная правка"
    with pytest.raises(DomainError, match="непосредственно"):
        a.write([{"routerId": "test", "row": 2, "previous": old, "values": {**old, "note": "Бот"}}])
    assert not any(call["method"] == "POST" for call in wire.calls)


@pytest.mark.parametrize(
    "mutation",
    [
        {"appendDimension": {"sheetId": 966826055, "dimension": "COLUMNS", "length": 1}},
        {"updateCells": {"range": {"sheetId": 966826055, "startRowIndex": 0}}},
        {"deleteConditionalFormatRule": {"sheetId": 0, "index": 0}},
    ],
)
def test_adapter_cannot_add_columns_or_modify_headers_other_sheet(mutation):
    a, wire = adapter()
    with pytest.raises(DomainError):
        a.batch([mutation])
    assert wire.calls == []


def test_google_network_error_is_safe_and_explicit():
    class Failed:
        def request(self, *args, **kwargs):
            raise RuntimeError("private key / bearer token / secret")

    a = GoogleSheets(spreadsheet_id="test-id", sheet_id=966826055, title="7ка", client=Failed())
    with pytest.raises(DomainError, match="Google Sheets недоступна") as error:
        a.read()
    assert "secret" not in str(error.value) and error.value.code == "network"


def legacy_rule():
    return {
        "ranges": [
            {
                "sheetId": 966826055,
                "startRowIndex": 0,
                "endRowIndex": 996,
                "startColumnIndex": 0,
                "endColumnIndex": 10,
            }
        ],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": '=$D1="Выдан"'}],
            },
            "format": {"backgroundColor": {"red": 1, "green": 0.9, "blue": 0.6}},
        },
    }


def test_color_repair_preserves_checked_foreign_and_data_no_duplicates(ctx):
    checked = {
        "ranges": [{"sheetId": 966826055, "startColumnIndex": 0, "endColumnIndex": 10}],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": "=$E1=ИСТИНА"}],
            },
            "format": {"textFormat": {"strikethrough": True}},
        },
    }
    foreign = {
        "ranges": [{"sheetId": 966826055, "startColumnIndex": 11, "endColumnIndex": 14}],
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Легенда"}]},
            "format": {"textFormat": {"bold": True}},
        },
    }
    ctx.a.sheet["rules"] = [legacy_rule(), checked, foreign, legacy_rule()]
    before = clone(ctx.a.sheet["rows"])
    plan = ctx.colors.preview(1)
    assert any("дублируется" in issue for issue in plan["issues"])
    ctx.colors.apply(1, plan["id"])
    assert len(ctx.a.sheet["rules"]) == 8
    assert ctx.a.sheet["rules"][-2:] == [checked, foreign]
    assert ctx.a.sheet["rows"] == before
    repeat = ctx.colors.preview(1)
    assert not repeat["changed"]
    ctx.colors.apply(1, repeat["id"])
    count = ctx.s.get("SELECT count(*) AS n FROM events")["n"]
    ctx.colors.apply(1, repeat["id"])
    assert ctx.s.get("SELECT count(*) AS n FROM events")["n"] == count


def test_colors_use_only_status_and_a_to_j_no_hidden_date_columns(ctx):
    rules = expected_rules(ctx.a.sheet)
    assert len(rules) == 6
    for rule in rules:
        target = rule["ranges"][0]
        assert (
            target["startRowIndex"] == 1
            and target["endColumnIndex"] == 10
            and "endRowIndex" not in target
        )
        formula = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        assert "LEN(TRIM(TO_TEXT($A2)))>0" in formula
        assert (
            "TODAY" not in formula
            and "$H" not in formula
            and "$AD" not in formula
            and "$AE" not in formula
        )
    assert '"?"' in rules[4]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
    assert '"Неизвестно"' in rules[4]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]


def test_color_rights_staleness_and_locale(ctx):
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.colors.preview(2)
    p = ctx.colors.preview(1)
    ctx.a.sheet["rules"].append(legacy_rule())
    with pytest.raises(DomainError, match="Правила изменились"):
        ctx.colors.apply(1, p["id"])
    ctx.a.sheet["locale"] = "ru_RU"
    with pytest.raises(DomainError, match="локал"):
        color_plan(ctx.a.sheet)
    assert set(FIELDS) == set(ctx.sync._project(ctx.i._new({"inventory": "TEST"})))
