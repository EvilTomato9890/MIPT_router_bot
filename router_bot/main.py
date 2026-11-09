"""Single-process polling runner with persistent maintenance state."""

import json
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .bot import Bot
from .campaigns import Campaigns
from .config import config
from .db import Store
from .service import Inventory
from .sheets.colors import ColorRepair, color_plan
from .sheets.google import GoogleSheets
from .sheets.memory import MemorySheets
from .sheets.sync import Synchronizer
from .telegram import Telegram
from .util import DomainError, local_clock, now_utc, parse_stamp, stamp

log = logging.getLogger("router_bot")


@contextmanager
def process_lock(path):
    lock = Path(str(Path(path).resolve()) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    file = lock.open("a+b")
    try:
        if file.seek(0, 2) == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DomainError("Другой процесс бота уже работает с этой БД") from None
        yield
    finally:
        # OS releases the advisory lock on close, including after a process crash.
        file.close()


def adapter_for(c):
    if c.mode == "google":
        return GoogleSheets(
            spreadsheet_id=c.spreadsheet_id,
            sheet_id=c.sheet_id,
            title=c.title,
            credentials=c.credentials,
        )
    return MemorySheets() if c.mode == "memory" else None


def migrate(store, backup_dir):
    if store.setting("runtime_engine") == "python-v1":
        return
    if store.get("SELECT count(*) AS n FROM routers")["n"]:
        target = Path(backup_dir) / (
            "before-python-" + now_utc().strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite"
        )
        store.backup(target)
        log.info("Создана копия БД перед переходом на Python.")
    store.activate_python()


def maintain(store, inventory, telegram, campaigns, c, sync=None, adapter=None):
    now, day = inventory.now(), local_clock(inventory.now())["day"]
    if sync:
        sync.refresh_day()
        sync.flush()
    if adapter and store.setting("colors_managed"):
        try:
            plan = color_plan(adapter.read())
            if plan["changed"]:
                adapter.batch(plan["requests"])
        except Exception:
            pass
    campaigns.tick(
        lambda actor, message: (
            inventory.authorize(actor, "operator"),
            telegram.send(actor, message),
        )
    )
    for row in store.all("SELECT user_id,data FROM sessions"):
        updated = json.loads(row["data"]).get("updatedAt")
        if not updated or parse_stamp(updated) + timedelta(minutes=30) < now:
            store.run("DELETE FROM sessions WHERE user_id=?", row["user_id"])
    store.run("DELETE FROM drafts WHERE expires_at<?", stamp(now))
    admins = [r["user_id"] for r in store.all("SELECT user_id FROM roles WHERE role='admin'")]
    if c.retention_days and admins and store.setting("privacy_day") != day:
        inventory.purge_contacts(admins[0], c.retention_days, "privacy:" + day)
        store.set("privacy_day", day)
    if store.setting("backup_day") != day:
        try:
            target = Path(c.backup_dir)
            target.mkdir(parents=True, exist_ok=True)
            store.backup(target / f"router-bot-{day}.sqlite")
            store.set("backup_day", day)
            for old in sorted(target.glob("router-bot-????-??-??.sqlite"), reverse=True)[
                c.backup_keep :
            ]:
                old.unlink()
        except Exception:
            log.warning("Резервная копия не создана. Проверьте место и права каталога.")
    waiting = store.get(
        "SELECT count(*) AS n FROM outbox WHERE first_at<?",
        stamp(now - timedelta(hours=c.alert_hours)),
    )["n"]
    if waiting and store.setting("sync_alert_day") != day:
        delivered = False
        for actor in admins:
            try:
                inventory.authorize(actor, "admin")
                telegram.send(
                    actor,
                    f"Обновление таблицы ожидает {waiting} записей более {c.alert_hours} ч. Проверьте отчёты «Ожидают таблицу» и «Конфликты».",
                )
                delivered = True
            except Exception:
                pass
        if delivered:
            store.set("sync_alert_day", day)


def run():
    c = config()
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    with process_lock(c.db):
        store = Store(c.db)
        try:
            migrate(store, c.backup_dir)
            inventory = Inventory(store, bootstrap_admin_id=c.admin)
            telegram, adapter = Telegram(c.token), adapter_for(c)
            sync = Synchronizer(inventory, adapter) if adapter else None
            colors = ColorRepair(inventory, adapter) if adapter else None
            campaigns = Campaigns(inventory, time=c.reminder_time, offsets=c.reminder_offsets)
            bot = Bot(
                inventory, telegram, sync=sync, colors=colors, campaigns=campaigns, mode=c.mode
            )
            if telegram.call("getWebhookInfo").get("url"):
                raise DomainError(
                    "У бота настроен webhook. Отключите его по инструкции без удаления ожидающих обновлений."
                )
            telegram.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "menu", "description": "Главное меню"},
                        {"command": "find", "description": "Поиск роутера"},
                        {"command": "reports", "description": "Отчёты"},
                        {"command": "collection", "description": "Сбор"},
                        {"command": "help", "description": "Помощь"},
                    ]
                },
            )
            log.info("Бот запущен. Режим таблицы: %s", c.mode)
            last_maintenance = 0
            while not stop.is_set():
                if time.monotonic() - last_maintenance >= 30:
                    last_maintenance = time.monotonic()
                    maintain(store, inventory, telegram, campaigns, c, sync, adapter)
                try:
                    updates = telegram.call(
                        "getUpdates",
                        {
                            "offset": store.setting("telegram_offset", 0),
                            "timeout": 25,
                            "allowed_updates": ["message", "callback_query"],
                        },
                        timeout=35,
                    )
                    for update in updates:
                        if stop.is_set():
                            break
                        bot.handle(update)
                        store.set("telegram_offset", update["update_id"] + 1)
                        store.run(
                            "DELETE FROM processed_updates WHERE id<?", update["update_id"] - 10000
                        )
                except Exception:
                    log.warning("Telegram временно недоступен. Повтор через 5 секунд.")
                    stop.wait(5)
        finally:
            store.close()
