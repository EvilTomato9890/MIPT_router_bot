import itertools

import pytest
from conftest import add, campaign, issue, key, receive

from router_bot.bot import Bot
from router_bot.config import config
from router_bot.dialogs import fields
from router_bot.telegram import Telegram
from router_bot.util import DomainError

_updates = itertools.count(100)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.last_file = None

    def send(self, actor, message, markup=None):
        for row in (markup or {}).get("inline_keyboard", []):
            for b in row:
                assert len(b["callback_data"].encode("utf-8")) <= 64
        self.messages.append({"id": actor, "text": message, "markup": markup})

    def answer(self, identifier, message=""):
        pass

    def file(self, actor, name, content):
        self.last_file = {"id": actor, "name": name, "content": content}


@pytest.fixture
def ui(ctx):
    t = FakeTelegram()
    bot = Bot(ctx.i, t, sync=ctx.sync, colors=ctx.colors, campaigns=ctx.c, mode="memory")
    return bot, t


def message(actor, text, n=None):
    return {
        "update_id": next(_updates) if n is None else n,
        "message": {"from": {"id": actor}, "chat": {"id": actor, "type": "private"}, "text": text},
    }


def callback(actor, data):
    n = next(_updates)
    return {
        "update_id": n,
        "callback_query": {
            "id": "query" + str(n),
            "from": {"id": actor},
            "data": data,
            "message": {"chat": {"id": actor, "type": "private"}},
        },
    }


def confirm_button(t):
    return next(
        b["callback_data"]
        for row in t.messages[-1]["markup"]["inline_keyboard"]
        for b in row
        if b["callback_data"].startswith("confirm:")
    )


def test_no_unauthorized_or_group_data(ctx, ui):
    bot, t = ui
    identifier = add(ctx, "СЕКРЕТНЫЙ-НОМЕР")
    bot.handle(callback(99, "card:" + identifier))
    assert not any("СЕКРЕТНЫЙ" in m["text"] for m in t.messages)
    assert "Нет доступа" in t.messages[-1]["text"]
    before = len(t.messages)
    bot.handle(
        {
            "update_id": next(_updates),
            "message": {"from": {"id": 1}, "chat": {"id": -10, "type": "group"}, "text": "/list"},
        }
    )
    assert len(t.messages) == before


def test_revoked_dialog_and_old_admin_buttons(ctx, ui):
    bot, t = ui
    identifier = add(ctx)
    bot.start(2, "lost", {"routerId": identifier, "expected": {identifier: 1}})
    bot.answer_wizard(2, "Тестовая причина")
    confirm = confirm_button(t)
    ctx.i.set_role(1, 2, None, key())
    bot.handle(callback(2, confirm))
    assert ctx.i.get(1, identifier)["status"] == "stored"
    assert "Нет доступа" in t.messages[-1]["text"]
    ctx.i.set_role(1, 2, "admin", key())
    preview, colors = ctx.sync.preview(2), ctx.colors.preview(2)
    ctx.i.set_role(1, 2, "observer", key())
    bot.handle(callback(2, "sa:" + preview["id"]))
    assert "Нет доступа" in t.messages[-1]["text"]
    bot.handle(callback(2, "confirm:" + colors["id"]))
    assert "Нет доступа" in t.messages[-1]["text"]


def test_add_dialog_back_cancel_duplicate_update_and_confirmation(ctx, ui):
    bot, t = ui
    bot.start(2, "add")
    first = message(2, "DIALOG-1")
    bot.handle(first)
    bot.handle(first)
    assert bot._session(2)["step"] == 1
    bot.back(2)
    bot.handle(message(2, "DIALOG-1"))
    answers = {
        "model": "-",
        "serial": "-",
        "mac": "-",
        "condition": "working",
        "storageName": "Тестовый склад",
    }
    while bot._session(2):
        session = bot._session(2)
        field = fields(session["kind"], session["data"])[session["step"]]
        bot.handle(message(2, answers[field.key]))
    assert not ctx.i.list(1)
    confirm = confirm_button(t)
    bot.handle(callback(2, confirm))
    bot.handle(callback(2, confirm))
    assert len(ctx.i.list(1)) == 1
    assert len(ctx.i.history(1, ctx.i.list(1)[0]["id"])) == 1
    bot.start(2, "add")
    bot.handle(message(2, "CANCELLED"))
    bot.handle(message(2, "/cancel"))
    assert len(ctx.i.list(1)) == 1


def test_step_nonce_and_version_confirmation(ctx, ui):
    bot, t = ui
    identifier = add(ctx)
    data = {"routerId": identifier, "expected": {identifier: 1}}
    bot.start(1, "vip", data)
    old = t.messages[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.start(1, "vip", data)
    bot.handle(callback(1, old))
    assert bot._session(1)["step"] == 0 and "устарела" in t.messages[-1]["text"]
    bot.answer_wizard(1, "yes")
    bot.answer_wizard(1, "Тестовый смысл")
    confirm = confirm_button(t)
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Параллельная правка"})
    bot.handle(callback(1, confirm))
    assert not ctx.i.get(1, identifier)["vip"]
    assert "Карточка изменилась" in t.messages[-1]["text"]


def test_views_export_queue_filter_and_button_sizes(ctx, ui):
    bot, t = ui
    for n in range(8):
        add(ctx, f"P-{n}")
    bot.show_list(3)
    assert "Найдено: 8" in t.messages[-1]["text"]
    bot.reports(3)
    bot.export(3)
    assert t.last_file["content"].startswith("\ufeff")
    with pytest.raises(DomainError, match="Нет доступа"):
        bot.settings(3)
    identifier = ctx.i.list(1)[0]["id"]
    issue(ctx, identifier)
    c = campaign(ctx)
    bot.campaign_preview(1, c)
    bot.queue(2, c)
    bot.card(1, identifier)
    bot.history(3, identifier)
    bot.start(3, "queue_filters", {"campaignId": c})
    for answer in ("-", "-", "2"):
        bot.answer_wizard(3, answer)
    assert len(t.messages) > 10


def test_old_queue_button_never_targets_next_loan(ctx, ui):
    bot, t = ui
    one, two = add(ctx, "Q1"), add(ctx, "Q2")
    issue(ctx, one)
    issue(ctx, two)
    c = campaign(ctx)
    bot.queue(2, c)
    action = next(
        b["callback_data"]
        for row in t.messages[-1]["markup"]["inline_keyboard"]
        for b in row
        if b["callback_data"].startswith("qa:")
    )
    receive(ctx, one)
    bot.handle(callback(2, action))
    assert "Очередь изменилась" in t.messages[-1]["text"]
    assert bot._session(2) is None


def test_removed_fields_absent_from_all_dialogs_and_no_provision(ui):
    bot, t = ui
    kinds = [
        "add",
        "issue",
        "return",
        "transfer",
        "replace",
        "resolve_found",
        "move",
        "condition",
        "loan_details",
        "inventory_check",
    ]
    for kind in kinds:
        assert not {"power", "cable", "other", "kit", "placeType", "storageType"} & {
            f.key for f in fields(kind)
        }
        assert not any(
            "комплект" in f.prompt.lower() or "тип места" in f.prompt.lower() for f in fields(kind)
        )
    bot.message(1, "/provision")
    assert "Добавление столбцов отключено" in t.messages[-1]["text"]


def test_telegram_errors_never_expose_token_or_response():
    class Failed:
        def post(self, url, **kwargs):
            raise RuntimeError(url + " SECRET")

    with pytest.raises(DomainError) as error:
        Telegram("123:TEST_SECRET", Failed()).send(1, "private")
    assert "SECRET" not in str(error.value)


def test_config_requires_ids_and_token_but_backup_does_not():
    with pytest.raises(DomainError, match="BOT_TOKEN"):
        config({})
    with pytest.raises(DomainError, match="BOOTSTRAP"):
        config({"BOT_TOKEN": "123:TEST_SECRET", "BOOTSTRAP_ADMIN_ID": "0"})
    c = config({}, require_bot=False)
    assert c.db == "./data/router-bot.sqlite" and c.mode == "disabled"
