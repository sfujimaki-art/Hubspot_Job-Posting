"""HRハッカー応募の担当者 (hubspot_owner_id) を取引の「コンサル担当」にする (2026-11-12)。

- 作成時: applicant_import.applicant_owner_id (求人 → 取引群の今の契約のコンサル担当)
- 毎晩: sync_appointment_owner (作成後に取引の担当が変わった応募を合わせる)
値・氏名はすべてダミー (公開リポジトリ)。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import backfill_appointment_anmokuchi as ba
from scripts.job_application_sync import deal_stages as DS
from scripts.job_application_sync import sync_appointment_owner as so

LIVE = sorted(DS.WRITABLE_STAGES)[0]
ENDED = "52016159"
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "sample_applicants.csv"


@pytest.fixture
def rows():
    return ai.load_applicants_csv(FIXTURE_CSV)


@pytest.fixture
def hr_row(rows):
    return next(r for r in rows if r.media == "HRハッカー" and r.media_job_id == "HR-1001")


@pytest.fixture
def aw_row(rows):
    return next(r for r in rows if r.media == "AirWork" and r.media_job_id == "AW-2002"
                and r.airwork_login_id == "login_user_a" and "鈴木" in r.name)


def _hr_client(**kw):
    c = ai.DryRunClient(listings_hr={"HR-1001": "L-1"}, **kw)
    c.listing_owners = {"L-1": "O-DEAL-OWNER"}        # 求人の Owner (取引担当者)
    c.listing_consultants = {"L-1": "O-CONSULT"}      # 取引のコンサル担当
    return c


# ---------------------------------------------------------------- 作成時
def test_HR応募は取引のコンサル担当が担当者になる(hr_row):
    client = _hr_client()
    res = ai.process_applicant(hr_row, client)
    assert res.status == "linked"
    assert client.created_appts[0]["properties"]["hubspot_owner_id"] == "O-CONSULT"


def test_コンサル担当が無効なユーザーなら求人のOwnerに戻す(hr_row):
    client = _hr_client()
    client.inactive_owners = {"O-CONSULT"}
    ai.process_applicant(hr_row, client)
    assert client.created_appts[0]["properties"]["hubspot_owner_id"] == "O-DEAL-OWNER"


def test_コンサル担当が決まらなければ求人のOwnerに戻す(hr_row):
    client = _hr_client()
    client.listing_consultants = {}
    ai.process_applicant(hr_row, client)
    assert client.created_appts[0]["properties"]["hubspot_owner_id"] == "O-DEAL-OWNER"


def test_求人が見つからなければ担当者を入れない(hr_row):
    client = ai.DryRunClient()
    client.listing_consultants = {"L-1": "O-CONSULT"}
    res = ai.process_applicant(hr_row, client)
    assert res.status == "unlinked"
    assert "hubspot_owner_id" not in client.created_appts[0]["properties"]


def test_AirWork応募は従来どおり求人のOwner(aw_row):
    client = ai.DryRunClient(listings_aw={("AW-2002", "login_user_a"): "L-2"})
    client.listing_owners = {"L-2": "O-DEAL-OWNER"}
    client.listing_consultants = {"L-2": "O-CONSULT"}
    ai.process_applicant(aw_row, client)
    assert client.created_appts[0]["properties"]["hubspot_owner_id"] == "O-DEAL-OWNER"


def test_重複応募の担当者が空ならコンサル担当で埋める(hr_row):
    row = dataclasses.replace(hr_row, extra={})
    client = _hr_client(existing_appts={
        ("HRハッカー", row.apply_date, "email", ai.normalize_email(row.email)): "APPT-1"})
    res = ai.process_applicant(row, client)
    assert res.status == "skip_duplicate"
    assert client.updated_appts[0]["properties"]["hubspot_owner_id"] == "O-CONSULT"


def test_重複応募の担当者が入っていれば作成時の経路では触らない(hr_row):
    row = dataclasses.replace(hr_row, extra={})
    client = _hr_client(existing_appts={
        ("HRハッカー", row.apply_date, "email", ai.normalize_email(row.email)): "APPT-1"})
    client.existing_appt_props = {"APPT-1": {"hubspot_owner_id": "O-OTHER"}}
    ai.process_applicant(row, client)
    ups = getattr(client, "updated_appts", [])
    assert all("hubspot_owner_id" not in u["properties"] for u in ups)


def test_applicant_owner_idは本番クライアントの取引群から選ぶ(monkeypatch):
    """RealHubSpotClient.get_listing_consultant (今の契約 = latest_live) を通る。"""
    cli = ai.RealHubSpotClient("dummy")
    deals = {
        "D1": {"dealstage": LIVE, "contract_start_date": "2025-01-01",
               "consultant": "O-LIVE", "hubspot_owner_id": "O-SALES"},
        "D2": {"dealstage": ENDED, "contract_start_date": "2026-04-01",
               "consultant": "O-ENDED", "hubspot_owner_id": "O-SALES2"},
    }

    class _R:
        status_code = 200

        def json(self):
            return {"properties": {}}
    monkeypatch.setattr(cli._requests, "get", lambda url, **k: _R())
    monkeypatch.setattr(cli, "_shop_is_shared", lambda shop: False)
    monkeypatch.setattr(cli, "_resolve_deal_group", lambda l, m, lo: (list(deals), deals))
    monkeypatch.setattr(cli, "owner_is_active", lambda oid: True)
    monkeypatch.setattr(cli, "get_listing_owner", lambda lid: "O-LISTING")
    assert ai.applicant_owner_id(cli, "L-1", "HRハッカー") == "O-LIVE"
    assert ai.applicant_owner_id(cli, "L-1", "AirWork") == "O-LISTING"
    assert ai.applicant_owner_id(cli, None, "HRハッカー") is None


# ---------------------------------------------------------------- 毎晩
def _deals():
    return {
        "D1": {"dealstage": LIVE, "contract_start_date": "2025-01-01",
               "consultant": "O-NEW", "hubspot_owner_id": "O-SALES"},
        "D2": {"dealstage": ENDED, "contract_start_date": "2026-04-01",
               "consultant": "O-OLD"},
    }


ACTIVE = {"O-NEW": "見本太郎", "O-OLD": "例示花子", "O-NONAME": None}


def test_担当が変わった応募は担当者と担当コンサルタントを書き換える():
    pl, why = so.plan_for_appt(
        {"hubspot_owner_id": "O-OLD", "dandangkonsarutanto": "例示花子"},
        ["D1", "D2"], _deals(), ACTIVE)
    assert why == "change"
    assert pl == {"hubspot_owner_id": "O-NEW", "dandangkonsarutanto": "見本太郎"}


def test_合っている応募は書かない():
    pl, why = so.plan_for_appt(
        {"hubspot_owner_id": "O-NEW", "dandangkonsarutanto": "見本太郎"},
        ["D1", "D2"], _deals(), ACTIVE)
    assert (pl, why) == ({}, "same")


def test_担当者だけ違う応募は担当者だけ書く():
    pl, _ = so.plan_for_appt(
        {"hubspot_owner_id": "O-SALES", "dandangkonsarutanto": "見本太郎"},
        ["D1"], _deals(), ACTIVE)
    assert pl == {"hubspot_owner_id": "O-NEW"}


def test_コンサル担当が決まらない応募は触らない():
    assert so.plan_for_appt({"hubspot_owner_id": "O-OLD"}, [], {}, ACTIVE) == (
        {}, "no_consultant")
    d = {"D1": {"dealstage": LIVE, "contract_start_date": "2025-01-01", "consultant": ""}}
    assert so.plan_for_appt({"hubspot_owner_id": "O-OLD"}, ["D1"], d, ACTIVE) == (
        {}, "no_consultant")


def test_コンサル担当が無効なユーザーなら触らない():
    assert so.plan_for_appt({"hubspot_owner_id": "O-OLD"}, ["D1"], _deals(),
                            {"O-OLD": "例示花子"}) == ({}, "inactive")


def test_姓名が取れないユーザーは担当コンサルタントを空で潰さない():
    d = {"D1": {"dealstage": LIVE, "contract_start_date": "2025-01-01",
                "consultant": "O-NONAME"}}
    pl, _ = so.plan_for_appt({"hubspot_owner_id": "O-OLD", "dandangkonsarutanto": "例示花子"},
                             ["D1"], d, ACTIVE)
    assert pl == {"hubspot_owner_id": "O-NONAME"}


def test_対象はHRハッカーの応募だけで窓の中だけ(monkeypatch):
    seen: dict = {}

    def search(obj, props, filters, **k):
        seen["obj"], seen["filters"], seen["props"] = obj, filters, props
        return [{"id": "1", "properties": {"hubspot_owner_id": "O-OLD"}}]
    monkeypatch.setattr(so, "search_all_by_id", search)
    monkeypatch.setattr(so.BA, "deal_groups",
                        lambda ids: ({"1": ["D1"]}, _deals(), ba.Counter(no_deal=0)))
    apps, groups, deals, stat = so.collect("2026-08-14T00:00:00Z")
    assert seen["obj"] == "0-421"
    assert {"propertyName": "oubobaitaimei", "operator": "EQ",
            "value": "HRハッカー"} in seen["filters"]
    assert {"propertyName": "hs_createdate", "operator": "GTE",
            "value": "2026-08-14T00:00:00Z"} in seen["filters"]
    assert set(seen["props"]) >= {"hubspot_owner_id", "dandangkonsarutanto"}
    assert list(apps) == ["1"] and groups == {"1": ["D1"]} and stat["window"] == 1


def test_既定の窓は直近90日():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
    assert so.window_start(now) == "2026-08-14T03:00:00Z"


def test_ドライランは書き込まない(monkeypatch):
    monkeypatch.setattr(so, "collect", lambda since: (
        {"1": {"hubspot_owner_id": "O-OLD"}}, {"1": ["D1"]}, _deals(), ba.Counter()))
    monkeypatch.setattr(so, "active_owners", lambda: ACTIVE)
    called = []
    monkeypatch.setattr(so, "post_retry", lambda *a, **k: called.append(a))
    assert so.main([]) == 0
    assert called == []


def test_本番は100件ずつ書き_失敗したバッチは1件ずつやり直す(monkeypatch):
    apps = {str(i): {"hubspot_owner_id": "O-OLD"} for i in range(150)}
    monkeypatch.setattr(so, "collect", lambda since: (
        apps, {a: ["D1"] for a in apps}, _deals(), ba.Counter()))
    monkeypatch.setattr(so, "active_owners", lambda: ACTIVE)
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    calls: list = []
    bodies: list = []

    def post(url, body):
        assert url.endswith("/crm/v3/objects/0-421/batch/update")
        ids = [x["id"] for x in body["inputs"]]
        calls.append(ids)
        bodies.extend(body["inputs"])
        if len(ids) == 100:
            raise RuntimeError("HTTP 400")
        if ids == ["7"]:
            raise RuntimeError("HTTP 400")
        return {"results": [{"id": i} for i in ids]}
    monkeypatch.setattr(so, "post_retry", post)
    assert so.main(["--actual"]) == 1                       # 1件失敗したので非0
    assert len(calls[0]) == 100                             # まず100件
    assert sum(1 for c in calls if len(c) == 1) == 100      # 失敗したバッチは1件ずつ
    assert calls[-1] == [str(i) for i in range(100, 150)]   # 残り50件
    assert all(x["properties"] == {"hubspot_owner_id": "O-NEW",
                                   "dandangkonsarutanto": "見本太郎"} for x in bodies)


def test_取引群の決め方は一次対応8項目の転記と共用している():
    import inspect
    src = inspect.getsource(so.collect)
    assert "BA.deal_groups" in src
    assert "deal_groups(list(need))" in inspect.getsource(ba.collect)
