import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .util import DomainError, require, valid_id


@dataclass
class Config:
    token: str = ""
    admin: int = 0
    db: str = "./data/router-bot.sqlite"
    mode: str = "disabled"
    spreadsheet_id: str = "1N6lyJS7-PZ_hunmjnY06-0tnEEeOaP0Ypn-yEVfooK4"
    sheet_id: int = 966826055
    title: str = "7ка"
    credentials: str = "./secrets/service-account.json"
    reminder_time: str = "10:00"
    reminder_offsets: tuple = (14, 7, 1, 0)
    retention_days: int = 365
    backup_dir: str = "./backups"
    backup_keep: int = 14
    alert_hours: int = 6


def config(env=None, *, require_bot=True):
    if env is None:
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        env = os.environ
    try:
        c = Config(
            token=env.get("BOT_TOKEN", ""),
            admin=int(env.get("BOOTSTRAP_ADMIN_ID") or 0),
            db=env.get("DB_PATH", "./data/router-bot.sqlite"),
            mode=env.get("SHEETS_MODE", "disabled"),
            spreadsheet_id=env.get("SPREADSHEET_ID", Config.spreadsheet_id),
            sheet_id=int(env.get("SHEET_ID", 966826055)),
            title=env.get("SHEET_TITLE", "7ка"),
            credentials=env.get("GOOGLE_APPLICATION_CREDENTIALS", "./secrets/service-account.json"),
            reminder_time=env.get("REMINDER_TIME", "10:00"),
            reminder_offsets=tuple(
                int(x) for x in env.get("REMINDER_OFFSETS", "14,7,1,0").split(",")
            ),
            retention_days=int(env.get("CONTACT_RETENTION_DAYS", 365)),
            backup_dir=env.get("BACKUP_DIR", "./backups"),
            backup_keep=int(env.get("BACKUP_KEEP", 14)),
            alert_hours=int(env.get("SYNC_ALERT_HOURS", 6)),
        )
    except (ValueError, TypeError):
        raise DomainError("Числовые параметры конфигурации имеют неверный формат") from None
    if require_bot:
        require(re.fullmatch(r"\d+:[\w-]+", c.token), "Заполните BOT_TOKEN в .env")
        require(valid_id(c.admin), "Заполните BOOTSTRAP_ADMIN_ID")
    require(c.mode in {"disabled", "memory", "google"}, "SHEETS_MODE: disabled, memory или google")
    require(
        env.get("TIMEZONE", "Europe/Moscow") == "Europe/Moscow",
        "Поддерживается TIMEZONE=Europe/Moscow",
    )
    require(
        re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", c.reminder_time), "Неверное время напоминаний"
    )
    require(
        c.reminder_offsets and all(0 <= n <= 365 for n in c.reminder_offsets),
        "Неверные интервалы напоминаний",
    )
    require(
        c.retention_days >= 0 and c.backup_keep > 0 and c.alert_hours > 0,
        "Неверный срок хранения / оповещения",
    )
    return c
