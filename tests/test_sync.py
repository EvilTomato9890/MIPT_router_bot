from datetime import UTC, datetime, timedelta

import pytest
from conftest import add, issue, key, receive

from router_bot.service import place
from router_bot.sheets.schema import FIELDS
from router_bot.util import DomainError


def legacy(inv, status="Свободен", **more):
    return {
        "inventory": inv,
        "mac": "",
        "place": "Клуб 7ки",
        "status": status,
        "checked": True,
        "owner": "",
        "checkin": "",
        "checkout": "",
        "rawContact": "",
        "note": "",
        **more,
    }


def put(ctx, values, number):
    ctx.a.sheet["rows"].append({"row": number, "values": values, "formulas": []})


def test_initial_import_real_headers_unknown_org_false_and_dates(ctx):
    put(
        ctx,
        legacy(
            "L1",
            "Выдан",
            owner="Тестовый человек",
            rawContact="TG @demo_user +7 (000) 000-00-00",
            checkout="НЕСОПОСТАВЛЕННАЯ ДАТА",
        ),
        2,
    )
    put(ctx, legacy("L2", "?", owner="Наследуемая пометка"), 3)
    put(ctx, legacy("L3", "Выдан", owner="Студсовет ФПМИ"), 4)
    put(ctx, legacy("L4", owner="Исторический получатель"), 5)
    put(ctx, {"inventory": "", "checked": False}, 6)
    preview = ctx.sync.preview(1)
    assert len(ctx.i.list(1)) == 0
    assert sum(e["type"] == "new" for e in preview["entries"]) == 4
    result = ctx.sync.apply(1, preview["id"])
    assert result["added"] == 4
    r = ctx.i.get(1, "L1")
    assert r["condition"] == "needs_check" and r["legacy"]["checked"] is True
    assert r["loan"]["dueAt"] is None and r["loan"]["issuedAt"] is None
    assert not r["loan"]["dueConfirmed"] and not r["loan"]["collectionEligible"]
    assert r["loan"]["recipient"]["kind"] == "unknown"
    assert r["loan"]["recipient"]["contact"]["telegram"] == "demo_user"
    assert r["loan"]["recipient"]["contact"]["userId"] is None
    assert r["legacy"]["checkout"] == "НЕСОПОСТАВЛЕННАЯ ДАТА"
    assert r["loan"]["place"]["name"] == "Клуб 7ки" and "type" not in r["loan"]["place"]
    assert ctx.i.get(1, "L2")["status"] == "unknown"
    assert ctx.i.get(1, "L3")["loan"]["recipient"]["kind"] == "organization"
    ctx.sync.flush()
    repeat = ctx.sync.preview(1)
    assert all(e["type"] == "same" for e in repeat["entries"])
    assert len(ctx.i.loans(1, r["id"])) == 1
    assert ctx.i.get(1, "L4")["loan"] is None
    assert ctx.i.get(1, "L4")["legacy"]["owner"] == "Исторический получатель"
    ctx.a.sheet["rows"][3]["values"]["status"] = "Утерян"
    assert (
        next(
            e
            for e in ctx.sync.preview(1)["entries"]
            if e.get("routerId") == ctx.i.get(1, "L4")["id"]
        )["type"]
        == "review"
    )


def test_offline_outbox_and_retry_without_duplicates(ctx):
    identifier = add(ctx)
    ctx.a.offline = True
    assert ctx.sync.flush()["failed"] == 1
    assert ctx.i.get(1, identifier)["status"] == "stored"
    assert ctx.s.get("SELECT count(*) AS n FROM outbox")["n"] == 1
    ctx.a.offline = False
    ctx.clock.at += timedelta(seconds=10)
    assert ctx.sync.flush()["sent"] == 1
    assert ctx.sync.flush()["sent"] == 0
    assert len(ctx.a.sheet["rows"]) == 1


def test_sorting_and_moving_rows_keep_internal_ids(ctx):
    one, two = add(ctx, "ONE"), add(ctx, "TWO")
    ctx.sync.flush()
    ctx.a.sheet["rows"].reverse()
    for i, row in enumerate(ctx.a.sheet["rows"]):
        row["row"] = i + 8
    ctx.i.execute(2, key(), {"kind": "note", "routerId": one, "note": "Проверка привязки"})
    assert ctx.sync.flush()["sent"] == 1
    values = {r["values"]["inventory"]: r["values"] for r in ctx.a.sheet["rows"]}
    assert values["ONE"]["note"] == "Проверка привязки" and not values["TWO"]["note"]
    assert ctx.i.get(1, "ONE")["id"] == one and ctx.i.get(1, "TWO")["id"] == two
    assert all(set(r["values"]) == set(FIELDS) for r in ctx.a.sheet["rows"])


def test_unknown_structure_blocks_outgoing_write(ctx):
    identifier = add(ctx)
    ctx.sync.flush()
    ctx.a.sheet["headers"][1] = "MAC без исходного пробела"
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Не отправлять"})
    assert ctx.sync.flush()["blocked"] == 1
    assert ctx.a.sheet["rows"][0]["values"]["note"] == ""


def test_preview_cancel_and_confirm_place_contact_note_new_record(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"].update(
        place="КДС 7ки", rawContact="TG @changed_demo", note="Ручное примечание"
    )
    put(ctx, legacy("MANUAL-NEW"), 20)
    preview = ctx.sync.preview(1)
    assert ctx.i.get(1, identifier)["note"] == "" and len(ctx.i.list(1)) == 1
    result = ctx.sync.apply(1, preview["id"])
    assert result["added"] == 1 and result["updated"] == 1
    r = ctx.i.get(1, identifier)
    assert r["loan"]["place"]["name"] == "КДС 7ки"
    assert r["loan"]["recipient"]["contact"]["telegram"] == "changed_demo"
    assert r["note"] == "Ручное примечание"
    assert len(ctx.i.loans(1, identifier)) == 1
    assert ctx.i.history(1, identifier)[-1]["action"] == "Импорт из таблицы"


def test_technical_and_cohort_metadata_stays_in_db(ctx):
    identifier = add(ctx, model="TEST-MODEL", serial="TEST-SERIAL")
    issue(ctx, identifier)
    ctx.i.execute(
        1, key(), {"kind": "vip", "routerId": identifier, "vip": True, "reason": "Особая отметка"}
    )
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"]["note"] = "Ручная правка"
    preview = ctx.sync.preview(1)
    ctx.sync.apply(1, preview["id"])
    r = ctx.i.get(1, identifier)
    assert r["vip"] and r["model"] == "TEST-MODEL" and r["serial"] == "TEST-SERIAL"
    assert r["loan"]["recipient"]["admissionYear"] == 2026
    assert r["loan"]["dueAt"] == "2027-07-01" and r["loan"]["dueConfirmed"]
    assert set(ctx.a.sheet["rows"][0]["values"]) == set(FIELDS)


def test_manual_status_and_owner_need_real_domain_operations(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"]["status"] = "Свободен"
    preview = ctx.sync.preview(1)
    assert preview["entries"][0]["type"] == "review"
    ctx.sync.apply(1, preview["id"])
    assert ctx.i.get(1, identifier)["status"] == "issued"
    ctx.a.sheet["rows"][0]["values"]["status"] = "Выдан"
    ctx.a.sheet["rows"][0]["values"]["owner"] = "Другой получатель"
    assert ctx.sync.preview(1)["entries"][0]["type"] == "review"


def test_independent_changes_merge_and_same_field_conflicts(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    ctx.i.execute(2, key(), {"kind": "move", "routerId": identifier, "place": place("103")})
    ctx.a.sheet["rows"][0]["values"]["note"] = "Из таблицы"
    preview = ctx.sync.preview(1)
    assert preview["entries"][0]["type"] == "change"
    ctx.sync.apply(1, preview["id"])
    assert ctx.i.get(1, identifier)["loan"]["place"]["name"] == "103"
    ctx.sync.flush()
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Бот"})
    ctx.a.sheet["rows"][0]["values"]["note"] = "Таблица"
    preview = ctx.sync.preview(1)
    assert preview["entries"][0]["type"] == "conflict"
    ctx.sync.apply(1, preview["id"])
    assert ctx.i.get(1, identifier)["note"] == "Бот"
    assert ctx.s.get("SELECT state FROM outbox WHERE router_id=?", identifier)["state"] == "blocked"
    resolve = ctx.sync.preview(1)
    ctx.sync.apply(1, resolve["id"], {identifier: {"source": "sheet", "reason": "Сверено"}})
    assert ctx.i.get(1, identifier)["note"] == "Таблица"


def test_linked_owner_place_conflict(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    ctx.i.execute(2, key(), {"kind": "move", "routerId": identifier, "place": place("104")})
    ctx.a.sheet["rows"][0]["values"]["owner"] = "Другой тестовый"
    assert "связанные поля выдачи" in ctx.sync.preview(1)["entries"][0]["conflicts"]


def test_inventory_rename_removal_and_duplicates_do_not_create_new_identity(ctx):
    identifier = add(ctx)
    ctx.sync.flush()
    original = ctx.a.read()["rows"][0]
    ctx.a.sheet["rows"][0]["values"]["inventory"] = "RENAMED"
    preview = ctx.sync.preview(1)
    assert any(e["type"] == "missing" for e in preview["entries"])
    assert not any(e["type"] == "new" for e in preview["entries"])
    ctx.sync.apply(1, preview["id"])
    assert len(ctx.i.list(1)) == 1 and ctx.i.get(1, identifier)["inventory"] == "TEST-01"
    ctx.a.sheet["rows"] = [original, {**original, "row": 9}]
    preview = ctx.sync.preview(1)
    assert sum(e["type"] == "error" for e in preview["entries"]) == 2


def test_duplicate_mac_new_rows_rejected_together(ctx):
    put(ctx, legacy("DUP-A", mac="AA:BB:CC:00:11:22"), 2)
    put(ctx, legacy("DUP-B", mac="aabb.cc00.1122"), 3)
    p = ctx.sync.preview(1)
    assert all(e["type"] == "error" for e in p["entries"])
    ctx.sync.apply(1, p["id"])
    assert not ctx.i.list(1)


def test_pending_write_keeps_manual_change_and_other_routers_continue(ctx):
    one, two = add(ctx, "ONE"), add(ctx, "TWO")
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"]["note"] = "Ручная правка"
    ctx.i.execute(2, key(), {"kind": "note", "routerId": one, "note": "Бот"})
    ctx.i.execute(2, key(), {"kind": "note", "routerId": two, "note": "Независимо"})
    result = ctx.sync.flush()
    assert result["blocked"] == 1 and result["sent"] == 1
    assert ctx.a.sheet["rows"][0]["values"]["note"] == "Ручная правка"
    preview = ctx.sync.preview(1)
    ctx.sync.apply(1, preview["id"], {one: {"source": "bot", "reason": "Сверено"}})
    ctx.sync.flush()
    assert ctx.a.sheet["rows"][0]["values"]["note"] == "Бот"


def test_stale_sheet_and_database_preview_rejected(ctx):
    identifier = add(ctx)
    ctx.sync.flush()
    ctx.a.sheet["rows"][0]["values"]["note"] = "A"
    preview = ctx.sync.preview(1)
    ctx.a.sheet["rows"][0]["values"]["note"] = "B"
    with pytest.raises(DomainError, match="Таблица изменилась"):
        ctx.sync.apply(1, preview["id"])
    preview = ctx.sync.preview(1)
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "БД"})
    with pytest.raises(DomainError, match="База изменилась"):
        ctx.sync.apply(1, preview["id"])


def test_import_replaces_old_queue_and_resync_is_noop(ctx):
    identifier = add(ctx)
    ctx.sync.flush()
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "Устаревшее"})
    ctx.a.sheet["rows"][0]["values"]["note"] = "Импортированное"
    preview = ctx.sync.preview(1)
    ctx.sync.apply(1, preview["id"], {identifier: {"source": "sheet", "reason": "Импорт"}})
    ctx.sync.flush()
    assert ctx.a.sheet["rows"][0]["values"]["note"] == "Импортированное"
    assert all(e["type"] == "same" for e in ctx.sync.preview(1)["entries"])


def test_replacement_outgoing_group_not_written_half(ctx):
    old, new = add(ctx, "OLD"), add(ctx, "NEXT")
    issue(ctx, old)
    ctx.sync.flush()
    ctx.i.execute(
        2,
        key(),
        {
            "kind": "replace",
            "routerId": old,
            "newRouterId": new,
            "inventory": "OLD",
            "physical": True,
            "storage": place("Склад"),
            "condition": "broken",
            "reason": "Замена",
        },
    )
    next(row for row in ctx.a.sheet["rows"] if row["values"]["inventory"] == "OLD")["values"][
        "note"
    ] = "Ручное"
    assert ctx.sync.flush()["blocked"] == 2
    assert (
        next(row for row in ctx.a.sheet["rows"] if row["values"]["inventory"] == "NEXT")["values"][
            "status"
        ]
        == "Свободен"
    )


def test_moscow_day_overdue_uses_status_only(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    ctx.clock.at = datetime(2027, 7, 1, 22, tzinfo=UTC)
    ctx.sync.refresh_day()
    assert ctx.sync.flush()["sent"] == 1
    assert ctx.a.sheet["rows"][0]["values"]["status"] == "Просрочен"
    assert ctx.i.get(1, identifier)["status"] == "issued"
    assert set(ctx.a.sheet["rows"][0]["values"]) == set(FIELDS)


def test_sync_admin_only_and_outgoing_formula_preserved(ctx):
    identifier = add(ctx)
    ctx.sync.flush()
    with pytest.raises(DomainError, match="Нет доступа"):
        ctx.sync.preview(2)
    ctx.a.sheet["rows"][0]["formulas"] = ["note"]
    ctx.i.execute(2, key(), {"kind": "note", "routerId": identifier, "note": "=1+1"})
    assert ctx.sync.flush()["blocked"] == 1


def test_return_clears_current_holder_but_retains_history(ctx):
    identifier = add(ctx)
    issue(ctx, identifier)
    ctx.sync.flush()
    receive(ctx, identifier)
    ctx.sync.flush()
    assert ctx.a.sheet["rows"][0]["values"]["owner"] == ""
    assert ctx.i.loans(1, identifier)[0]["recipient"]["name"] == "Тестовый получатель"


def test_first_connection_local_inventory_requires_explicit_choice(ctx):
    identifier = add(ctx)
    put(ctx, legacy("TEST-01", note="Из исходной таблицы"), 2)
    preview = ctx.sync.preview(1)
    assert preview["entries"][0]["type"] == "conflict"
    assert not preview["entries"][0].get("identity")
    ctx.sync.apply(
        1,
        preview["id"],
        {identifier: {"source": "sheet", "reason": "Проверен тот же физический роутер"}},
    )
    assert len(ctx.i.list(1)) == 1
    assert ctx.i.get(1, identifier)["note"] == "Из исходной таблицы"
