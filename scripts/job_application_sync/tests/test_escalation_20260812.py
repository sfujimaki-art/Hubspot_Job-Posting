# -*- coding: utf-8 -*-
"""エスカレーション（人へ回す通知）の回帰テスト (2026-08-12 / 2026-09-10 追随)。

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

## 2026-09-10: E-3 の持ち主が移ったことへの追随

E-3 は当初 `check_recent_listings_linked`（直近14日の求人が取引に紐付いているか）に
明細・やること・影響を持たせて解決していた。2026-08-17 に、同じ根本原因で必ず同時に
NGになる「直近30日に応募が来た求人で取引未紐付けの顧客」が**顧客単位**の要対応リスト
として入り、両方が明細と上位5件をSlackに出すと、受け手には同じ顧客が**別々の2依頼**に
見える状態になった（しかも求人単位のほうは顧客に畳まれていないぶん行数だけ多い）。

そこで現場への依頼は顧客単位の1本に統一し、求人単位のほうは**停止検知**に役割を絞った。
4要素を出すという性質は消えていない。**持ち主が移った**だけなので、テストも移す。
求人単位のほうには「明細を手放す代わりに、どこを見ればよいかを必ず書く」という
新しい性質が生まれたので、それをここで固定する。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import health_check as hc


@pytest.fixture(autouse=True)
def _reset_caches():
    """索引のプロセス内キャッシュをテスト間で持ち越さない。"""
    hc._reset_caches()
    yield
    hc._reset_caches()


# ---------------------------------------------------------------- 足場(停止検知)
def _listing(i, status="公開中", hr_shop="", aw_login="", name="求人A"):
    p = {"hs_name": name, "kyuujin_status": status}
    if hr_shop:
        p["id_hrhakkaa"] = "9999"
        p["id_shop_hrhakkaa"] = hr_shop
    if aw_login:
        p["id_airwork"] = "8888"
        p["airwork_account_login_id"] = aw_login
    return {"id": i, "properties": p}


def _setup(monkeypatch, rows, deal_map, appt_map):
    monkeypatch.setattr(hc, "search_all", lambda *a, **k: rows)
    monkeypatch.setattr(hc, "_assoc",
                        lambda frm, to, ids: deal_map if to == "0-3" else appt_map)


# ------------------------------------------------------------- 足場(要対応リスト)
def _job(*, shop="", login="", name="求人", url=""):
    """求人1件ぶんのプロパティ。HR(店舗ID) と AW(ログインID) を作り分ける。"""
    return {"hs_name": name, "kyuujin_status": "公開中",
            "id_hrhakkaa": "9999" if shop else "",
            "id_shop_hrhakkaa": shop,
            "id_airwork": "8888" if login else "",
            "airwork_account_login_id": login,
            "url_hrhakkaa": url if shop else "",
            "url_airwork": url if login else ""}


def _wire(monkeypatch, jobs, *, hint=None, mails=None, patch_hint=True):
    """要対応リストの外部依存を全部差し替える。求人1件につき応募1件を作る。

    取引には1件も紐付いていない状態にするので、渡した求人はそのまま要対応になる。
    索引(会社名/管理用メール)の既定は「読めたが空」= ok:True。読めなかった場合は
    対応区分が変わる別の話なので、その分岐は専用のテストで見る。
    """
    lids = list(jobs)
    apps = [f"app{i}" for i in range(len(lids))]
    a2l = {a: [lid] for a, lid in zip(apps, lids)}
    monkeypatch.setattr(hc, "search_all",
                        lambda *a, **k: [{"id": x} for x in apps])
    monkeypatch.setattr(hc, "_assoc",
                        lambda frm, to, ids: a2l if to == "0-420" else {})
    monkeypatch.setattr(hc, "_batch_props", lambda obj, ids, props:
                        {k: v for k, v in jobs.items() if k in set(ids)})
    if patch_hint:
        monkeypatch.setattr(hc, "_company_hint",
                            lambda: hint or {"idx": {}, "ok": True, "hr": {}})
    monkeypatch.setattr(hc, "_manage_mail_hint",
                        lambda: mails or {"exact": {}, "lower": {}, "ok": True})
    # 担当者名の解決は HubSpot への実HTTP。単体テストから外へ出さない
    monkeypatch.setattr(hc, "_owner_names", lambda: {})


def _rows(monkeypatch, jobs, **kw):
    _wire(monkeypatch, jobs, **kw)
    return hc.collect_unlinked_customers()["rows"]


# --------------------------------------------------------------------------
# E-3(a): 停止検知に役割を絞った側 — 件数だけで終わらせない
# --------------------------------------------------------------------------
def test_停止検知の項目は明細を持たない(monkeypatch):
    """求人単位で明細を出すと、顧客単位の依頼と二重になる (2026-08-17)。

    この項目と「直近30日に応募が来た求人で取引未紐付けの顧客」は同じ根本原因で
    必ず同時にNGになり、対象も重なる。両方が「対象一覧」と「上位5件」をSlackへ
    出すと、受け手には同じ顧客が別々の2依頼に見える。依頼は顧客単位の1本に統一し、
    こちらは「日々増えているか」だけを見る。
    """
    _setup(monkeypatch, [_listing("A", hr_shop="1234567")], {}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 1, "未紐付けの検知そのものは続ける(停止検知)"
    assert "items" not in r, "明細を持つのは顧客単位の要対応リストだけ"
    assert "action" not in r and "impact" not in r, (
        "現場への依頼文を2箇所から出さない")


def test_停止検知の項目は要対応リストへの導線を出す(monkeypatch):
    """件数だけ渡して終わりにしない。E-3 の再発防止はここに残る。

    明細を手放した代わりに、「何をどこに入れるか」がどこに書いてあるかを本文に
    必ず書く。これが無いと読者は件数だけを渡されて動けない(E-3 で実際にそうなった)。
    """
    _setup(monkeypatch, [_listing("A", hr_shop="1234567")], {}, {"A": ["P1"]})
    d = " ".join(hc.check_recent_listings_linked()["detail"])
    assert "要対応リスト" in d
    assert f"直近{hc.UNLINKED_DAYS}日に応募が来た求人で取引未紐付けの顧客" in d, (
        "移った先の項目名をそのまま書く(読者が探せる形で書く)")


def test_正常なら明細は空(monkeypatch):
    _setup(monkeypatch, [_listing("A", hr_shop="111")], {"A": ["D1"]}, {"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 0 and not r.get("items")


# --------------------------------------------------------------------------
# E-3(b): 4要素の持ち主 = 顧客単位の要対応リスト
# --------------------------------------------------------------------------
def test_やることと影響が付く(monkeypatch):
    """件数の隣に「やること」と「放置するとどうなるか」を必ず置く。"""
    _wire(monkeypatch, {"L1": _job(shop="S1")})
    r = hc.check_unlinked_listings_by_customer()
    assert r["action"] and r["impact"]
    assert "一次対応の要否" in r["impact"], "放置した時の実害を具体で書く"


def test_取引を作れとは言わない(monkeypatch):
    """応募が来ている顧客の取引は必ず存在する。作成依頼は出してはいけない。"""
    _wire(monkeypatch, {"L1": _job(shop="S1")})
    r = hc.check_unlinked_listings_by_customer()
    blob = r["action"] + r["impact"] + str(r["items"])
    assert "取引を作" not in blob
    assert "取引は必ず存在する" in r["action"], "見つからない=自分のバグ、と明示する"


def test_HRとAWで入れる場所が変わる(monkeypatch):
    """媒体で鍵の入れ先が違う。どちらも「どのレコードのどの項目か」まで書く。"""
    rows = _rows(monkeypatch,
                 {"L1": _job(shop="S1"), "L2": _job(login="shop@example.com")},
                 mails={"exact": {"shop@example.com": "kanri+sample@example.com"},
                        "lower": {"shop@example.com": "kanri+sample@example.com"},
                        "ok": True})
    by = {r["入れる鍵"]: r for r in rows}
    assert "HRハッカー店舗ID" in by["S1"]["入れる場所"]
    assert "S1" in by["S1"]["入れる場所"], "入れるべき値そのものを提示する"
    aw = by["shop@example.com"]["入れる場所"]
    assert "管理用メールアドレス" in aw
    assert "kanri+sample@example.com" in aw, "本番の紐付けが引く値をそのまま出す"


def test_会社名の候補が付く(monkeypatch):
    """どの取引を開けばよいか分かるように会社名を添える。"""
    rows = _rows(monkeypatch, {"L1": _job(shop="S1")},
                 hint={"idx": {"shop:S1": [("株式会社サンプル", False)]},
                       "ok": True, "hr": {}})
    assert rows[0]["会社名"] == "株式会社サンプル"


def test_会社名の候補が複数なら会社名を出さない(monkeypatch):
    """外れた会社名は空欄より悪い。人が別事業所の取引に書き込む。

    実測 2026-08-17: 顧客管理シートの126キーが2社以上で共用されていたのに、
    先勝ちで1社だけを表示していた。この検査の「やること」は会社名で取引を開いて
    鍵を入れることなので、外れた行では誤配が起きる。候補が複数なら会社名は出さず、
    注記に候補を並べて人へ回す(誤配より未解決が安全)。
    """
    rows = _rows(monkeypatch, {"L1": _job(shop="S1")},
                 hint={"idx": {"shop:S1": [("株式会社サンプルA", False),
                                           ("株式会社サンプルB", False)]},
                       "ok": True, "hr": {}})
    assert rows[0]["会社名"] == "", "決められないなら出さない"
    assert "候補が複数" in rows[0]["会社名の注記"]
    assert "株式会社サンプルA" in rows[0]["会社名の注記"], "候補は人に見せる"


def test_会社名が引けなくても対象を開ける導線が残る(monkeypatch):
    """会社名は best-effort。引けなくても行は出し、必ず1つは開けるリンクを持たせる。

    媒体側の求人URLが空の求人が実在する(実測4件)ので、URLの有無に関係なく開ける
    HubSpotの求人リンクを全行に持たせる。
    """
    r = _rows(monkeypatch, {"L1": _job(shop="S1")})[0]   # 索引は空
    assert r["会社名"] == "" and r["入れる鍵"] == "S1"
    assert r["求人URL(例)"] == ""
    assert r["HubSpot求人リンク"].endswith("/0-420/L1"), (
        "会社名も媒体URLも無い行でも、開ける先を必ず1つ持たせる")


def test_会社名索引の失敗で監視が死なない(monkeypatch):
    """_company_hint はシートとCSVを読む。落ちても検知は続けなければならない。

    ★2026-09-10: 見る場所を移した。会社名の逆引きは顧客単位の要対応リスト側へ
      移っており、停止検知の側は _company_hint を呼ばなくなっていたため、
      そちらを通しても索引の失敗を再現できず**素通りで合格**していた。
      索引を実際に壊して、行が出続けることと、空欄の理由が本文に出ることを見る。
    """
    from scripts.job_application_sync import applicant_queue as aq

    class _BrokenResolver:
        def build(self):
            raise RuntimeError("顧客管理シートを読めない")

    monkeypatch.setattr(aq, "AccountResolver", _BrokenResolver)
    _wire(monkeypatch, {"L1": _job(shop="S1")}, patch_hint=False)
    r = hc.check_unlinked_listings_by_customer()
    assert r["value"] == 1, "会社名が取れなくても未紐付けの検知は成立する"
    assert any("会社名の索引を作れませんでした" in d for d in r["detail"]), (
        "空欄の理由を黙らない(シート側の不備だと誤解させない)")
