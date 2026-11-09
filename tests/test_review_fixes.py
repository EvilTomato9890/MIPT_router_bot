from datetime import UTC, datetime, timedelta

import pytest
from conftest import add, issue, key, person
from test_bot import FakeTelegram, callback, message
from test_sync import legacy

from router_bot.bot import Bot
from router_bot.config import Config
from router_bot.db import Store
from router_bot.dialogs import fields
from router_bot.main import sheets_worker
from router_bot.service import Inventory, place
from router_bot.sheets.memory import MemorySheets
from router_bot.util import DomainError, overdue, stable


def ui(ctx):
    telegram = FakeTelegram()
    return Bot(ctx.i, telegram, sync=ctx.sync, colors=ctx.colors, campaigns=ctx.c), telegram


def button_value(telegram, label):
    return next(
        b["callback_data"]
        for row in telegram.messages[-1]["markup"]["inline_keyboard"]
        for b in row
        if b["text"] == label
    )


@pytest.mark.parametrize("legacy_stale_found", [False, True])
def test_repeated_loss_requires_a_new_discovery(ctx, legacy_stale_found):
    rid = add(ctx)
    issue(ctx, rid)
    ctx.i.execute(2, key(), {"kind": "lost", "routerId": rid, "reason": "Первая утрата"})
    ctx.i.execute(2, key(), {"kind": "found", "routerId": rid, "reason": "Первое обнаружение"})
    found = ctx.i.get(1, rid)["found"]
    resolve = {
        "kind": "resolve_found",
        "routerId": rid,
        "resolution": "holder",
        "reason": "Найден у получателя",
    }
    ctx.i.execute(1, key(), resolve)
    ctx.clock.at += timedelta(days=2)
    ctx.i.execute(2, key(), {"kind": "lost", "routerId": rid, "reason": "Повторная утрата"})
    assert "found" not in ctx.i.get(1, rid)
    if legacy_stale_found:
        router = ctx.i._router(rid)
        router["found"] = found
        with ctx.s.transaction():
            ctx.i._save(router, key())
    with pytest.raises(DomainError, match="обнаружение"):
        ctx.i.execute(1, key(), resolve)
    assert ctx.i.get(1, rid)["status"] == "lost"
    ctx.i.execute(2, key(), {"kind": "found", "routerId": rid, "reason": "Новое обнаружение"})
    ctx.i.execute(1, key(), resolve)
    assert ctx.i.get(1, rid)["status"] == "issued"


def test_overdue_replacement_keeps_deadline_and_can_be_undone_atomically(ctx):
    old, new = add(ctx, "OLD"), add(ctx, "NEW")
    issue(ctx, old)
    ctx.clock.at = datetime(2027, 7, 2, 10, tzinfo=UTC)
    operation = ctx.i.execute(
        2,
        key(),
        {
            "kind": "replace",
            "routerId": old,
            "newRouterId": new,
            "inventory": "OLD",
            "physical": True,
            "condition": "broken",
            "storage": place("Склад"),
            "dueAt": "2027-08-01",
            "reason": "Замена",
        },
    )
    loan = ctx.i.get(1, new)["loan"]
    assert loan["issuedAt"] == "2027-07-02" and loan["dueAt"] == "2027-07-01"
    assert overdue(loan, ctx.clock.at)
    assert ctx.sync.flush()["sent"] == 2
    assert (
        next(r for r in ctx.a.sheet["rows"] if r["values"]["inventory"] == "NEW")["values"][
            "status"
        ]
        == "Просрочен"
    )
    ctx.i.execute(
        1,
        key(),
        {"kind": "correct", "routerId": old, "eventId": operation["eventId"], "reason": "Ошибка"},
    )
    assert ctx.i.get(1, old)["status"] == "issued" and ctx.i.get(1, new)["status"] == "stored"
    assert ctx.i.get(1, old)["loan"]["dueAt"] == "2027-07-01"


@pytest.mark.parametrize("kind", ["issue", "transfer"])
def test_past_deadline_is_not_accepted_for_a_new_or_transferred_loan(ctx, kind):
    rid = add(ctx)
    if kind == "transfer":
        issue(ctx, rid)
    before = ctx.i.get(1, rid)
    with pytest.raises(DomainError, match="Срок раньше"):
        ctx.i.execute(
            2,
            key(),
            {
                "kind": kind,
                "routerId": rid,
                "recipient": person(),
                "place": place("101"),
                "dueAt": "2027-05-31",
                "reason": "Проверка",
            },
        )
    assert ctx.i.get(1, rid) == before


def test_replacement_dialog_does_not_ask_for_an_ignored_deadline(ctx):
    old = add(ctx, "OLD")
    add(ctx, "NEW")
    issue(ctx, old)
    bot, telegram = ui(ctx)
    bot.handle(callback(2, "act:replace:" + old))
    answers = dict(
        newInventory="NEW",
        inventory="OLD",
        physical="yes",
        storageName="Склад",
        condition="broken",
        reason="Замена",
    )
    while bot._session(2)["mode"] == "wizard":
        state = bot._session(2)
        current = fields("replace", state["data"])[state["step"]].key
        assert current != "dueAt"
        bot.handle(message(2, answers[current]))
    assert bot._session(2)["data"]["dueAt"] == "2027-07-01"
    assert "2027-07-01" in telegram.messages[-2]["text"]


def preview(ctx, bot, telegram, kind):
    if kind == "sync":
        if not ctx.a.sheet["rows"]:
            ctx.a.sheet["rows"].append({"row": 2, "values": legacy("NEW"), "formulas": []})
        bot.sync_preview(1)
        apply = button_value(telegram, "Применить проверенные изменения")
    elif kind == "colors":
        bot.color_preview(1)
        apply = button_value(telegram, "Применить")
    else:
        campaign = ctx.c.save(
            1,
            key(),
            {
                "name": "Сбор",
                "admissionYear": 2026,
                "startsAt": "2027-06-01",
                "deadline": "2027-07-01",
                "responsibleIds": [2],
                "location": "Склад",
                "hours": "10–18",
                "instruction": "Сдать роутер",
            },
        )
        bot.campaign_preview(1, campaign["id"])
        apply = button_value(telegram, "Подтвердить запуск")
    return apply, button_value(telegram, "Отмена")


@pytest.mark.parametrize("kind", ["sync", "colors", "launch"])
@pytest.mark.parametrize("cancel", ["button", "command"])
def test_cancelled_admin_preview_cannot_be_applied_by_an_old_button(ctx, kind, cancel):
    bot, telegram = ui(ctx)
    apply, discard = preview(ctx, bot, telegram, kind)
    bot.handle(callback(1, discard) if cancel == "button" else message(1, "/cancel"))
    count = ctx.s.get("SELECT count(*) AS n FROM events")["n"]
    bot.handle(callback(1, apply))
    assert ctx.i.list(1) == [] and ctx.a.sheet["rules"] == []
    assert not any(c["active"] for c in ctx.c.list(1))
    assert ctx.s.get("SELECT count(*) AS n FROM events")["n"] == count
    assert "устарело" in telegram.messages[-1]["text"]
    fresh, _ = preview(ctx, bot, telegram, kind)
    bot.handle(callback(1, fresh))
    if kind == "sync":
        assert len(ctx.i.list(1)) == 1
    elif kind == "colors":
        assert len(ctx.a.sheet["rules"]) == 6
    else:
        assert sum(c["active"] for c in ctx.c.list(1)) == 1


def test_scoped_preview_cancel_preserves_an_unrelated_unfinished_command(ctx):
    bot, telegram = ui(ctx)
    bot.start(1, "add")
    bot.handle(message(1, "SAVED-INPUT"))
    _, discard = preview(ctx, bot, telegram, "sync")
    identifier = discard.split(":")[1]
    bot.handle(callback(2, discard))
    assert ctx.s.get("SELECT 1 FROM drafts WHERE id=?", identifier)
    bot.handle(callback(1, discard))
    assert not ctx.s.get("SELECT 1 FROM drafts WHERE id=?", identifier)
    assert bot._session(1)["data"]["inventory"] == "SAVED-INPUT"
    bot.handle(message(1, "/resume"))
    assert "Модель" in telegram.messages[-1]["text"]


def test_cancel_sync_also_discards_its_pending_conflict_confirmation(ctx):
    target = ctx.s.draft(1, "sync", {}, ctx.clock.at)
    pending = ctx.s.draft(
        1, "confirm", {"kind": "resolve", "data": {"syncDraft": target}}, ctx.clock.at
    )
    ctx.s.run(
        "INSERT INTO sessions VALUES(?,?)",
        1,
        stable(
            {
                "mode": "confirm",
                "kind": "resolve",
                "data": {"syncDraft": target},
                "draftId": pending,
                "dialogVersion": 3,
            }
        ),
    )
    ctx.s.cancel_preview(1, target)
    assert not ctx.s.get("SELECT 1 FROM drafts WHERE id IN (?,?)", target, pending)
    assert not ctx.s.get("SELECT 1 FROM sessions WHERE user_id=1")


def test_switching_to_conflict_resolution_keeps_its_parent_preview(ctx):
    rid = add(ctx)
    bot, telegram = ui(ctx)
    bot.start(1, "add")
    target = ctx.s.draft(1, "sync", {"entries": [{"routerId": rid}]}, ctx.clock.at)
    bot.start(1, "resolve", {"syncDraft": target, "entryIndex": 0, "source": "bot"})
    bot.handle(callback(1, button_value(telegram, "Отменить и начать новую")))
    assert ctx.s.get_draft(1, target, ctx.clock.at)
    assert bot._session(1)["kind"] == "resolve"
    bot.handle(message(1, "Оставить значение бота"))
    bot.handle(callback(1, button_value(telegram, "Подтвердить")))
    assert (
        ctx.s.get_draft(1, target, ctx.clock.at)["data"]["resolutions"][rid]["reason"]
        == "Оставить значение бота"
    )


def test_reports_are_combined_and_retired_period_buttons_remain_safe(ctx):
    rid = add(ctx)
    issued = issue(ctx, rid)
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "correct",
            "routerId": rid,
            "eventId": issued["eventId"],
            "reason": "Не выдавался",
        },
    )
    bot, telegram = ui(ctx)
    bot.handle(callback(3, "start:loan_report"))
    labels = [b["text"] for row in telegram.messages[-1]["markup"]["inline_keyboard"] for b in row]
    assert labels.count("Нужно уточнить") == 1
    assert not set(labels) & {
        "Выдачи за период",
        "Срок не определён",
        "Получатель не определён",
        "Набор не подтверждён",
    }
    assert ctx.i.stats(1)["statuses"]["stored"] == 1 and ctx.i.stats(1)["statuses"]["issued"] == 0
    assert ctx.i.loans(1, rid)[0]["closedKind"] == "correction"


def test_nonparticipating_loan_is_not_flagged_for_a_missing_cohort(ctx):
    rid = add(ctx)
    issue(ctx, rid, recipient={**person(), "admissionYear": None}, collectionEligible=False)
    assert ctx.i.list(1, {"report": "incomplete"}) == []


def test_schema_upgrade_retires_cache_without_losing_explicit_ids_or_dialogs(ctx, tmp_path):
    rid = add(ctx)
    issue(ctx, rid, recipient={**person(), "contact": {"raw": "tg://user?id=77", "userId": 77}})
    bot, _ = ui(ctx)
    bot.start(2, "add")
    bot.handle(message(2, "PENDING"))
    path = tmp_path / "upgrade.sqlite"
    ctx.s.backup(path)
    old = Store(path)
    old.run("DELETE FROM schema_migrations WHERE version=3")
    old.run(
        "CREATE TABLE telegram_users(user_id INTEGER PRIMARY KEY,username TEXT,observed_at TEXT)"
    )
    old.run("INSERT INTO telegram_users VALUES(77,'old_user','2027-06-01')")
    old.set("colors_managed", True)
    preview = old.draft(1, "colors", {}, ctx.clock.at)
    events = old.get("SELECT count(*) AS n FROM events")["n"]
    old.close()
    upgraded = Store(path)
    try:
        assert not upgraded.get("SELECT 1 FROM sqlite_master WHERE name='telegram_users'")
        assert not upgraded.setting("colors_managed")
        assert not upgraded.get("SELECT 1 FROM drafts WHERE id=?", preview)
        inventory = Inventory(upgraded, now=ctx.i.now)
        assert inventory.get(1, rid)["loan"]["recipient"]["contact"]["userId"] == 77
        assert upgraded.get("SELECT count(*) AS n FROM events")["n"] == events
        assert "PENDING" in upgraded.get("SELECT data FROM sessions WHERE user_id=2")["data"]
        fresh = upgraded.draft(1, "colors", {}, ctx.clock.at)
    finally:
        upgraded.close()
    again = Store(path)
    try:
        assert again.get_draft(1, fresh, ctx.clock.at)
        assert not again.get("SELECT 1 FROM sqlite_master WHERE name='telegram_users'")
    finally:
        again.close()


def test_idle_worker_does_not_poll_or_repair_formatting(ctx, tmp_path):
    path = tmp_path / "idle.sqlite"
    ctx.s.set("colors_managed", True)
    ctx.s.backup(path)

    class IdleSheets(MemorySheets):
        def read(self):
            pytest.fail("При пустой очереди фоновое чтение не требуется")

    class OnePass:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _):
            self.stopped = True

    sheets_worker(Config(db=str(path), mode="memory"), OnePass(), IdleSheets(), now=ctx.i.now)
