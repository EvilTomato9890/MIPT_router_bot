import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import config
from .db import Store
from .main import adapter_for, process_lock, run
from .service import Inventory, place
from .sheets.memory import MemorySheets
from .sheets.schema import validate_sheet
from .sheets.sync import Synchronizer
from .util import DomainError, require, stamp


def demo():
    store = Store()
    try:
        inventory = Inventory(
            store, bootstrap_admin_id=1, now=lambda: datetime(2027, 6, 1, 10, tzinfo=UTC)
        )
        adapter = MemorySheets()
        sync = Synchronizer(inventory, adapter)
        result = inventory.execute(
            1,
            "demo:add",
            {
                "kind": "add",
                "inventory": "DEMO-001",
                "condition": "working",
                "storage": place("Тестовый склад"),
            },
        )
        inventory.execute(
            1,
            "demo:issue",
            {
                "kind": "issue",
                "routerId": result["routerId"],
                "recipient": {
                    "kind": "person",
                    "name": "Тестовый получатель",
                    "contact": {"raw": "+70000000000", "phone": "+70000000000"},
                },
                "place": place("Тестовая 101"),
                "dueAt": "2027-07-01",
            },
        )
        print("ДЕМОНСТРАЦИЯ: настоящие Telegram и Google не используются.")
        print("Тестовая отправка:", sync.flush())
        adapter.sheet["rows"][0]["values"]["note"] = "Ручная правка тестовой таблицы"
        preview = sync.preview(1)
        print("Тестовый импорт:", sync.apply(1, preview["id"]))
        print("Статистика:", inventory.stats(1))
    finally:
        store.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Учёт роутеров общежития № 7")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Запустить бота (также действие по умолчанию)")
    sub.add_parser("demo", help="Проверить цикл без внешних подключений")
    backup = sub.add_parser("backup", help="Создать и проверить отдельную копию SQLite")
    backup.add_argument("target", nargs="?")
    sub.add_parser("inspect-sheets", help="Только чтение структуры и агрегатов Google")
    sub.add_parser(
        "pause-sync", help="При остановленном боте заблокировать outbox перед восстановлением"
    )
    args = parser.parse_args()
    try:
        if args.command in (None, "run"):
            run()
        elif args.command == "demo":
            demo()
        elif args.command == "backup":
            c = config(require_bot=False)
            require(Path(c.db).is_file(), "Исходная БД не найдена; пустая копия не создаётся")
            target = args.target or str(
                Path(c.backup_dir) / ("manual-" + stamp().replace(":", "-") + ".sqlite")
            )
            store = Store(c.db)
            try:
                store.backup(target)
            finally:
                store.close()
            print("Резервная копия создана и проверена:", Path(target).resolve())
        elif args.command == "pause-sync":
            c = config(require_bot=False)
            require(Path(c.db).is_file(), "БД не найдена")
            with process_lock(c.db):
                store = Store(c.db)
                try:
                    store.run("UPDATE outbox SET state='blocked'")
                    for row in store.all("SELECT router_id FROM outbox"):
                        store.run(
                            "INSERT OR REPLACE INTO conflicts VALUES(?,?)",
                            row["router_id"],
                            json.dumps(
                                {
                                    "type": "review",
                                    "message": "Восстановление БД: согласуйте /sync до отправки",
                                },
                                ensure_ascii=False,
                            ),
                        )
                finally:
                    store.close()
            print("Отправка приостановлена. После запуска выполните /sync и проверьте расхождения.")
        elif args.command == "inspect-sheets":
            c = config(require_bot=False)
            require(c.mode == "google", "Задайте SHEETS_MODE=google и Google-доступ")
            sheet = adapter_for(c).read()
            validate_sheet(sheet)
            rows = [r for r in sheet["rows"] if str(r["values"].get("inventory", "")).strip()]
            statuses = {}
            for row in rows:
                key = str(row["values"]["status"]).strip()
                statuses[key] = statuses.get(key, 0) + 1
            print(
                json.dumps(
                    {
                        "readOnly": True,
                        "title": sheet["title"],
                        "sheetId": sheet["sheetId"],
                        "headers": sheet["headers"],
                        "records": len(rows),
                        "statuses": statuses,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    except Exception as error:
        print(
            str(error)
            if isinstance(error, DomainError)
            else "Не удалось выполнить команду. Проверьте конфигурацию, каталог данных и подключение.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
