"""求人の持ち主 (通知先メール×店舗ID×取引先コード) の判定と、各処理での優先 (2026-10-05)。

## 守る不変条件

1. 通知先メールが指すコードが1つなら持ち主 (店舗IDより優先)
2. 通知先メールが複数社 (同じ会社の別拠点が同じ別名) なら、店舗IDとの共通部分が1つのときだけ
3. 通知先メールが取引に無ければ、店舗IDが1社のときだけ
4. それ以外は決めない (別会社の値を入れない)
5. 判定結果は CSV に載っている求人だけ書き換える。変わらないものは書かない
6. 持ち主が判定済みの求人は、付け替え・紐付け・担当者・要否・応募転記のどれでも
   持ち主の取引群を使う。持ち主でない会社の取引群へは広げない
"""
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

import deal_master as DM  # noqa: E402
import resolve_listing_owner as R  # noqa: E402
from scripts.job_application_sync import relink_to_latest_deal as RL  # noqa: E402
from scripts.job_application_sync import sync_deal_association as SDA  # noqa: E402
from scripts.job_application_sync import sync_ichijitaiou as ICH  # noqa: E402

LIVE = "52016156"
ENDED = "66848546"


def d(stage, code, shops="", mails="", owner="", start="2026-04-01", flag=""):
    return {"dealstage": stage, "code_of_customer": code, "hrhacker_shop_ids": shops,
            "kanri_mail_address": mails, "hubspot_owner_id": owner,
            "contract_start_date": start, "createdate": start, "itijitaiou": flag,
            "dealname": ""}


# ---- 1〜4. 判定の規則 ---------------------------------------------------------

def _idx(deals):
    return DM.owner_indexes(deals)


def test_通知先メールが1社なら店舗IDが共有でも持ち主():
    deals = {"a": d(LIVE, "RL1", shops="S9", mails="rpo+a@x"),
             "b": d(LIVE, "RL2", shops="S9", mails="rpo+b@x")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("S9", ["rpo+b@x"], si, mi) == ("RL2", DM.BASIS_MAIL)


def test_通知先メールが複数社なら店舗IDとの共通部分が1つのときだけ():
    deals = {"a": d(LIVE, "RL1", shops="S1", mails="rpo+g@x"),
             "b": d(LIVE, "RL2", shops="S2", mails="rpo+g@x")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("S2", ["rpo+g@x"], si, mi) == ("RL2", DM.BASIS_MAIL_SHOP)
    # 店舗IDも両方が持っていれば決めない (同じ会社の別拠点)
    deals["a"]["hrhacker_shop_ids"] = "S2"
    si, mi = _idx(deals)
    assert DM.resolve_owner("S2", ["rpo+g@x"], si, mi) == (None, DM.BASIS_NG_SITES)


def test_通知先メールが取引に無ければ店舗IDが1社のときだけ():
    deals = {"a": d(LIVE, "RL1", shops="S1"), "b": d(LIVE, "RL2", shops="S9"),
             "c": d(LIVE, "RL3", shops="S9")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("S1", ["typo@x"], si, mi) == ("RL1", DM.BASIS_SHOP)
    assert DM.resolve_owner("S9", [], si, mi) == (None, DM.BASIS_NG_SHOP)
    assert DM.resolve_owner("", [], si, mi) == (None, DM.BASIS_NG_NONE)


def test_コード無しの取引は手がかりに使わない():
    deals = {"n": d(LIVE, "", shops="S1", mails="rpo+n@x")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("S1", ["rpo+n@x"], si, mi) == (None, DM.BASIS_NG_NONE)


def test_メールの大文字小文字を区別しない():
    deals = {"a": d(LIVE, "RL1", mails="RPO+A@X")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("", ["rpo+a@x"], si, mi)[0] == "RL1"


# ---- 5. 書き込み計画 ------------------------------------------------------------

def test_書き込みはCSVにある求人で値が変わるものだけ():
    deals = {"a": d(LIVE, "RL1", mails="rpo+a@x")}
    si, mi = _idx(deals)
    listings = [
        {"id": "L1", "properties": {"id_hrhakkaa": "J1"}},                                    # 新規
        {"id": "L2", "properties": {"id_hrhakkaa": "J2", DM.LISTING_OWNER: "RL1",
                                    DM.LISTING_OWNER_BASIS: DM.BASIS_MAIL}},                  # 変化なし
        {"id": "L3", "properties": {"id_hrhakkaa": "J9", DM.LISTING_OWNER: "RL7"}},           # CSVに無い
        {"id": "L4", "properties": {"id_hrhakkaa": "J4", DM.LISTING_OWNER: "RL1",
                                    DM.LISTING_OWNER_BASIS: DM.BASIS_MAIL}},                  # 判定不可へ
    ]
    csv_in = {"J1": ("", ["rpo+a@x"]), "J2": ("", ["rpo+a@x"]), "J4": ("", ["other@x"])}
    up, stat = R.plan(listings, csv_in, si, mi)
    assert up == [("L1", "RL1", DM.BASIS_MAIL), ("L4", "", DM.BASIS_NG_NONE)]
    assert stat["CSVに無い(触らない)"] == 1


# ---- 6. 各処理での優先 ------------------------------------------------------------

def test_付け替え_持ち主でない会社の取引群へは広げず持ち主へ運ぶ():
    deals = {"y_old": d(ENDED, "RLY"), "y_new": d(LIVE, "RLY"), "x_new": d(LIVE, "RLX")}
    d2l = {"y_old": ["L1", "L2"]}                 # L1 は持ち主X なのにYの旧取引に付いている
    pairs, manual, stat = RL.plan_all_live(deals, d2l, {}, {}, {}, {"L1": "RLX"})
    assert ("L1", "x_new") in pairs               # 持ち主Xの生きた取引へ
    assert ("L1", "y_new") not in pairs           # Yへは広げない
    assert ("L2", "y_new") in pairs               # 持ち主不明の L2 は従来どおり


def test_付け替え_持ち主が決まった求人は別会社またがりでも人の確認に回さない():
    deals = {"x": d(LIVE, "RLX"), "y": d(LIVE, "RLY")}
    d2l = {"x": ["L1"], "y": ["L1"]}
    pairs, manual, stat = RL.plan_all_live(deals, d2l, {}, {}, {}, {"L1": "RLX"})
    assert not any("別々の取引先コード" in k for k in stat)


def test_新規紐付け_持ち主コードがあればその生きた主契約へ():
    deals = {"x1": d(LIVE, "RLX"), "x0": d(ENDED, "RLX"), "y": d(LIVE, "RLY", shops="S9")}
    by_code = DM.group_by_code(deals)
    shop, mail = SDA.build_indexes(deals)
    listings = [{"id": "L1", "properties": {"id_shop_hrhakkaa": "S9", DM.LISTING_OWNER: "RLX"}}]
    pairs, stat, review = SDA.plan_new_links(listings, {}, shop, mail, {}, deals, by_code)
    assert pairs == [("L1", "x1", "hr")]


def test_担当者_持ち主コードがあれば店舗IDの共有でも持ち主の担当者():
    deals = {"x": d(LIVE, "RLX", shops="S9", owner="UX"), "y": d(LIVE, "RLY", shops="S9", owner="UY")}
    listings = [{"id": "L1", "properties": {"id_shop_hrhakkaa": "S9", "hubspot_owner_id": "UY",
                                            DM.LISTING_OWNER: "RLX"}}]
    to_set = SDA.plan_owner(listings, {"L1": ["y"]}, deals, DM.group_by_code(deals),
                            {"UX", "UY"}, DM.shop_code_index(deals))
    assert to_set == [("L1", "UX")]


def test_要否_持ち主コードがあれば持ち主の今の契約に合わせる(monkeypatch):
    deals = {"x": d(LIVE, "RLX", shops="S9", flag="true"), "y": d(LIVE, "RLY", shops="S9", flag="false")}
    monkeypatch.setattr(ICH, "list_all", lambda *a, **k: [
        {"id": "L1", "properties": {"id_shop_hrhakkaa": "S9", DM.LISTING_OWNER: "RLX",
                                    "ichijitaiounoumu_deforuto": "不要"}}])
    monkeypatch.setattr(ICH, "_batch_assoc", lambda ids: {"L1": ["y"]})
    monkeypatch.setattr(ICH, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(ICH, "_batch_deals", lambda ids: {})
    monkeypatch.setattr(ICH, "build_login_to_mail", lambda: {})
    r = ICH.run(dry_run=True)
    assert r["to_update"] == 1 and r["other_company_skipped"] == 0


# ---- 逆証明(第3回)の是正 ------------------------------------------------------

def test_持ち主は生きた契約のあるコードだけ_通知先が新旧の契約を指せば生きた方():
    deals = {"old": d(ENDED, "RLOLD", mails="rpo+a@x"), "new": d(LIVE, "RLNEW", mails="rpo+a@x")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("", ["rpo+a@x"], si, mi, DM.live_codes(deals)) == ("RLNEW", DM.BASIS_MAIL_LIVE)


def test_通知先が終わった契約だけなら店舗IDの生きたコードへ_無ければ決めない():
    deals = {"old": d(ENDED, "RLOLD", mails="rpo+a@x"), "y": d(LIVE, "RLY", shops="S1")}
    si, mi = _idx(deals)
    live = DM.live_codes(deals)
    assert DM.resolve_owner("S1", ["rpo+a@x"], si, mi, live) == ("RLY", DM.BASIS_SHOP)
    assert DM.resolve_owner("", ["rpo+a@x"], si, mi, live) == (None, DM.BASIS_NG_DEAD)


def test_店舗IDだけで終わった契約しか指さなければ決めない():
    deals = {"old": d(ENDED, "RLOLD", shops="S1")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("S1", [], si, mi, DM.live_codes(deals)) == (None, DM.BASIS_NG_DEAD)


def test_共通の元アドレスは判定に使わない():
    deals = {"a": d(LIVE, "RL1", mails="base@example.com;base+a@example.com")}
    si, mi = _idx(deals)
    assert DM.resolve_owner("", ["base@example.com"], si, mi, DM.live_codes(deals)) == (None, DM.BASIS_NG_NONE)
    assert DM.resolve_owner("", ["BASE+A@example.com"], si, mi, DM.live_codes(deals))[0] == "RL1"
