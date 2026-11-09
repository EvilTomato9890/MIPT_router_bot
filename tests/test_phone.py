import pytest
from conftest import add, issue, key, person
from test_bot import FakeTelegram, callback, confirm_button, message

from router_bot.bot import Bot
from router_bot.campaigns import Campaigns
from router_bot.contacts import observe
from router_bot.db import Store
from router_bot.dialogs import fields
from router_bot.service import Inventory, place
from router_bot.util import DomainError, contact, phone_number, stable


def current_field(bot):
    state = bot._session(2)
    return fields(state["kind"], state["data"])[state["step"]].key


def click(bot, telegram, label):
    value = next(
        b["callback_data"]
        for row in telegram.messages[-1]["markup"]["inline_keyboard"]
        for b in row
        if b["text"] == label
    )
    bot.handle(callback(2, value))
    return value


def phone_step(ctx):
    identifier = add(ctx)
    telegram = FakeTelegram()
    bot = Bot(ctx.i, telegram, sync=ctx.sync, campaigns=ctx.c)
    bot.start(2, "issue", {"routerId": identifier})
    for value in ("person", "Тестовый получатель", "2026"):
        bot.handle(message(2, value))
    assert current_field(bot) == "phone"
    return identifier, bot, telegram


def select_id(bot, telegram):
    click(bot, telegram, "Дополнительно")
    return click(bot, telegram, "Использовать Telegram ID")


def finish(bot, telegram):
    bot.handle(message(2, "101"))
    bot.handle(message(2, "2027-07-01"))
    confirmation = confirm_button(telegram)
    bot.handle(callback(2, confirmation))
    return confirmation


@pytest.mark.parametrize(
    "value",
    [
        "-",
        "",
        "@known_user",
        "tg://user?id=123456789",
        "abc",
        "+123",
        "1234567890123456",
        "++79991234567",
    ],
)
def test_phone_is_required_even_when_username_is_known(ctx, value):
    observe(ctx.s, {"id": 100, "username": "known_user"}, ctx.clock.at)
    identifier, bot, telegram = phone_step(ctx)
    step = bot._session(2)["step"]
    bot.handle(message(2, value))
    assert current_field(bot) == "phone" and bot._session(2)["step"] == step
    assert "Введите телефон" in telegram.messages[-1]["text"]
    assert ctx.i.get(1, identifier)["loan"] is None


@pytest.mark.parametrize("value", ["+7 (999) 123-45-67", "89991234567", "+123456789012345"])
def test_default_phone_is_saved_in_existing_contacts_column(ctx, value):
    identifier, bot, telegram = phone_step(ctx)
    bot.handle(message(2, value))
    assert current_field(bot) == "placeName"
    confirmation = finish(bot, telegram)
    bot.handle(callback(2, confirmation))
    saved = ctx.i.get(1, identifier)["loan"]["recipient"]["contact"]
    assert saved["phone"] == phone_number(value) and saved["method"] == "phone"
    assert saved["userId"] is None
    assert len(ctx.i.loans(1, identifier)) == 1
    assert ctx.sync.flush()["sent"] == 1
    assert ctx.a.sheet["rows"][0]["values"]["rawContact"] == phone_number(value)
    assert len(ctx.a.sheet["rows"][0]["values"]) == 10


@pytest.mark.parametrize("value", ["-", "", "0", "-123", "@unknown_user", "99999999999999999999"])
def test_id_alternative_cannot_be_empty_or_invalid(ctx, value):
    identifier, bot, telegram = phone_step(ctx)
    select_id(bot, telegram)
    assert current_field(bot) == "userId"
    bot.handle(message(2, value))
    assert current_field(bot) == "userId"
    assert ctx.i.get(1, identifier)["loan"] is None


def test_additional_button_replaces_phone_with_required_id(ctx):
    identifier, bot, telegram = phone_step(ctx)
    select_id(bot, telegram)
    bot.handle(message(2, "123456789"))
    assert current_field(bot) == "placeName"
    finish(bot, telegram)
    saved = ctx.i.get(1, identifier)["loan"]["recipient"]["contact"]
    assert saved["method"] == "telegram_id" and saved["phone"] is None
    assert saved["userId"] == 123456789
    ctx.sync.flush()
    assert ctx.a.sheet["rows"][0]["values"]["rawContact"] == "tg://user?id=123456789"


def test_switching_back_and_stale_buttons_cannot_change_the_new_step(ctx):
    _, bot, telegram = phone_step(ctx)
    stale = select_id(bot, telegram)
    click(bot, telegram, "Дополнительно")
    click(bot, telegram, "Использовать телефон")
    assert current_field(bot) == "phone"
    bot.handle(message(2, "+70000000000"))
    bot.handle(callback(2, stale))
    assert current_field(bot) == "placeName"
    assert bot._session(2)["data"]["contactMethod"] == "phone"
    assert "устарела" in telegram.messages[-1]["text"]


@pytest.mark.parametrize("mode", ["phone", "telegram_id"])
def test_required_contact_survives_reopening_the_database(ctx, tmp_path, mode):
    identifier, bot, telegram = phone_step(ctx)
    if mode == "telegram_id":
        select_id(bot, telegram)
    path = tmp_path / "unfinished-contact.sqlite"
    ctx.s.backup(path)
    store = Store(path)
    try:
        inventory = Inventory(store, now=ctx.i.now)
        telegram = FakeTelegram()
        bot = Bot(inventory, telegram, campaigns=Campaigns(inventory))
        bot.handle(message(2, "/resume"))
        assert current_field(bot) == ("phone" if mode == "phone" else "userId")
        bot.handle(message(2, "-"))
        assert bot._session(2)["mode"] == "wizard"
        bot.handle(message(2, "+70000000000" if mode == "phone" else "123456789"))
        finish(bot, telegram)
        assert inventory.get(1, identifier)["loan"]["recipient"]["contact"]["method"] == mode
    finally:
        store.close()


def test_previous_confirmation_returns_to_required_contact_and_preserves_answers(ctx):
    identifier = add(ctx)
    data = {
        "routerId": identifier,
        "recipientKind": "person",
        "recipientName": "Сохранённое имя",
        "admissionYear": 2026,
        "contactInput": "@unknown_user",
        "userId": None,
        "placeName": "Сохранённая комната",
        "dueAt": "2027-07-01",
    }
    command = {
        "kind": "issue",
        "routerId": identifier,
        "recipient": {**person(), "contact": contact("@unknown_user")},
        "place": place("Сохранённая комната"),
        "dueAt": "2027-07-01",
    }
    draft = ctx.s.draft(2, "confirm", {"kind": "operation", "command": command}, ctx.clock.at)
    ctx.s.run(
        "INSERT INTO sessions VALUES(?,?)",
        2,
        stable(
            {
                "mode": "confirm",
                "kind": "issue",
                "data": data,
                "step": 10,
                "visited": [],
                "nonce": "oldnonce",
                "dialogVersion": 2,
                "draftId": draft,
            }
        ),
    )
    telegram = FakeTelegram()
    bot = Bot(ctx.i, telegram, campaigns=ctx.c)
    bot.handle(callback(2, "confirm:" + draft))
    assert ctx.i.get(1, identifier)["loan"] is None
    assert current_field(bot) == "phone"
    assert bot._session(2)["data"]["recipientName"] == "Сохранённое имя"
    assert not ctx.s.get("SELECT 1 FROM drafts WHERE id=?", draft)
    bot.handle(message(2, "+70000000000"))
    assert bot._session(2)["mode"] == "confirm"
    bot.handle(callback(2, confirm_button(telegram)))
    assert ctx.i.get(1, identifier)["loan"]["place"]["name"] == "Сохранённая комната"


@pytest.mark.parametrize("kind", ["issue", "transfer", "loan_details"])
def test_accounting_cannot_bypass_required_phone_with_an_automatic_id(ctx, kind):
    identifier = add(ctx)
    if kind != "issue":
        issue(ctx, identifier)
    before = ctx.i.get(1, identifier)
    observe(ctx.s, {"id": 100, "username": "known_user"}, ctx.clock.at)
    details = {**contact("@known_user"), "method": "phone"}
    command = {"kind": kind, "routerId": identifier, "reason": "Проверка"}
    if kind == "loan_details":
        command.update(recipientKind="person", contact=details)
    else:
        command.update(
            recipient={**person(), "contact": details}, place=place("101"), dueAt="2027-07-01"
        )
    with pytest.raises(DomainError, match="телефон"):
        ctx.i.execute(1, key(), command)
    assert ctx.i.get(1, identifier) == before
