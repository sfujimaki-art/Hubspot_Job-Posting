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


# ---------------------------------------------------------------- レビュー後に追加
def test_HRの追加項目つき重複行でも担当者は空のときだけ埋める(hr_row):
    row = dataclasses.replace(hr_row, extra={"seibetsu": "女"})
    key = ("HRハッカー", row.apply_date, "email", ai.normalize_email(row.email))
    client = _hr_client(existing_appts={key: "APPT-1"})
    client.existing_appt_props = {"APPT-1": {"hubspot_owner_id": "O-OTHER"}}
    ai.process_applicant(row, client)
    assert all("hubspot_owner_id" not in u["properties"]
               for u in getattr(client, "updated_appts", []))
    client2 = _hr_client(existing_appts={key: "APPT-1"})
    client2.existing_appt_props = {"APPT-1": {}}
    ai.process_applicant(row, client2)
    assert client2.updated_appts[0]["properties"]["hubspot_owner_id"] == "O-CONSULT"


def test_リクロジ応募は従来どおり求人のOwner():
    client = ai.DryRunClient()
    client.listing_owners = {"L-3": "O-DEAL-OWNER"}
    client.listing_consultants = {"L-3": "O-CONSULT"}
    assert ai.applicant_owner_id(client, "L-3", ai.RECLOGI_MEDIA) == "O-DEAL-OWNER"


def test_生きている取引が無い群は契約期間が最も新しい取引のコンサル担当():
    d = {"D1": {"dealstage": ENDED, "contract_start_date": "2025-01-01", "consultant": "O-OLD"},
         "D2": {"dealstage": ENDED, "contract_start_date": "2026-04-01", "consultant": "O-NEW"}}
    pl, why = so.plan_for_appt({"hubspot_owner_id": "O-OLD"}, ["D1", "D2"], d, ACTIVE)
    assert (pl.get("hubspot_owner_id"), why) == ("O-NEW", "change")


def _fake_hubspot(monkeypatch, a2l, l2d, deals, listings, pipeline):
    """deal_groups が読む HubSpot を差し替える (通信しない)。"""
    def assoc(frm, to, ids):
        src = a2l if frm == "0-421" else l2d
        return {i: src[i] for i in ids if i in src}

    def read(obj, ids, props):
        src = deals if obj == "0-3" else listings
        return {i: src[i] for i in ids if i in src}
    monkeypatch.setattr(ba, "_batch_assoc", assoc)
    monkeypatch.setattr(ba, "_batch_read", read)
    monkeypatch.setattr(ba, "search_all_by_id", lambda *a, **k: [
        {"id": i, "properties": p} for i, p in pipeline.items()])


def _d(code, stage=LIVE, start="2025-01-01", consultant="", shops=""):
    return {"code_of_customer": code, "dealstage": stage, "contract_start_date": start,
            "consultant": consultant, "pipeline": ai.DELIVERY_PIPELINE,
            "hrhacker_shop_ids": shops}


def test_deal_groups_取引群の決め方(monkeypatch):
    pipeline = {"P1": _d("C1", consultant="O-NEW", shops="S1"),
                "P2": _d("C2", consultant="O-X", shops="S2"),
                "P3": _d("C3", consultant="O-Y", shops="S9"),
                "P4": _d("C4", consultant="O-Z", shops="S9")}
    deals = {"D1": _d("C1", stage=ENDED, consultant="O-OLD", shops="S1"),
             "D2": _d("C2", shops="S2"), "D3": _d("C3", shops="S3")}
    a2l = {"A-code": ["L1"], "A-owner": ["L2"], "A-shared": ["L3"],
           "A-span": ["L4", "L5"], "A-none": [], "A-nodeal": ["L6"]}
    l2d = {"L1": ["D1"], "L2": ["D3"], "L4": ["D1"], "L5": ["D2"]}
    listings = {"L1": {"id_shop_hrhakkaa": "S1"}, "L2": {"hr_owner_code": "C2"},
                "L3": {"id_shop_hrhakkaa": "S9"}, "L4": {}, "L5": {}, "L6": {}}
    _fake_hubspot(monkeypatch, a2l, l2d, deals, listings, pipeline)
    groups, got, stat = ba.deal_groups(list(a2l))
    # 直接の取引 (終了) + 同じ取引先コードの納品管理の取引 → 今の契約は生きている P1
    assert sorted(groups["A-code"]) == ["D1", "P1"]
    assert ai.consultant_owner_id(groups["A-code"], got) == "O-NEW"
    # 持ち主コードが判定済み → 直接の取引 (C3) ではなく持ち主コード C2 の取引群
    assert groups["A-owner"] == ["P2"]
    assert ai.consultant_owner_id(groups["A-owner"], got) == "O-X"
    # 店舗IDを別会社と共有・別会社の取引にまたがる → 決めない
    assert groups["A-shared"] == [] and groups["A-span"] == []
    assert groups["A-none"] == [] and groups["A-nodeal"] == []
    assert (stat["shared_shop"], stat["no_listing"], stat["no_deal"]) == (2, 1, 1)


def test_担当者一覧は全ページ読み一時的な失敗はやり直す(monkeypatch):
    pages = [
        (503, {}),
        (200, {"results": [{"id": "1", "lastName": "見本", "firstName": "太郎"}],
               "paging": {"next": {"after": "x"}}}),
        (200, {"results": [{"id": "2", "lastName": "", "firstName": ""}]}),
    ]
    urls: list = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    def get(url, **k):
        urls.append(url)
        return R(*pages.pop(0))
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "dummy")
    monkeypatch.setattr(so.requests, "get", get)
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    assert so.active_owners() == {"1": "見本太郎", "2": None}
    assert all("archived=false" in u for u in urls)
    assert urls[-1].endswith("&after=x")


def test_一部しか返らなかった更新は失敗に数える(monkeypatch):
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    monkeypatch.setattr(so, "post_retry", lambda url, body: {"results": [{"id": "1"}]})
    assert so._update([("1", {"x": "y"}), ("2", {"x": "y"})]) == (1, 1)


def test_失敗のログは状態コードだけで本文を出さない():
    assert so._why(RuntimeError("HTTP 400: 氏名入りの本文")) == "HTTP 400"
    assert so._why(ValueError("x")) == "ValueError"


# ---------------------------------------------------------------- 再紐付け・空欄埋め
class _Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code = body, code

    def json(self):
        return self._b


def _patch_client(monkeypatch, consultant="O-CONSULT"):
    C = ai.RealHubSpotClient
    monkeypatch.setattr(C, "get_listing_consultant", lambda self, l, m="", lo="": consultant)
    monkeypatch.setattr(C, "owner_is_active", lambda self, o: True)
    monkeypatch.setattr(C, "get_listing_owner", lambda self, l: "O-LISTING")
    monkeypatch.setattr(C, "get_oubosaki_props", lambda self, *a: {})
    monkeypatch.setattr(C, "copy_listing_note", lambda self, *a: None)
    monkeypatch.setattr(C, "associate_appointment_to_listing", lambda self, *a: None)


@pytest.mark.parametrize("media,expected", [("HRハッカー", "O-CONSULT"),
                                             ("AirWork", "O-LISTING")])
def test_再紐付けでもHRはコンサル担当_他は求人のOwner(monkeypatch, media, expected):
    from scripts.job_application_sync import applicant_sync as asy
    _patch_client(monkeypatch)
    prop = "id_hrhakkaa" if media == "HRハッカー" else "id_airwork"

    def post(url, **k):
        if "/0-421/search" in url:
            return _Resp({"results": [{"id": "A1", "properties": {
                "oubokyuujinmemo": "J1", "oubobaitaimei": media,
                "yingmuri": "2026-01-01"}}]})
        return _Resp({"results": [{"id": "L1", "properties": {
            prop: "J1", "hubspot_owner_id": "O-LISTING"}}]})
    patched: list = []
    monkeypatch.setattr(asy.requests, "post", post)
    monkeypatch.setattr(asy.requests, "patch",
                        lambda url, **k: patched.append(k["json"]["properties"]))
    monkeypatch.setattr(asy, "slack_notify", lambda *a, **k: True)
    monkeypatch.setattr(asy.time, "sleep", lambda s: None)
    assert asy.relink(dry_run=False)["relinked"] == 1
    assert patched[0]["hubspot_owner_id"] == expected


@pytest.mark.parametrize("media,expected", [("HRハッカー", "O-CONSULT"),
                                             ("AirWork", "O-LISTING")])
def test_空欄埋めでもHRはコンサル担当_他は求人のOwner(monkeypatch, media, expected):
    from scripts.job_application_sync import applicant_sync as asy
    _patch_client(monkeypatch)

    def post(url, **k):
        if "/0-421/search" in url:
            return _Resp({"results": [{"id": "A1", "properties": {
                "oubobaitaimei": media, "yingmuri": "2026-01-01"}}]})
        if "/associations/" in url:
            return _Resp({"results": [{"from": {"id": "A1"},
                                       "to": [{"toObjectId": "L1"}]}]})
        return _Resp({"results": [{"id": "L1", "properties": {
            "hubspot_owner_id": "O-LISTING"}}]})
    patched: list = []
    monkeypatch.setattr(asy.requests, "post", post)
    monkeypatch.setattr(asy.requests, "patch",
                        lambda url, **k: patched.append(k["json"]["properties"]))
    out = asy.propagate_from_listing(dry_run=False, days=30, limit=10)
    assert out["filled_owner"] == 1
    assert patched == [{"hubspot_owner_id": expected}]
