"""リクロジは専用の求人 (id_reclogi) を持つ (2026-10-20 ユーザー決定)。

- 求人はページから作り、その場で取引に付ける: HR の同じ求人の取引を写す /
  無ければ店舗ID → 取引 (sync_deal_association と同じ判定)
- 410 (掲載終了) なら作らない
- 既に HR の求人に紐付いた応募者を付け替えるスクリプトの計画と更新値
値・ID はすべてダミー (公開リポジトリ)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import deal_stages as DS
from scripts.job_application_sync import migrate_reclogi_listing_20261020 as M
from scripts.job_application_sync import reclogi_applicants as rl
from scripts.job_application_sync.tests.test_reclogi_applicants_20261020 import (
    FakeClient, FakeWs, _row, _values)

FIXTURE = Path(__file__).parent / "fixtures" / "reclogi_job_page.html"
PAGE = FIXTURE.read_text(encoding="utf-8")
URL = "https://reclogi.jp/job-data/10756"
LIVE = sorted(DS.WRITABLE_STAGES)[0]
ENDED = "52016159"           # 解約済(成果不足)


def _deal(code, stage=LIVE, start="2026-01-01", shops="", owner="", name="サンプル取引"):
    return {"dealname": name, "dealstage": stage, "contract_start_date": start,
            DS.PROP_CODE: code, "hrhacker_shop_ids": shops, "hubspot_owner_id": owner}


# ---------------------------------------------------------------- 取引の決め方
def test_HRの求人があればその取引を全部写し_担当者などの値も写す():
    cli = FakeClient(listings_hr={"11694710": "L-HR"},
                     listing_deals={"L-HR": ["D1", "D2"]},
                     listing_props={"L-HR": {"hubspot_owner_id": "O-1",
                                             "ichijitaiounoumu_deforuto": "必要",
                                             "customer_sheet_url": "https://example.com/s"}})
    plan = rl.plan_listing_deals(cli, "11694710", "1154643")
    assert plan["deals"] == ["D1", "D2"] and plan["hr_listing"] == "L-HR"
    assert plan["copy"] == {"hubspot_owner_id": "O-1", "ichijitaiounoumu_deforuto": "必要",
                            "customer_sheet_url": "https://example.com/s"}


def test_HRの求人が無ければ店舗IDで取引を探し_同じ取引先コードの生きた取引に付ける():
    shop = {"D1": _deal("C-1", shops="1154643;999", owner="O-1", start="2026-04-01"),
            "D9": _deal("C-1", stage=ENDED, shops="1154643", start="2025-01-01")}
    group = dict(shop, D2=_deal("C-1", start="2025-06-01"))
    cli = FakeClient(shop_deals={"1154643": shop}, code_deals={"C-1": group})
    plan = rl.plan_listing_deals(cli, "11694710", "1154643")
    assert sorted(plan["deals"]) == ["D1", "D2"]          # 解約済は付けない
    assert plan["how"].startswith("店舗ID")
    assert plan["copy"] == {"hubspot_owner_id": "O-1"}    # 最新の生きた取引の担当者


def test_店舗IDが別会社と共有なら付けない():
    shop = {"D1": _deal("C-1", shops="1154643"), "D2": _deal("C-2", shops="1154643")}
    cli = FakeClient(shop_deals={"1154643": shop},
                     code_deals={"C-1": {"D1": shop["D1"]}, "C-2": {"D2": shop["D2"]}})
    plan = rl.plan_listing_deals(cli, "11694710", "1154643")
    assert plan["deals"] == [] and "またがる" in plan["how"]


def test_店舗IDが部分一致の取引は候補にしない_担当者が無効なら写さない():
    shop = {"D1": _deal("C-1", shops="11546430", owner="O-1")}     # 別の店舗ID
    cli = FakeClient(shop_deals={"1154643": shop})
    assert rl.plan_listing_deals(cli, "", "1154643")["deals"] == []
    shop = {"D1": _deal("C-1", shops="1154643", owner="O-OFF")}
    cli = FakeClient(shop_deals={"1154643": shop}, code_deals={"C-1": shop}, inactive={"O-OFF"})
    plan = rl.plan_listing_deals(cli, "", "1154643")
    assert plan["deals"] == ["D1"] and plan["copy"] == {}


def test_HRの求人も店舗IDの取引も無ければ付けない():
    plan = rl.plan_listing_deals(FakeClient(), "11694710", "1154643")
    assert plan["deals"] == [] and "見つからない" in plan["how"]


# ---------------------------------------------------------------- 求人を用意する
def test_無ければページから作り_取引に付ける():
    cli = FakeClient(listings_hr={"11694710": "L-HR"}, listing_deals={"L-HR": ["D1"]},
                     listing_props={"L-HR": {"hubspot_owner_id": "O-1"}})
    r = rl.prepare_listing(cli, "10756", URL, "11694710", "1154643", dry_run=False,
                           fetch_page=lambda u: PAGE, make_listing=rl.create_listing)
    assert r["action"] == "作成" and r["listing_id"] == "L-NEW1"
    made = cli.created_listings[0]
    assert made["id_reclogi"] == "10756" and made["hubspot_owner_id"] == "O-1"
    assert "id_hrhakkaa" not in made and "id_shop_hrhakkaa" not in made
    assert cli.deal_links == [("L-NEW1", ["D1"])] and r["linked_deals"] == 1


def test_既にあれば作らない():
    cli = FakeClient(listings_reclogi={"10756": "L-RL"})
    r = rl.prepare_listing(cli, "10756", URL, dry_run=False,
                           fetch_page=lambda u: pytest.fail("ページは読まない"))
    assert r["action"] == "既存" and r["listing_id"] == "L-RL" and cli.created_listings == []


def test_掲載終了_410_なら作らない():
    cli = FakeClient()
    r = rl.prepare_listing(cli, "10756", URL, dry_run=False, fetch_page=lambda u: None)
    assert r["action"] == "ページなし" and cli.created_listings == [] and cli.deal_links == []


def test_ページのリクロジ求人IDが違えば作らない():
    cli = FakeClient()
    r = rl.prepare_listing(cli, "99999", URL, dry_run=False, fetch_page=lambda u: PAGE)
    assert r["action"] == "食い違い" and cli.created_listings == []


def test_dry_runでは作らず予定だけ出す():
    cli = FakeClient(listings_hr={"11694710": "L-HR"}, listing_deals={"L-HR": ["D1"]})
    r = rl.prepare_listing(cli, "10756", URL, dry_run=True, fetch_page=lambda u: PAGE)
    assert r["action"] == "作成予定" and r["deals"] == ["D1"]
    assert cli.created_listings == [] and cli.deal_links == []


def test_取込でもリクロジの求人を作って応募者を紐付ける(tmp_path):
    from scripts.job_application_sync import applicant_sync as S
    ws = FakeWs(_values([_row()]))
    cli = FakeClient(listings_hr={"11694710": "L-HR"}, listing_deals={"L-HR": ["D1"]})
    s = rl.run(dry_run=False, ledger=S.Ledger(tmp_path / "l.json"), ws=ws, client=cli,
               fetch_page=lambda u: PAGE)
    assert s["listing_created"] == 1 and s["linked"] == 1
    assert cli.associations[0]["listing_id"] == "L-NEW1"     # HR の求人 (L-HR) ではない
    assert cli.deal_links == [("L-NEW1", ["D1"])]


# ---------------------------------------------------------------- oubosaki の URL
def test_応募先求人URLはリクロジの求人のURL(monkeypatch):
    cli = ai.RealHubSpotClient("dummy")

    class R:
        def json(self):
            return {"properties": {"hs_name": "大型ドライバー", "url_reclogi": URL}}
    monkeypatch.setattr(cli._requests, "get", lambda url, **k: R())
    monkeypatch.setattr(cli, "_listing_deal_group", lambda *a: ([], {}))
    p = cli.get_oubosaki_props("L-RL", ai.RECLOGI_MEDIA, "", "10756")
    assert p["oubosaki_kyuujin_url"] == URL and p["oubosaki_kyuujin_title"] == "大型ドライバー"


# ---------------------------------------------------------------- 付け替えスクリプト
class MigClient(FakeClient):
    def __init__(self, appt_listings=None, **kw):
        super().__init__(**kw)
        self.appt_listings = appt_listings or {}
        self.removed: list = []

    def get_appointment_listings(self, appointment_id):
        return list(self.appt_listings.get(appointment_id, []))

    def remove_appointment_listing(self, appointment_id, listing_id):
        self.removed.append((appointment_id, listing_id))

    def get_oubosaki_props(self, listing_id, media, login_id, media_job_id):
        return {"oubosaki_kyuujin_url": URL, "oubosaki_torihiki_name": "サンプル取引",
                "ichijitaiounoumu": "必要", "anmokuchi_ashigirijouken": "ダミー条件",
                "anmokuchi_tenki_nichiji": "1"}


def _recs(*rows):
    _, recs = rl.records_from_values(_values(list(rows)))
    return {r.get("HubSpot応募者ID"): r for r in recs}


def test_付け替えのdry_run計画():
    recs = _recs(_row(messageId="m1", HubSpot登録="受入済", HubSpot応募者ID="A1"),
                 _row(messageId="m2", HubSpot登録="受入済", HubSpot応募者ID="A2",
                      応募求人ID="20000", 掲載求人URL="https://reclogi.jp/job-data/20000"),
                 _row(messageId="m3", HubSpot登録="受入済", HubSpot応募者ID="A3"))
    cli = MigClient(appt_listings={"A1": ["L-HR"], "A2": ["L-HR2"], "A3": ["L-HR"],
                                   "A4": ["L-HR"]},
                    listings_hr={"11694710": "L-HR"}, listing_deals={"L-HR": ["D1"]})
    pages = {URL: PAGE}
    rows = M.build_plan([{"id": "A1"}, {"id": "A2"}, {"id": "A3"}, {"id": "A4"}], recs, cli,
                        fetch_page=pages.get)
    by = {r["appointment_id"]: r for r in rows}
    assert by["A1"]["action"] == "作成予定" and by["A1"]["deals"] == ["D1"]
    assert by["A1"]["current_listings"] == ["L-HR"] and by["A1"]["reclogi_id"] == "10756"
    assert by["A2"]["action"] == "ページなし"                 # 410 は作らない
    assert by["A3"]["action"] == "作成予定"
    assert by["A4"]["action"].startswith("タブに行が無い")
    assert cli.created_listings == [] and cli.deal_links == [] and cli.removed == []


def test_付け替えの実行は求人を1回だけ作り_紐付けを差し替える(tmp_path):
    recs = _recs(_row(messageId="m1", HubSpot登録="受入済", HubSpot応募者ID="A1"),
                 _row(messageId="m3", HubSpot登録="受入済", HubSpot応募者ID="A3"))
    cli = MigClient(appt_listings={"A1": ["L-HR"], "A3": ["L-HR"]},
                    listings_hr={"11694710": "L-HR"}, listing_deals={"L-HR": ["D1"]})
    appts = [{"id": "A1", "properties": {"oubokyuujinmemo": "11694710"}},
             {"id": "A3", "properties": {"anmokuchi_tenki_nichiji": "123",
                                         "ichijitaiounoumu": "不要",
                                         "hubspot_owner_id": "O-HUMAN"}}]
    rows = M.build_plan(appts, recs, cli, fetch_page=lambda u: PAGE)
    path = M.backup(appts, rows, tmp_path)
    assert path.exists() and '"L-HR"' in path.read_text(encoding="utf-8")
    stat = M.apply(appts, rows, recs, cli, fetch_page=lambda u: PAGE,
                   make_listing=rl.create_listing)
    assert stat["listing_created"] == 1 and len(cli.created_listings) == 1
    assert sorted(cli.removed) == [("A1", "L-HR"), ("A3", "L-HR")]
    assert [a["listing_id"] for a in cli.associations] == ["L-NEW1", "L-NEW1"]
    upd = {u["id"]: u["properties"] for u in cli.updated_appts}
    assert upd["A1"]["oubokyuujinmemo"] == "10756"
    assert upd["A1"]["anmokuchi_ashigirijouken"] == "ダミー条件"     # 印が無いので写す
    # 転記の印・担当者・要否がある応募者は守る
    assert "anmokuchi_ashigirijouken" not in upd["A3"]
    assert "hubspot_owner_id" not in upd["A3"] and "ichijitaiounoumu" not in upd["A3"]


def test_更新値の計画_今と同じ値は書かず_対象外は未転記に戻す():
    cur = {"oubokyuujinmemo": "10756", "yingmujinglu": ai.RECLOGI_YINGMUJINGLU,
           "kokyakushiitotenkijoukyou": "対象外", "yingmuri": "2026-01-01",
           "honnin_kibou_kinyuuran": "人が直した"}
    p = M.plan_update_props(cur, {"ichijitaiounoumu": "必要", "oubosaki_shokushu": "ドライバー"},
                            {"honnin_kibou_kinyuuran": "元の値", "moyori_eki": "見本駅"},
                            "10756", owner="O-1")
    assert p == {"oubosaki_shokushu": "ドライバー", "moyori_eki": "見本駅",
                 "hubspot_owner_id": "O-1", "kokyakushiitotenkijoukyou": "未転記"}
    # (古い応募なので一次対応の要否は付けない / 人が直した本人希望は守る)
