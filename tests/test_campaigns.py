from datetime import UTC, datetime, timedelta

import pytest
from conftest import add, campaign, issue, key, receive

from router_bot.util import DomainError


def test_vip_extension_and_exception_change_queue(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.i.execute(
        1, key(), {"kind": "vip", "routerId": identifier, "vip": True, "reason": "Отметка"}
    )
    c = campaign(ctx)
    assert len(ctx.c.queue(2, c)) == 1
    ctx.clock.at = datetime(2027, 6, 17, 10, tzinfo=UTC)
    sent = []
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 1
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0
    ctx.i.execute(
        1,
        key(),
        {"kind": "extend", "routerId": identifier, "dueAt": "2027-08-01", "reason": "Продление"},
    )
    assert ctx.c.queue(2, c)[0]["due"] == "2027-08-01"
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "exception",
            "routerId": identifier,
            "reviewAt": "2027-09-01",
            "reason": "Исключение",
        },
    )
    ctx.clock.at = datetime(2027, 8, 2, 10, tzinfo=UTC)
    assert ctx.c.queue(2, c)[0]["excluded"]
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0
    ctx.i.execute(
        1,
        key(),
        {"kind": "exception", "routerId": identifier, "enabled": False, "reason": "Возобновить"},
    )
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 1


def test_no_automatic_org_unknown_cohort_or_unconfirmed_collection(ctx):
    personal, org, shared, unknown = [
        add(ctx, inv) for inv in ("STUDENT", "ORG", "SHARED", "UNKNOWN")
    ]
    issue(ctx, personal)
    issue(ctx, org, recipient={"kind": "organization", "name": "Тестовая организация"})
    issue(ctx, shared, collectionEligible=False)
    issue(ctx, unknown, recipient={"kind": "person", "name": "Без набора"})
    c = campaign(ctx)
    assert len(ctx.c.queue(2, c)) == 1
    assert len(ctx.c.preview(1, c)["clarify"]) == 2
    ctx.c.member(
        1,
        key(),
        c,
        ctx.i.get(1, unknown)["loan"]["id"],
        {"action": "include", "reason": "Набор проверен отдельно"},
    )
    assert len(ctx.c.queue(2, c)) == 2


def test_natural_queue_actions_and_actual_returns(ctx):
    identifiers = [add(ctx, inv) for inv in ("A", "B", "C")]
    from router_bot.service import place

    for identifier, room in zip(identifiers, ("102", "12", "2")):
        issue(ctx, identifier, place=place(room))
    c = campaign(ctx)
    assert [x["loan"]["place"]["name"] for x in ctx.c.queue(2, c)] == ["2", "12", "102"]
    loan_id = ctx.i.get(1, identifiers[0])["loan"]["id"]
    ctx.c.member(2, key(), c, loan_id, {"action": "note", "reason": "Результат обхода"})
    assert ctx.i.get(1, identifiers[0])["loan"]["dueAt"] == "2027-07-01"
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.c.member(2, key(), c, loan_id, {"action": "exclude", "reason": "Исключение"})
    receive(ctx, identifiers[0])
    assert len(ctx.c.queue(2, c)) == 2 and ctx.c.report(2, c)["returned"] == 1


def test_downtime_one_current_summary_return_and_revocation(ctx):
    one, two = add(ctx, "A"), add(ctx, "B")
    issue(ctx, one)
    issue(ctx, two)
    campaign(ctx)
    ctx.clock.at = datetime(2027, 7, 5, 11, tzinfo=UTC)
    sent = []
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 1
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0
    ctx.clock.at += timedelta(days=1)
    ctx.i.set_role(1, 2, None, key())
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0
    ctx.i.set_role(1, 2, "operator", key())
    receive(ctx, one)
    receive(ctx, two)
    assert ctx.c.tick(lambda *a: sent.append(a))["sent"] == 0


def test_delivery_failure_keeps_job_and_does_not_change_inventory(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    campaign(ctx)
    ctx.clock.at = datetime(2027, 6, 17, 11, tzinfo=UTC)
    before = ctx.i.get(1, identifier)["version"]

    def fail(*_):
        raise TimeoutError("timeout")

    assert ctx.c.tick(fail)["failed"] == 1
    assert ctx.i.get(1, identifier)["version"] == before
    assert ctx.s.get("SELECT count(*) AS n FROM reminders WHERE state='pending'")["n"] == 1
    assert ctx.c.tick(lambda *_: pytest.fail("Слишком ранний повтор"))["sent"] == 0
    ctx.clock.at += timedelta(minutes=1)
    assert ctx.c.tick(lambda *_: None)["sent"] == 1
