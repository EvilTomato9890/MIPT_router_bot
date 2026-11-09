"""Google Sheets transport restricted to the existing A:J accounting columns."""

from urllib.parse import urlencode

from ..util import DomainError, equal, require
from .schema import FIELDS, row_values, validate_sheet


class GoogleSheets:
    def __init__(self, *, spreadsheet_id, sheet_id, title, credentials=None, client=None):
        require(
            spreadsheet_id and all(c.isalnum() or c in "_-" for c in spreadsheet_id),
            "Неверный Spreadsheet ID",
        )
        self.id, self.sheet_id, self.title = spreadsheet_id, sheet_id, title
        self.credentials, self.client = credentials, client

    def request(self, path, method="GET", body=None):
        try:
            if self.client is None:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2.service_account import Credentials

                credentials = Credentials.from_service_account_file(
                    self.credentials, scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                self.client = AuthorizedSession(credentials)
            response = self.client.request(
                method,
                f"https://sheets.googleapis.com/v4/spreadsheets/{self.id}{path}",
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            raise DomainError(
                "Google Sheets недоступна. Локальные данные сохранены; проверьте подключение и повторите команду.",
                "network",
            ) from None

    def read(self):
        meta = self.request(
            "?fields=spreadsheetId,properties(locale,timeZone),sheets(properties,conditionalFormats)"
        )
        target = next(
            (s for s in meta["sheets"] if s["properties"]["sheetId"] == self.sheet_id), None
        )
        require(
            target and target["properties"]["title"] == self.title,
            "ID или имя целевого листа изменились; запись остановлена",
            "schema",
        )
        grid = target["properties"]["gridProperties"]
        prefix = "'" + self.title.replace("'", "''") + "'!"
        ranges = urlencode({"ranges": f"{prefix}A1:J{grid['rowCount']}"})
        values = self.request(
            "/values:batchGet?" + ranges + "&valueRenderOption=UNFORMATTED_VALUE"
        )["valueRanges"][0].get("values", [])
        data = self.request(
            "?"
            + ranges
            + "&includeGridData=true&fields=sheets(data(startRow,startColumn,rowData(values(userEnteredValue))))"
        )
        formulas, grid_end = {}, 0
        for sheet in data.get("sheets", []):
            for block in sheet.get("data", []):
                grid_end = max(grid_end, block.get("startRow", 0) + len(block.get("rowData", [])))
                for ri, row in enumerate(block.get("rowData", [])):
                    for ci, cell in enumerate(row.get("values", [])):
                        index = block.get("startColumn", 0) + ci
                        if cell.get("userEnteredValue", {}).get("formulaValue") and index < 10:
                            number = block.get("startRow", 0) + ri + 1
                            formulas.setdefault(number, []).append(FIELDS[index])
        rows = [
            {
                "row": i + 1,
                "values": row_values(values[i] if i < len(values) else []),
                "formulas": formulas.get(i + 1, []),
            }
            for i in range(1, max(len(values), grid_end))
        ]
        return {
            "sheetId": self.sheet_id,
            "title": self.title,
            "headers": values[0] if values else [],
            "rows": rows,
            "rules": target.get("conditionalFormats", []),
            "rowCount": grid["rowCount"],
            "columnCount": grid["columnCount"],
            **meta["properties"],
        }

    def write(self, writes):
        sheet = self.read()
        validate_sheet(sheet)
        used = {
            row["row"]
            for row in sheet["rows"]
            if row["formulas"]
            or any(k != "checked" and v not in (None, "") for k, v in row["values"].items())
        }
        data, maximum = [], sheet["rowCount"]
        for w in writes:
            matches = [
                r
                for r in sheet["rows"]
                if str(r["values"].get("inventory", "")).casefold()
                == w["values"]["inventory"].casefold()
            ]
            require(len(matches) <= 1, "Дубли инвентарного номера", "conflict")
            old = matches[0] if matches else None
            require(
                (old is None and w["previous"] is None)
                or (old and equal(old["values"], w["previous"])),
                "Таблица изменилась непосредственно перед записью",
                "conflict",
            )
            number = old["row"] if old else 2
            if not old:
                while number in used:
                    number += 1
                used.add(number)
                maximum = max(maximum, number)
            for index, field in enumerate(FIELDS):
                if old and equal(old["values"].get(field), w["values"].get(field)):
                    continue
                require(
                    not old or field not in old["formulas"],
                    "Бот не перезаписывает формулу; разберите поле вручную",
                    "conflict",
                )
                prefix = "'" + self.title.replace("'", "''") + "'!"
                data.append(
                    {
                        "range": f"{prefix}{chr(65 + index)}{number}",
                        "values": [[w["values"].get(field, "")]],
                    }
                )
        if maximum > sheet["rowCount"]:
            self.batch(
                [
                    {
                        "appendDimension": {
                            "sheetId": self.sheet_id,
                            "dimension": "ROWS",
                            "length": maximum - sheet["rowCount"],
                        }
                    }
                ]
            )
        if data:
            self.request("/values:batchUpdate", "POST", {"valueInputOption": "RAW", "data": data})

    def batch(self, requests):
        # Defence in depth: no schema/column/header mutation is accepted by this adapter.
        for request in requests:
            require(
                set(request)
                <= {"appendDimension", "deleteConditionalFormatRule", "addConditionalFormatRule"},
                "Изменение схемы таблицы запрещено",
            )
            if "appendDimension" in request:
                require(
                    request["appendDimension"]["dimension"] == "ROWS"
                    and request["appendDimension"]["sheetId"] == self.sheet_id,
                    "Добавление столбцов запрещено",
                )
            if "deleteConditionalFormatRule" in request:
                require(
                    request["deleteConditionalFormatRule"]["sheetId"] == self.sheet_id,
                    "Другой лист вне управления",
                )
            if "addConditionalFormatRule" in request:
                require(
                    all(
                        r["sheetId"] == self.sheet_id
                        and r.get("startColumnIndex", 0) == 0
                        and r["endColumnIndex"] == 10
                        for r in request["addConditionalFormatRule"]["rule"]["ranges"]
                    ),
                    "Раскраска допускается только в A:J",
                )
        if requests:
            self.request(":batchUpdate", "POST", {"requests": requests})
