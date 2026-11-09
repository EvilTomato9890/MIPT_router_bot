import itertools
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from router_bot.campaigns import Campaigns
from router_bot.db import Store
from router_bot.service import Inventory, place
from router_bot.sheets.colors import ColorRepair
from router_bot.sheets.memory import MemorySheets
from router_bot.sheets.sync import Synchronizer

_counter = itertools.count()


def key():
    return f"test:{next(_counter)}"


def person(name="Тестовый получатель"):
    return {
        "kind": "person",
        "name": name,
        "group": "ТЕСТ-01",
        "admissionYear": 2026,
        "contact": {"raw": "", "phone": None, "telegram": None, "userId": None},
    }


def add(ctx, inventory="TEST-01", **more):
    return ctx.i.execute(
        1,
        key(),
        {
            "kind": "add",
            "inventory": inventory,
            "condition": "working",
            "storage": place("Тестовый склад"),
            **more,
        },
    )["routerId"]


def issue(ctx, identifier, **more):
    return ctx.i.execute(
        2,
        key(),
        {
            "kind": "issue",
            "routerId": identifier,
            "recipient": person(),
            "place": place("101", floor="1", section="А"),
            "dueAt": "2027-07-01",
            "collectionEligible": True,
            **more,
        },
    )


def receive(ctx, identifier, **more):
    return ctx.i.execute(
        2,
        key(),
        {
            "kind": "return",
            "routerId": identifier,
            "inventory": ctx.i.get(2, identifier)["inventory"],
            "physical": True,
            "storage": place("Тестовый склад"),
            "condition": "working",
            **more,
        },
    )


def campaign(ctx, **more):
    c = ctx.c.save(
        1,
        key(),
        {
            "name": "Тестовый сбор",
            "admissionYear": 2026,
            "startsAt": "2027-05-01",
            "deadline": "2027-07-01",
            "responsibleIds": [2],
            "location": "Тестовый склад",
            "hours": "10–18",
            "instruction": "Сдать роутер",
            **more,
        },
    )
    ctx.c.launch(1, key(), c["id"], c["version"])
    return c["id"]


@pytest.fixture
def ctx():
    s = Store()
    clock = SimpleNamespace(at=datetime(2027, 6, 1, 10, tzinfo=UTC))
    i = Inventory(s, bootstrap_admin_id=1, now=lambda: clock.at)
    i.set_role(1, 2, "operator", key())
    i.set_role(1, 3, "observer", key())
    a = MemorySheets()
    context = SimpleNamespace(
        s=s,
        i=i,
        a=a,
        sync=Synchronizer(i, a),
        colors=ColorRepair(i, a),
        c=Campaigns(i),
        clock=clock,
    )
    yield context
    s.close()
