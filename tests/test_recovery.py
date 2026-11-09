import threading
from datetime import timedelta

import pytest
from conftest import add, campaign, issue, key, person, receive
from test_bot import FakeTelegram, callback, confirm_button, message
from test_sync import legacy

from router_bot.bot import Bot
from router_bot.campaigns import Campaigns
from router_bot.config import Config
from router_bot.contacts import observe, resolve
from router_bot.db import Store
from router_bot.dialogs import fields
from router_bot.main import maintain, sheets_worker
from router_bot.service import Inventory, place
from router_bot.sheets.memory import MemorySheets
from router_bot.sheets.schema import projection
from router_bot.sheets.sync import Synchronizer
from router_bot.util import DomainError, contact, stable


def ui(ctx, telegram=None):
    telegram = telegram or FakeTelegram()
    return Bot(ctx.i, telegram, sync=ctx.sync, colors=ctx.colors, campaigns=ctx.c), telegram


def finish_note(ctx, actor=2):
    identifier = add(ctx)
    bot, telegram = ui(ctx)
    bot.start(actor, "note", {"routerId": identifier, "expected": {identifier: 1}})
    bot.handle(message(actor, "Сохранённое примечание"))
    return identifier, bot, telegram, confirm_button(telegram)


@pytest.mark.parametrize("pre_upgrade", [False, True])
def test_universal_undo_preserves_imported_contact_and_sheet(ctx, pre_upgrade):
    ctx.a.sheet["rows"].append(
        {
            "row": 2,
            "values": legacy("OLD", rawContact="@known_user +70000000000"),
            "formulas": [],
        }
    )
    ctx.sync.apply(1, ctx.sync.preview(1)["id"])
    ctx.sync.flush()
    identifier = ctx.i.get(1, "OLD")["id"]
    before = ctx.i.get(1, identifier)["legacy"]["contact"]
    edited = ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Ошибка"})
    ctx.sync.flush()
    if pre_upgrade:
        ctx.s.run("DELETE FROM event_states WHERE event_id=?", edited["eventId"])
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "correct",
            "routerId": identifier,
            "eventId": edited["eventId"],
            "reason": "Откат",
        },
    )
    assert ctx.i.get(1, identifier)["legacy"]["contact"] == before
    assert ctx.sync.flush()["sent"] == 1
    assert ctx.a.sheet["rows"][0]["values"]["rawContact"] == before["raw"]


def test_undo_contact_edit_restores_original_private_state(ctx):
    identifier = add(ctx)
    original = {**person(), "contact": contact("@original_user +70000000000")}
    issue(ctx, identifier, recipient=original)
    changed = ctx.i.execute(
        1,
        key(),
        {
            "kind": "loan_details",
            "routerId": identifier,
            "recipientKind": "person",
            "contact": contact("@different_user"),
            "admissionYear": 2026,
            "issuedAt": "2027-06-01",
            "collectionEligible": True,
            "reason": "Уточнение",
        },
    )
    assert "@original_user" not in ctx.i.history(1, identifier)[-1]["before_json"]
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "correct",
            "routerId": identifier,
            "eventId": changed["eventId"],
            "reason": "Ошибка",
        },
    )
    assert ctx.i.get(1, identifier)["loan"]["recipient"]["contact"] == original["contact"]


def test_undo_never_resurrects_purged_contacts(ctx):
    identifier = add(ctx)
    issue(ctx, identifier, recipient={**person(), "contact": contact("@private_user")})
    returned = receive(ctx, identifier)
    ctx.clock.at += timedelta(days=400)
    assert ctx.i.purge_contacts(1, 365, key())["count"] == 1
    assert "@private_user" not in stable(ctx.s.all("SELECT * FROM event_states"))
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "correct",
            "routerId": identifier,
            "eventId": returned["eventId"],
            "reason": "Уточнение",
        },
    )
    assert ctx.i.get(1, identifier)["loan"]["recipient"]["contact"] is None


@pytest.mark.parametrize("kind", ["transfer", "write_off", "correct"])
def test_collection_closure_is_not_physical_return(ctx, kind):
    identifier = add(ctx)
    issuance = issue(ctx, identifier)
    collection = campaign(ctx)
    command = {"kind": kind, "routerId": identifier, "reason": "Изменение"}
    if kind == "transfer":
        command.update(
            recipient={"kind": "organization", "name": "Клуб"},
            place=place("Клуб"),
            dueAt="2027-07-01",
        )
    elif kind == "write_off":
        command["resolveOutstanding"] = True
    else:
        command["eventId"] = issuance["eventId"]
    ctx.i.execute(1, key(), command)
    report = ctx.c.report(1, collection)
    assert report["returned"] == 0 and report["remaining"] == 0
    assert report["closedWithoutReturn"] == 1


def test_only_one_collection_active_and_previous_history_retained(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    first = campaign(ctx)
    second = campaign(ctx, name="Новый сбор")
    assert not ctx.c._get(first)["active"] and ctx.c._get(second)["active"]
    assert len(ctx.c.list(1)) == 2
    assert ctx.c.report(1, first)["plan"] == 1


def test_daily_digest_starts_immediately_and_does_not_repeat(ctx):
    issue(ctx, add(ctx))
    campaign(ctx)
    delivered = []
    assert ctx.c.tick(lambda *args: delivered.append(args))["sent"] == 1
    assert ctx.c.tick(lambda *args: delivered.append(args))["sent"] == 0
    ctx.clock.at += timedelta(days=1)
    assert ctx.c.tick(lambda *args: delivered.append(args))["sent"] == 1
    assert len(delivered) == 2


def test_menu_interruption_and_new_command_preserve_answers(ctx):
    bot, telegram = ui(ctx)
    bot.start(2, "add")
    bot.handle(message(2, "UNFINISHED"))
    bot.handle(message(2, "/menu"))
    assert bot._session(2)["data"]["inventory"] == "UNFINISHED"
    bot.handle(callback(2, "select:issue"))
    assert "незавершённая" in telegram.messages[-1]["text"]
    assert bot._session(2)["kind"] == "add"
    bot.handle(callback(2, "resume"))
    assert "Модель" in telegram.messages[-1]["text"]


def test_explicit_switch_discards_old_confirmation(ctx):
    identifier, bot, telegram, confirm = finish_note(ctx)
    bot.handle(callback(2, "start:add"))
    switch = telegram.messages[-1]["markup"]["inline_keyboard"][1][0]["callback_data"]
    bot.handle(callback(2, switch))
    bot.handle(callback(2, confirm))
    assert ctx.i.get(1, identifier)["note"] == ""
    assert bot._session(2)["kind"] == "add"


def test_confirmation_survives_restart_expiry_and_maintenance(ctx, tmp_path):
    identifier, bot, telegram, _ = finish_note(ctx)
    ctx.clock.at += timedelta(days=3)
    c = Config(backup_dir=str(tmp_path / "backups"), retention_days=0)
    maintain(ctx.s, ctx.i, telegram, ctx.c, c)
    assert bot._session(2)["mode"] == "confirm"
    bot, telegram = ui(ctx)
    bot.handle(message(2, "/resume"))
    confirmation = confirm_button(telegram)
    bot.handle(callback(2, confirmation))
    bot.handle(callback(2, confirmation))
    assert ctx.i.get(1, identifier)["note"] == "Сохранённое примечание"
    assert len([e for e in ctx.i.history(1, identifier) if e["action"] == "note"]) == 1
    assert bot._session(2) is None


def test_cancel_invalidates_confirmation(ctx):
    identifier, bot, _, confirmation = finish_note(ctx)
    bot.handle(message(2, "/cancel"))
    bot.handle(callback(2, confirmation))
    assert ctx.i.get(1, identifier)["note"] == ""


def test_crash_during_wizard_rolls_back_step_and_processed_marker(ctx, monkeypatch):
    bot, _ = ui(ctx)
    bot.start(2, "add")
    update = message(2, "RECOVERED")
    original = bot.ask

    def crash(actor):
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, "ask", crash)
    with pytest.raises(KeyboardInterrupt):
        bot.handle(update)
    assert bot._session(2)["step"] == 0
    assert not ctx.s.get("SELECT 1 FROM processed_updates WHERE id=?", update["update_id"])
    monkeypatch.setattr(bot, "ask", original)
    bot.handle(update)
    bot.handle(update)
    assert bot._session(2)["step"] == 1


def test_crash_after_domain_operation_retries_without_duplicates(ctx, monkeypatch):
    identifier, bot, _, confirmation = finish_note(ctx)
    update = callback(2, confirmation)
    execute = ctx.i.execute

    def crash(*args):
        execute(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(ctx.i, "execute", crash)
    with pytest.raises(KeyboardInterrupt):
        bot.handle(update)
    assert ctx.i.get(1, identifier)["note"] == ""
    monkeypatch.setattr(ctx.i, "execute", execute)
    bot.handle(update)
    bot.handle(update)
    assert len([e for e in ctx.i.history(1, identifier) if e["action"] == "note"]) == 1


def test_crash_after_commit_keeps_reply_for_restart(ctx):
    identifier, bot, _, confirmation = finish_note(ctx)

    class InterruptedTelegram(FakeTelegram):
        def send(self, *args):
            raise KeyboardInterrupt

    interrupted, _ = ui(ctx, InterruptedTelegram())
    update = callback(2, confirmation)
    with pytest.raises(KeyboardInterrupt):
        interrupted.handle(update)
    assert ctx.i.get(1, identifier)["note"] == "Сохранённое примечание"
    assert ctx.s.get("SELECT count(*) AS n FROM reply_outbox")["n"] > 0
    restarted, telegram = ui(ctx)
    restarted.handle(update)
    restarted.t.flush()
    assert any("Операция сохранена" in m["text"] for m in telegram.messages)
    assert len([e for e in ctx.i.history(1, identifier) if e["action"] == "note"]) == 1


def test_pending_replies_recheck_revoked_permissions(ctx):
    identifier, _, _, confirmation = finish_note(ctx)

    class OfflineTelegram(FakeTelegram):
        def send(self, *args):
            raise TimeoutError

    bot, _ = ui(ctx, OfflineTelegram())
    bot.handle(callback(2, confirmation))
    ctx.i.set_role(1, 2, None, key())
    restarted, telegram = ui(ctx)
    restarted.t.flush()
    assert not telegram.messages
    assert ctx.s.get("SELECT count(*) AS n FROM reply_outbox")["n"] == 0


def test_confirmation_does_not_wait_for_sheets(ctx, monkeypatch):
    identifier, bot, telegram, confirmation = finish_note(ctx)
    monkeypatch.setattr(ctx.sync, "flush", lambda: pytest.fail("Синхронный запрос к таблице"))
    bot.handle(callback(2, confirmation))
    assert ctx.i.get(1, identifier)["note"]
    assert any("Операция сохранена" in m["text"] for m in telegram.messages)


def test_worker_blocked_on_sheet_does_not_block_accounting(ctx, tmp_path):
    path = tmp_path / "worker.sqlite"
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()

    class SlowSheets(MemorySheets):
        def read(self):
            entered.set()
            assert release.wait(10)
            return super().read()

    identifier = add(ctx)
    ctx.s.backup(path)
    config = Config(db=str(path), mode="memory")
    adapter = SlowSheets()
    worker = threading.Thread(
        target=sheets_worker, args=(config, stop, adapter), kwargs={"now": ctx.i.now}
    )
    worker.start()
    try:
        assert entered.wait(5)
        with_store = Store(path)
        try:
            inventory = Inventory(with_store, now=ctx.i.now)
            telegram = FakeTelegram()
            bot = Bot(
                inventory,
                telegram,
                sync=Synchronizer(inventory, adapter),
                campaigns=Campaigns(inventory),
            )
            bot.start(2, "note", {"routerId": identifier})
            bot.handle(message(2, "Во время недоступности таблицы"))
            bot.handle(callback(2, confirm_button(telegram)))
            assert inventory.get(1, identifier)["note"] == "Во время недоступности таблицы"
        finally:
            with_store.close()
    finally:
        stop.set()
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def test_observed_username_resolves_id_without_granting_access(ctx):
    bot, _ = ui(ctx)
    update = message(99, "/start")
    update["message"]["from"]["username"] = "known_user"
    bot.handle(update)
    assert resolve(ctx.s, contact("@KNOWN_USER"))["userId"] == 99
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.i.authorize(99)
    identifier = add(ctx)
    issue(ctx, identifier, recipient={**person(), "contact": contact("@known_user")})
    exported = projection(ctx.i.get(1, identifier), ctx.i.get(1, identifier)["loan"], ctx.clock.at)
    assert "@known_user" in exported["rawContact"] and "tg://user?id=99" in exported["rawContact"]
    assert contact(exported["rawContact"])["userId"] == 99
    assert contact("tg://user?id=1234567890")["phone"] is None


def test_unknown_username_never_fabricates_id_and_confirmed_pair_is_reused(ctx):
    assert resolve(ctx.s, contact("@unknown_user"))["userId"] is None
    first, second = add(ctx, "FIRST"), add(ctx, "SECOND")
    issue(ctx, first, recipient={**person(), "contact": {**contact("@manual_user"), "userId": 200}})
    issue(ctx, second, recipient={**person(), "contact": contact("@manual_user")})
    assert ctx.i.get(1, second)["loan"]["recipient"]["contact"]["userId"] == 200
    observe(ctx.s, {"id": 200, "username": "renamed_user"}, ctx.clock.at)
    assert resolve(ctx.s, contact("@manual_user"))["userId"] is None
    observe(ctx.s, {"id": 300, "username": "renamed_user"}, ctx.clock.at)
    assert resolve(ctx.s, contact("@renamed_user"))["userId"] == 300


def test_short_issue_dialog_autofills_id_and_shared_contact(ctx):
    identifier = add(ctx)
    observe(ctx.s, {"id": 100, "username": "known_user"}, ctx.clock.at)
    bot, telegram = ui(ctx)
    bot.start(2, "issue", {"routerId": identifier})
    values = dict(
        recipientKind="person",
        recipientName="Тест",
        admissionYear="2026",
        contactInput="@known_user",
        placeName="101",
        dueAt="2027-07-01",
    )
    answered = []
    while bot._session(2)["mode"] == "wizard":
        state = bot._session(2)
        f = fields(state["kind"], state["data"])[state["step"]]
        answered.append(f.key)
        bot.handle(message(2, values[f.key]))
    assert len(answered) == 6 and "userId" not in answered
    bot.handle(callback(2, confirm_button(telegram)))
    assert ctx.i.get(1, identifier)["loan"]["recipient"]["contact"]["userId"] == 100
    second = add(ctx, "SHARED")
    bot.start(2, "issue", {"routerId": second})
    for value in ("person", "Имя", "-"):
        bot.handle(message(2, value))
    update = message(2, "")
    update["message"]["contact"] = {"user_id": 500, "phone_number": "+70000000000"}
    bot.handle(update)
    assert bot._session(2)["data"]["userId"] == 500
    assert fields("issue", bot._session(2)["data"])[bot._session(2)["step"]].key == "placeName"


@pytest.mark.parametrize("other_changed", [False, True])
def test_universal_undo_replacement_is_atomic_for_both_devices(ctx, other_changed):
    old, new = add(ctx, "OLD"), add(ctx, "NEW")
    issue(ctx, old, recipient={**person(), "contact": contact("@original_user")})
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
            "reason": "Замена",
        },
    )
    correction = {
        "kind": "correct",
        "routerId": old,
        "eventId": operation["eventId"],
        "reason": "Ошибочная замена",
    }
    if other_changed:
        ctx.i.execute(2, key(), {"kind": "note", "routerId": new, "note": "Зависимое событие"})
        with pytest.raises(DomainError, match="зависимые"):
            ctx.i.execute(1, key(), correction)
        assert ctx.i.get(1, old)["status"] == "stored" and ctx.i.get(1, new)["status"] == "issued"
        return
    undone = ctx.i.execute(1, key(), correction)
    assert ctx.i.get(1, old)["loan"]["recipient"]["contact"]["telegram"] == "original_user"
    assert ctx.i.get(1, old)["status"] == "issued" and ctx.i.get(1, new)["status"] == "stored"
    jobs = ctx.s.all("SELECT group_id FROM outbox")
    assert len(jobs) == 2 and len({j["group_id"] for j in jobs}) == 1
    ctx.i.execute(1, key(), {**correction, "eventId": undone["eventId"]})
    assert ctx.i.get(1, old)["status"] == "stored" and ctx.i.get(1, new)["status"] == "issued"


def test_work_in_progress_reopens_from_real_sqlite_file(ctx, tmp_path):
    bot, _ = ui(ctx)
    bot.start(2, "add")
    bot.handle(message(2, "PERSISTENT"))
    path = tmp_path / "reopened.sqlite"
    ctx.s.backup(path)
    reopened = Store(path)
    try:
        inventory = Inventory(reopened, now=ctx.i.now)
        telegram = FakeTelegram()
        restarted = Bot(inventory, telegram, campaigns=Campaigns(inventory))
        restarted.handle(message(2, "/resume"))
        assert restarted._session(2)["data"]["inventory"] == "PERSISTENT"
        assert "Модель" in telegram.messages[-1]["text"]
    finally:
        reopened.close()


def test_old_python_wizard_answers_are_migrated_without_reset(ctx):
    identifier = add(ctx)
    data = {
        "routerId": identifier,
        "recipientKind": "person",
        "recipientName": "Имя",
        "group": "ТЕСТ",
        "admissionYear": 2026,
        "academicYear": "2026/2027",
        "collectionEligible": True,
        "phone": "+70000000000",
        "telegram": "old_user",
    }
    ctx.s.run(
        "INSERT INTO sessions VALUES(?,?)",
        2,
        stable(
            {
                "mode": "wizard",
                "kind": "issue",
                "data": data,
                "step": 9,
                "visited": [0, 1, 3, 4, 5, 6, 7, 8],
                "nonce": "oldnonce",
                "updatedAt": "2020-01-01T00:00:00.000Z",
            }
        ),
    )
    bot, telegram = ui(ctx)
    bot.handle(message(2, "/resume"))
    session = bot._session(2)
    assert session["data"]["recipientName"] == "Имя"
    assert "@old_user" in session["data"]["contactInput"]
    assert fields("issue", session["data"])[session["step"]].key == "userId"
    assert "ID" in telegram.messages[-1]["text"]


def test_stale_switch_button_never_discards_a_later_command(ctx):
    bot, telegram = ui(ctx)
    bot.start(2, "add")
    bot.handle(callback(2, "select:issue"))
    switch = telegram.messages[-1]["markup"]["inline_keyboard"][1][0]["callback_data"]
    bot.handle(message(2, "/cancel"))
    bot.start(2, "add")
    bot.handle(message(2, "NEW-COMMAND"))
    bot.handle(callback(2, switch))
    assert bot._session(2)["data"]["inventory"] == "NEW-COMMAND"
    assert "устарела" in telegram.messages[-1]["text"]


def test_pending_offline_recipient_does_not_block_other_staff(ctx):
    bot, telegram = ui(ctx)
    for _ in range(25):
        ctx.s.run(
            "INSERT INTO reply_outbox(actor,minimum_role,method,data,next_at) VALUES(?,?,?,?,?)",
            2,
            "operator",
            "send",
            stable([2, "Отложено"]),
            "2099-01-01T00:00:00.000Z",
        )
    bot.handle(message(1, "/menu"))
    assert any(m["id"] == 1 for m in telegram.messages)


@pytest.mark.parametrize(
    "changed_contact,expected_id", [("@other_user", None), ("", None), ("@next_user", 333)]
)
def test_sheet_contact_edit_does_not_keep_previous_identity(ctx, changed_contact, expected_id):
    identifier = add(ctx)
    issue(
        ctx,
        identifier,
        recipient={**person(), "contact": {**contact("@first_user"), "userId": 222}},
    )
    observe(ctx.s, {"id": 333, "username": "next_user"}, ctx.clock.at)
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"]["rawContact"] = changed_contact
    preview = ctx.sync.preview(1)
    ctx.sync.apply(1, preview["id"])
    saved = ctx.i.get(1, identifier)["loan"]["recipient"]["contact"]
    assert saved["userId"] == expected_id


def test_upgrade_archives_older_active_collections_once(ctx, monkeypatch):
    identifiers = iter(
        ["ffffffff-ffff-4fff-afff-ffffffffffff", "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"]
    )
    monkeypatch.setattr("router_bot.campaigns.uuid", lambda: next(identifiers))
    first = campaign(ctx)
    second = campaign(ctx, name="Последний сбор")
    previous = ctx.c._get(first)
    previous["active"] = True
    ctx.c._save(previous)
    result = ctx.c.simplify(1)
    assert result["active"] == second and not ctx.c._get(first)["active"]
    count = ctx.s.get("SELECT count(*) AS n FROM events")["n"]
    ctx.c.simplify(1)
    assert ctx.s.get("SELECT count(*) AS n FROM events")["n"] == count


def test_undo_old_event_does_not_restore_removed_equipment_or_place_type(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    previous = ctx.i._bundle(ctx.i._router(identifier))
    previous["router"]["kit"] = {"power": True}
    previous["loans"][0]["place"]["type"] = "room"
    previous["loans"][0]["kitOut"] = {"power": True}
    previous["loans"][0]["kitIn"] = None
    previous["loans"][0].pop("collectionEligible")
    event = ctx.s.event(1, identifier, "note", previous, ctx.i._bundle(ctx.i._router(identifier)))
    ctx.s.run("DELETE FROM event_states WHERE event_id=?", event)
    ctx.i.execute(
        1,
        key(),
        {"kind": "correct", "routerId": identifier, "eventId": event, "reason": "Старая операция"},
    )
    router = ctx.i.get(1, identifier)
    assert "kit" not in router
    assert not {"kitOut", "kitIn"} & router["loan"].keys()
    assert "type" not in router["loan"]["place"]
    assert router["loan"]["collectionEligible"] is True
