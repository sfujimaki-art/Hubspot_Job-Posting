# -*- coding: utf-8 -*-
"""至急の取得の直後に、入った求人を取引に紐付け、担当者・一次対応をそろえる (2026-10-27)。"""
from scripts.job_application_sync.fetchers import aw_orchestrator as orch
from scripts.job_application_sync import sync_deal_association as sda
from scripts.job_application_sync import sync_ichijitaiou as si
from scripts.job_application_sync import hs_paging


def test_ok_job_ids_only_for_ok_companies():
    res = [{"login_id": "a", "status": "ok"}, {"login_id": "b", "status": "error"},
           {"login_id": "c", "status": "ok"}]
    got = orch._urgent_ok_job_ids(res, {"a": ["1", "1", "2"], "b": ["3"], "c": []})
    assert got == {"a": ["1", "2"]}


def test_deal_link_runs_both_on_found_listings(monkeypatch):
    calls = {}

    def fake_search(obj, props, filters, **kw):
        calls.setdefault("search", []).append((obj, filters))
        assert "hubspot_owner_id" in props and "ichijitaiounoumu_deforuto" in props
        return [{"id": "L1", "properties": {}}]

    monkeypatch.setattr(hs_paging, "search_all_by_id", fake_search)
    monkeypatch.setattr(sda, "run", lambda dry_run, listings: calls.setdefault(
        "sda", (dry_run, [o["id"] for o in listings])) and {"associations": 1, "owner_set": 1})
    monkeypatch.setattr(si, "run", lambda dry_run, listings: calls.setdefault(
        "si", (dry_run, [o["id"] for o in listings])) and {"applied": 1})
    out = orch._deal_link_urgent([{"login_id": "a", "status": "ok"}], {"a": ["J1", "J2"]})
    assert out["listings"] == 1
    assert calls["sda"] == (False, ["L1"]) and calls["si"] == (False, ["L1"])
    obj, filters = calls["search"][0]
    assert obj == "0-420"
    assert {"propertyName": "id_airwork", "operator": "IN", "values": ["J1", "J2"]} in filters
    assert {"propertyName": "airwork_account_login_id", "operator": "EQ", "value": "a"} in filters


def test_deal_link_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(hs_paging, "search_all_by_id", boom)
    assert orch._deal_link_urgent([{"login_id": "a", "status": "ok"}], {"a": ["J1"]}) == {}


def test_nothing_when_no_ok(monkeypatch):
    monkeypatch.setattr(hs_paging, "search_all_by_id",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert orch._deal_link_urgent([{"login_id": "a", "status": "error"}], {"a": ["J1"]}) == {}


def test_sda_targeted_skips_full_list_and_review_tab(monkeypatch):
    monkeypatch.setattr(sda, "list_all", lambda *a, **k: (_ for _ in ()).throw(AssertionError("list_all")))
    monkeypatch.setattr(sda.plog, "replace_list", lambda *a, **k: (_ for _ in ()).throw(AssertionError("tab")))
    monkeypatch.setattr(sda, "_existing_deal_assoc", lambda lids: {"L1": ["D1"]})
    monkeypatch.setattr(sda, "load_deals", lambda: {})
    monkeypatch.setattr(sda, "build_login_to_mail", lambda: {})
    monkeypatch.setattr(sda, "load_active_owners", lambda: {"1"})
    out = sda.run(dry_run=True, listings=[{"id": "L1", "properties": {}}])
    assert out["listings"] == 1 and out["already_linked"] == 1
    assert sda.run(dry_run=True, listings=[])["listings"] == 0


def test_si_targeted_skips_full_list(monkeypatch):
    monkeypatch.setattr(si, "list_all", lambda *a, **k: (_ for _ in ()).throw(AssertionError("list_all")))
    assert si.run(dry_run=True, listings=[])["listings"] == 0
