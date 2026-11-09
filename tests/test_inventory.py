import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from conftest import add, issue, key, person, receive

from router_bot.db import Store
from router_bot.main import migrate, process_lock
from router_bot.service import Inventory, place
from router_bot.util import DomainError, csv_cell, iso_date, overdue


def test_access_revocation_and_last_admin(ctx):
    identifier = add(ctx)
    for operation in (
        lambda: ctx.i.get(99, identifier),
        lambda: ctx.i.list(99),
        lambda: ctx.i.history(99, identifier),
        lambda: ctx.i.stats(99),
        lambda: ctx.i.execute(3, key(), {"kind": "lost", "routerId": identifier, "reason": "test"}),
    ):
        with pytest.raises(DomainError, match="Нет доступа"):
            operation()
    ctx.i.set_role(1, 2, None, key())
    with pytest.raises(DomainError, match="Нет доступа"):
        issue(ctx, identifier)
    with pytest.raises(DomainError, match="последнего"):
        ctx.i.set_role(1, 1, None, key())
    ctx.i.set_role(1, 4, "admin", key())
    ctx.i.set_role(4, 1, None, key())
    Inventory(ctx.s, bootstrap_admin_id=1)
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.i.get(1, identifier)


def test_duplicates_normalization_and_placeholders(ctx):
    add(ctx, "X", mac="aa-bb-cc-00-11-22", serial="AB- 12")
    for inv, data in (("x", {}), ("Y", {"mac": "aabb.cc00.1122"}), ("Z", {"serial": "ab12"})):
        with pytest.raises(DomainError, match="уже существует|дубликат"):
            add(ctx, inv, **data)
    add(ctx, "N1", mac="нет", serial="нет")
    add(ctx, "N2", mac="-", serial="нет")
    assert len(ctx.i.list(1)) == 3


def test_duplicate_request_and_stale_confirmation(ctx):
    identifier = add(ctx)
    command = {
        "kind": "issue",
        "routerId": identifier,
        "recipient": person(),
        "place": place("101"),
        "dueAt": "2027-07-01",
        "expected": {identifier: 1},
    }
    request = key()
    first = ctx.i.execute(2, request, command)
    assert ctx.i.execute(2, request, command) == first
    assert len(ctx.i.loans(1, identifier)) == 1
    with pytest.raises(DomainError, match="Карточка изменилась"):
        ctx.i.execute(2, key(), command)
    with pytest.raises(DomainError, match="Ключ уже"):
        ctx.i.execute(2, request, {**command, "dueAt": "2027-08-01"})
    ctx.i.set_role(1, 2, None, key())
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.i.execute(2, request, command)


def test_complete_cycle_and_history(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.i.execute(
        1,
        key(),
        {"kind": "extend", "routerId": identifier, "dueAt": "2027-08-01", "reason": "Продление"},
    )
    receive(ctx, identifier)
    issue(ctx, identifier, recipient=person("Второй тестовый получатель"))
    loans = ctx.i.loans(1, identifier)
    assert len(loans) == 2 and not loans[0]["open"] and loans[1]["open"]
    assert loans[0]["returnedAt"] and loans[0]["dueAt"] == "2027-08-01"
    assert len(ctx.i.history(1, identifier)) == 5


def test_lost_found_and_physical_return(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.clock.at = datetime(2027, 7, 10, 10, tzinfo=UTC)
    assert overdue(ctx.i.get(1, identifier)["loan"], ctx.clock.at)
    assert ctx.i.get(1, identifier)["status"] == "issued"
    ctx.i.execute(2, key(), {"kind": "lost", "routerId": identifier, "reason": "Не найден"})
    assert ctx.i.get(1, identifier)["loan"]["recipient"]["name"] == "Тестовый получатель"
    ctx.i.execute(
        2, key(), {"kind": "found", "routerId": identifier, "reason": "Найден у получателя"}
    )
    assert ctx.i.get(1, identifier)["status"] == "lost"
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "resolve_found",
            "routerId": identifier,
            "resolution": "holder",
            "reason": "Остаётся у получателя",
        },
    )
    assert ctx.i.get(1, identifier)["status"] == "issued"
    receive(ctx, identifier)
    assert ctx.i.get(1, identifier)["loss"]


@pytest.mark.parametrize("condition", ["needs_check", "broken", "repair"])
def test_unchecked_or_broken_return_not_available(ctx, condition):
    identifier = add(ctx)
    issue(ctx, identifier)
    receive(ctx, identifier, condition=condition)
    with pytest.raises(DomainError, match="исправный"):
        issue(ctx, identifier)
    assert ctx.i.get(1, identifier)["status"] == "stored"
    assert ctx.i.list(1, {"report": "check"})


def test_atomic_replacement_and_transfer(ctx):
    old, new, busy = add(ctx, "OLD"), add(ctx, "NEW"), add(ctx, "BUSY")
    issue(ctx, old)
    issue(ctx, busy)
    command = {
        "kind": "replace",
        "routerId": old,
        "newRouterId": busy,
        "inventory": "OLD",
        "physical": True,
        "storage": place("Склад"),
        "condition": "broken",
        "reason": "Замена",
    }
    version = ctx.i.get(1, old)["version"]
    with pytest.raises(DomainError, match="недоступен"):
        ctx.i.execute(2, key(), command)
    assert ctx.i.get(1, old)["version"] == version
    ctx.i.execute(2, key(), {**command, "newRouterId": new})
    assert ctx.i.get(1, old)["status"] == "stored"
    assert ctx.i.loans(1, old)[0]["linkedLoanId"] == ctx.i.get(1, new)["loan"]["id"]
    current = ctx.i.get(1, new)["loan"]["id"]
    with pytest.raises(DomainError, match="срок"):
        ctx.i.execute(
            2,
            key(),
            {
                "kind": "transfer",
                "routerId": new,
                "recipient": person("Следующий"),
                "place": place("102"),
                "reason": "Нет срока",
            },
        )
    assert ctx.i.get(1, new)["loan"]["id"] == current
    ctx.i.execute(
        2,
        key(),
        {
            "kind": "transfer",
            "routerId": new,
            "recipient": person("Следующий"),
            "place": place("102"),
            "dueAt": "2027-07-02",
            "reason": "Передача",
        },
    )
    assert ctx.i.get(1, new)["loan"]["linkedLoanId"] == current
    assert len(ctx.i.loans(1, new)) == 2


def test_correction_keeps_original_and_rejects_dependent_events(ctx):
    identifier = add(ctx)
    bad = issue(ctx, identifier)
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "correct",
            "routerId": identifier,
            "eventId": bad["eventId"],
            "reason": "Ошибочно выбрали",
        },
    )
    assert ctx.i.get(1, identifier)["status"] == "stored"
    assert ctx.i.loans(1, identifier)[0]["closedKind"] == "correction"
    assert ctx.i.history(1, identifier)[-1]["correction_of"] == bad["eventId"]
    new = issue(ctx, identifier)
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Зависимое событие"})
    with pytest.raises(DomainError, match="зависимые"):
        ctx.i.execute(
            1,
            key(),
            {
                "kind": "correct",
                "routerId": identifier,
                "eventId": new["eventId"],
                "reason": "Ошибка",
            },
        )
    with pytest.raises(sqlite3.IntegrityError, match="Журнал"):
        ctx.s.run("DELETE FROM events")
    with pytest.raises(sqlite3.IntegrityError, match="Журнал"):
        ctx.s.run("UPDATE events SET action='x'")


def test_independent_vip_writeoff_and_organization(ctx):
    identifier = add(ctx)
    ctx.i.execute(
        1, key(), {"kind": "vip", "routerId": identifier, "vip": True, "reason": "Особая отметка"}
    )
    issue(
        ctx,
        identifier,
        recipient={
            "kind": "organization",
            "name": "Тестовая организация",
            "responsible": "Тестовый ответственный",
        },
    )
    assert ctx.i.stats(1)["physical"] == 1
    assert ctx.i.stats(1)["vip"] == 1
    with pytest.raises(DomainError, match="Отдельно"):
        ctx.i.execute(1, key(), {"kind": "write_off", "routerId": identifier, "reason": "Решение"})
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "write_off",
            "routerId": identifier,
            "reason": "Решение",
            "resolveOutstanding": True,
        },
    )
    assert ctx.i.get(1, identifier)["status"] == "written_off"
    assert ctx.i.loans(1, identifier)[0]["returnedAt"] is None


def test_contacts_purged_separately_from_equipment_history(ctx):
    identifier = add(ctx)
    issue(
        ctx,
        identifier,
        recipient={
            **person(),
            "contact": {
                "raw": "+70000000000",
                "phone": "+70000000000",
                "telegram": "test_demo",
                "userId": 123,
            },
        },
    )
    receive(ctx, identifier)
    assert "+70000000000" not in json.dumps(ctx.i.history(1, identifier))
    ctx.clock.at = datetime(2029, 1, 1, tzinfo=UTC)
    assert ctx.i.purge_contacts(1, 365, key())["count"] == 1
    assert ctx.i.loans(1, identifier)[0]["recipient"]["contact"] is None
    assert len(ctx.i.loans(1, identifier)) == 1


def test_no_kit_or_place_type_in_live_model(ctx):
    identifier = add(ctx, storage=place("Клуб 7ки"))
    issue(ctx, identifier, place=place("КДС 7ки"))
    r = ctx.i.get(1, identifier)
    assert "kit" not in r
    assert "type" not in r["loan"]["place"]
    assert not {"kitOut", "kitIn"} & r["loan"].keys()
    assert r["loan"]["place"]["name"] == "КДС 7ки"


def test_unknown_inventory_needs_physical_confirmation(ctx):
    identifier = add(ctx)
    with ctx.s.transaction():
        r = ctx.i._router(identifier)
        r["status"] = "unknown"
        ctx.i._save(r, key())
    with pytest.raises(DomainError, match="исправный"):
        issue(ctx, identifier)
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "inventory_check",
            "routerId": identifier,
            "inventory": "TEST-01",
            "physical": True,
            "storage": place("Склад"),
            "condition": "working",
            "reason": "Инвентаризация",
        },
    )
    issue(ctx, identifier)
    assert ctx.i.get(1, identifier)["status"] == "issued"


def test_metadata_in_sqlite_without_google_columns(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.i.execute(
        1,
        key(),
        {
            "kind": "loan_details",
            "routerId": identifier,
            "recipientKind": "person",
            "group": "НОВАЯ",
            "admissionYear": 2025,
            "academicYear": "",
            "contact": {
                "raw": "@demo_user +70000000000",
                "phone": "+70000000000",
                "telegram": "demo_user",
                "userId": None,
            },
            "issuedAt": "2027-06-01",
            "collectionEligible": False,
            "reason": "Данные проверены",
        },
    )
    loan = ctx.i.get(1, identifier)["loan"]
    assert loan["recipient"]["group"] == "НОВАЯ" and loan["recipient"]["admissionYear"] == 2025
    assert loan["collectionEligible"] is False
    assert len(ctx.i.loans(1, identifier)) == 1


def test_two_sqlite_connections_only_one_issue(tmp_path):
    path = tmp_path / "race.sqlite"
    s = Store(path)
    i = Inventory(s, bootstrap_admin_id=1)
    i.set_role(1, 2, "operator", key())
    i.set_role(1, 4, "operator", key())
    identifier = i.execute(1, key(), {"kind": "add", "inventory": "RACE", "condition": "working"})[
        "routerId"
    ]
    s.close()
    ready = threading.Barrier(2)

    def worker(actor):
        own = Store(path)
        service = Inventory(own, now=lambda: datetime(2027, 6, 1, tzinfo=UTC))
        ready.wait(timeout=10)
        try:
            service.execute(
                actor,
                f"race:{actor}",
                {
                    "kind": "issue",
                    "routerId": identifier,
                    "recipient": person(),
                    "place": place("101"),
                    "dueAt": "2027-07-01",
                },
            )
            return True
        except DomainError:
            return False
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, [2, 4]))
    assert sum(outcomes) == 1
    check = Store(path)
    try:
        assert check.get("SELECT count(*) AS n FROM loans WHERE open=1")["n"] == 1
    finally:
        check.close()


def test_node_database_migration_preserves_history_ids_and_replay(tmp_path):
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/legacy_node_v1.json").read_text(encoding="utf-8")
    )
    path = tmp_path / "legacy.sqlite"
    s = Store(path)
    try:
        for table, entries in fixture["tables"].items():
            s.run(f"DELETE FROM {table}") if table != "events" else None
            for row in entries:
                fields = ",".join(row)
                s.run(
                    f"INSERT INTO {table}({fields}) VALUES({','.join('?' for _ in row)})",
                    *row.values(),
                )
        migrate(s, tmp_path / "backups")
        i = Inventory(s, bootstrap_admin_id=99)
        assert i.get(1, fixture["routerId"])["id"] == fixture["routerId"]
        assert "type" not in i.get(1, fixture["routerId"])["loan"]["place"]
        assert "kit" not in i.get(1, fixture["routerId"])
        assert s.all("SELECT * FROM events ORDER BY rowid") == fixture["events"]
        assert i.execute(2, "legacy:issue", fixture["command"]) == fixture["issued"]
        assert len(i.loans(1, fixture["routerId"])) == 1
        from router_bot.campaigns import Campaigns
        from router_bot.sheets.memory import MemorySheets
        from router_bot.sheets.schema import FIELDS
        from router_bot.sheets.sync import Synchronizer

        i.now = lambda: datetime(2027, 6, 17, 10, tzinfo=UTC)
        campaigns = Campaigns(i)
        assert campaigns.list(1)[0]["id"] == fixture["campaignId"]
        assert (
            campaigns.tick(lambda *_: pytest.fail("Прежнее уведомление повторилось"))["sent"] == 0
        )
        assert s.get("SELECT count(*) AS n FROM reminders WHERE state='sent'")["n"] == 1
        adapter = MemorySheets(
            [{k: row["values"][k] for k in FIELDS} for row in fixture["sheetRows"]]
        )
        assert s.get("SELECT count(*) AS n FROM sessions")["n"] == 0
        assert s.get("SELECT count(*) AS n FROM drafts")["n"] == 0
        preview = Synchronizer(i, adapter).preview(1)
        assert all(e["type"] == "same" for e in preview["entries"])
        assert list((tmp_path / "backups").glob("before-python-*.sqlite"))
        with pytest.raises(DomainError, match="Нет доступа"):
            i.roles(99)
    finally:
        s.close()


def test_backup_restart_and_process_lock(tmp_path):
    path = tmp_path / "db.sqlite"
    s = Store(path)
    i = Inventory(s, bootstrap_admin_id=1)
    identifier = i.execute(1, key(), {"kind": "add", "inventory": "PERSIST"})["routerId"]
    s.backup(tmp_path / "copy.sqlite")
    s.close()
    restored = Store(tmp_path / "copy.sqlite")
    try:
        assert Inventory(restored).get(1, identifier)["inventory"] == "PERSIST"
        assert restored.get("PRAGMA integrity_check")["integrity_check"] == "ok"
        assert restored.get("SELECT count(*) AS n FROM outbox")["n"] == 1
    finally:
        restored.close()
    with process_lock(path):
        with pytest.raises(DomainError, match="Другой процесс"):
            with process_lock(path):
                pytest.fail("Вторая блокировка не должна проходить")
    with process_lock(path):
        pass


@pytest.mark.parametrize("value", ["2027-02-30", "01.02.2027", "2027-2-01"])
def test_invalid_dates(value):
    with pytest.raises(DomainError, match="Дата"):
        iso_date(value)


@pytest.mark.parametrize("value", ["=1+1", " \t=1", "+1", "@formula", "-1", "\ufeff=1"])
def test_csv_injection(value):
    assert csv_cell(value).startswith("\"'")


def test_moscow_day_and_period_counts(ctx):
    identifier = add(ctx)
    ctx.clock.at = datetime(2027, 6, 1, 22, tzinfo=UTC)
    issue(ctx, identifier)
    assert ctx.i.get(1, identifier)["loan"]["issuedAt"] == "2027-06-02"
    report = ctx.i.loan_report(1, "2027-06-02", "2027-06-02")
    assert report["loans"] == 1 and report["physical"] == 1
