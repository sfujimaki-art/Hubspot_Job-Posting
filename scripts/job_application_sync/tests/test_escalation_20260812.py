"""エスカレーション（人へ回す通知）の回帰テスト (2026-08-12)。

## なぜ作ったか

自動化で埋まらなかった項目を人へ回す経路が、3箇所で機能していなかった。

| # | 何が起きていたか |
|---|---|
| E-1 | 店舗ID補完の失敗が `::warning::` にしか出ず、**Slackに飛ばない**。止まっても誰も気づかない |
| E-2 | RPOアドレスの「候補が複数で機械では決められない」件がCSVに出るだけ。実測13件が滞留 |
| E-3 | `health_check` が件数しか出さず、**誰が何をどこに入れるか**が分からない |

E-3 は特に、5項目中3項目が恒久NGのまま誰にも見られていなかった実績がある
（較正ミスに加え、件数だけでは動けなかったことも一因）。

## 通知に必ず入れる4要素（設計方針）

1. **対象の特定情報**（取引名・求人名・会社名・ID）
2. **入れるべき値**（分かる場合は候補を提示）
3. **入れる場所**（HubSpotのどのレコードのどの項目か）
4. **放置した場合の影響**

「取引が無い」は通知しない。応募が来ている顧客の取引は必ず納品管理PLに存在するため、
見つからないなら突合ロジックの欠陥であって、現場への依頼にしてはいけない。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import health_check as hc


def _listing(i, status="公開中", hr_shop="", aw_login="", name="求人A"):
    p = {"hs_name": name, "kyuujin_status": status}
    if hr_shop:
        p["id_hrhakkaa"] = "9999"
        p["id_shop_hrhakkaa"] = hr_shop
    if aw_login:
        p["id_airwork"] = "8888"
        p["airwork_account_login_id"] = aw_login
    return {"id": i, "properties": p}


def _setup(monkeypatch, rows, deal_map, appt_map, hint=None):
    monkeypatch.setattr(hc, "search_all", lambda *a, **k: rows)
    monkeypatch.setattr(hc, "_assoc",
                        lambda frm, to, ids: deal_map if to == "0-3" else appt_map)
    monkeypatch.setattr(hc, "_company_hint", lambda: (hint or {}))


# --------------------------------------------------------------------------
# E-3: 件数だけでなく「誰が・何を・どこに・放置するとどうなるか」
# --------------------------------------------------------------------------
def test_未紐付けの明細が返る(monkeypatch):
    rows = [_listing("A", hr_shop="1521642")]
    _setup(monkeypatch, rows, {}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 1
    assert len(r["items"]) == 1
    it = r["items"][0]
    assert it["媒体"] == "HRハッカー"
    assert it["HR店舗ID"] == "1521642"
    assert "1521642" in it["入れる場所"], "入れるべき値そのものを提示する"


def test_HRとAWで入れる場所が変わる(monkeypatch):
    rows = [_listing("A", hr_shop="111"), _listing("B", aw_login="x@y.jp")]
    _setup(monkeypatch, rows, {}, {"A": ["P1"], "B": ["P2"]})
    items = {i["求人ID"]: i for i in hc.check_recent_listings_linked()["items"]}
    assert "HRハッカー店舗ID" in items["A"]["入れる場所"]
    assert "管理用メールアドレス" in items["B"]["入れる場所"]


def test_やることと影響が付く(monkeypatch):
    _setup(monkeypatch, [_listing("A", hr_shop="111")], {}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["action"] and r["impact"]
    assert "一次対応の要否" in r["impact"], "放置した時の実害を具体で書く"


def test_取引を作れとは言わない(monkeypatch):
    """応募が来ている顧客の取引は必ず存在する。作成依頼は出してはいけない。"""
    _setup(monkeypatch, [_listing("A", hr_shop="111")], {}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    blob = r["action"] + r["impact"] + str(r["items"])
    assert "取引を作" not in blob
    assert "取引は必ず存在する" in r["action"], "見つからない=自分のバグ、と明示する"


def test_会社名の候補が付く(monkeypatch):
    """どの取引を開けばよいか分かるように会社名を添える。"""
    rows = [_listing("A", hr_shop="1519030"), _listing("B", aw_login="x@y.jp")]
    _setup(monkeypatch, rows, {}, {"A": ["P1"], "B": ["P2"]},
           hint={"shop:1519030": "スカイ・ウォーター株式会社", "x@y.jp": "渡辺容器株式会社"})
    items = {i["求人ID"]: i for i in hc.check_recent_listings_linked()["items"]}
    assert items["A"]["会社名(候補)"] == "スカイ・ウォーター株式会社"
    assert items["B"]["会社名(候補)"] == "渡辺容器株式会社"


def test_会社名が引けなくても明細は出る(monkeypatch):
    """会社名は best-effort。引けなくても件数と店舗IDは必ず出す。"""
    _setup(monkeypatch, [_listing("A", hr_shop="111")], {}, {"A": ["P1"]}, hint={})
    it = hc.check_recent_listings_linked()["items"][0]
    assert it["会社名(候補)"] == "" and it["HR店舗ID"] == "111"


def test_正常なら明細は空(monkeypatch):
    _setup(monkeypatch, [_listing("A", hr_shop="111")], {"A": ["D1"]}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 0 and not r.get("items")


def test_会社名索引の失敗で監視が死なない(monkeypatch):
    """_company_hint はシートとCSVを読む。落ちても検知は続けなければならない。"""
    monkeypatch.setattr(hc, "search_all",
                        lambda *a, **k: [_listing("A", hr_shop="111")])
    monkeypatch.setattr(hc, "_assoc",
                        lambda frm, to, ids: {} if to == "0-3" else {"A": ["P1"]})
    r = hc.check_recent_listings_linked()   # 本物の _company_hint を通す
    assert r["value"] == 1, "会社名が取れなくても未紐付けの検知は成立する"
