# -*- coding: utf-8 -*-
"""「人が取引に鍵を入れる」要対応リストの回帰テスト (2026-08-17 / 2026-09-10 追随)。

## 何を守るテストか

このリストは現場に「この顧客の取引にこの値を入れてください」と依頼する。
**母数を間違えると依頼の量が桁で変わる**ので、絞り込みの順序を固定する:

  1. 直近N日に応募が来た応募   … 軸は yingmuri(応募日)。hs_createdate ではない
  2. その応募に紐付く求人
  3. その求人が**生きている取引**に紐付いていないもの
  4. 公開終了を除く
  5. 顧客単位(HR店舗ID / AWログインID)に集約し、会社名が分かった行は会社で畳む

実測 2026-08-17: 応募2,226 → 求人970 → 未紐付け105 → 公開中85 → 顧客71。
(同日の再実行では 93→75→63。応募と求人の件数は同一で、間に
 sync_deal_association が10件を紐付けたため。母数の定義は動いていない)

## 特に守りたい不変条件

- **応募の重複カウントをしない**。応募と求人は多対多なので、素朴に足すと
  1件の応募が複数求人ぶん数えられ、実害を過大に報告する
- **顧客単位に畳む**。鍵は顧客ごとに1つしか無く、人の作業は1顧客1回。
  求人単位で出すと同じ作業を何行にも分けて依頼することになる
- **対応区分を分ける**。実測63件中16件はAWログインIDが顧客管理シートに無く、
  取引を開いても入れる値が存在しない(先にシートを直す別作業)

## 2026-09-10: 実装の変更に追随した点

本体は 2026-08-17 に4つ変わっていたが、テストが取り残されて落ちたままだった。
守る性質そのものは減っていないので、新しい形で書き直す。

| # | 実装がどう変わったか | テストをどう直したか |
|---|---|---|
| 1 | 「取引に紐付いているか」→「**生きた取引に**紐付いているか」。納品管理PLの取引は契約更新のたびに作り直され、前の取引は継続済へ移るので、死んだ取引にだけ紐付く求人が「紐付き在り」として母数から黙って消えていた | 取引側のプロパティ(pipeline/dealstage)も差し替えられるようにし、終了した取引にだけ紐付く求人が母数に入ることを別テストで固定した |
| 2 | funnel のキーが「何を数えたか」が分かる名前になり、**脱落の内訳**(求人が取得できず除外 等)は `meta` へ分離された | 新しいキーで読む。件数の意味は変えていない |
| 3 | Slackに出す列が「先頭5列」から `slack_cols` の明示指定に変わった(列を1つ足した瞬間に「対応区分」が黙って通知から消えるため) | 列順の固定ではなく「出す列が明示されていて、その列が行に実在する」を見る。テスト側からも位置依存を外す |
| 4 | 索引(会社名/管理用メール)が成否を `ok` で返すようになった。作れなかったことを「シートに未登録」と断定しない | 索引の戻り値の形を実装に合わせ、「作れなかった」ケースを別テストに切り出した |
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import health_check as hc


# ---------------------------------------------------------------- 足場
def _listing(lid, *, status="公開中", hr=None, shop="", login="", name=None,
             url_hr="", url_aw=""):
    p = {"hs_name": name if name is not None else f"求人{lid}",
         "kyuujin_status": status, "id_shop_hrhakkaa": shop,
         "airwork_account_login_id": login,
         "url_hrhakkaa": url_hr, "url_airwork": url_aw}
    if hr:
        p["id_hrhakkaa"] = hr
    return p


#: 契約が終わったステージのID。ラベルから引く(IDを直接書くと実装と二重管理になる)。
_DEAD_STAGE = next(k for k, v in hc.DEAD_STAGES.items() if v == "継続済")


def _deal(*, dead=False, name="取引", owner=""):
    """納品管理PLの取引。dead=True で「契約がそこで終わっている」ステージにする。"""
    return {"dealname": name,
            "pipeline": hc.PIPELINE_NOUHIN,
            # 生きている側は DEAD_STAGES に無いステージなら何でもよい
            "dealstage": _DEAD_STAGE if dead else "999999999",
            "hubspot_owner_id": owner}


@pytest.fixture(autouse=True)
def _no_sheets(monkeypatch):
    """会社名/管理用メールの索引は外部(スプレッドシート)依存なので既定で殺す。

    索引が引けなくても行は出る = best-effort であることをここで担保する。

    ★既定は「読めたが1件も無い」= ok:True (2026-09-10)。本体は「索引を作れなかった」
      と「シートにその顧客が無い」を区別して対応区分を変えるようになったので、
      既定でどちらか一方に倒すと、もう一方の分岐が誰にも見られなくなる。
      作れなかった側は専用のテストで見る。
    ★担当者名の解決(_owner_names)は HubSpot への実HTTP。単体テストから外へ出さない。
    """
    hc._reset_caches()
    monkeypatch.setattr(hc, "_company_hint",
                        lambda: {"idx": {}, "ok": True, "hr": {}})
    monkeypatch.setattr(hc, "_manage_mail_hint",
                        lambda: {"exact": {}, "lower": {}, "ok": True})
    monkeypatch.setattr(hc, "_owner_names", lambda: {})
    yield
    hc._reset_caches()


def _wire(monkeypatch, *, apps, app2listing, listing2deal, listing_props,
          deal_props=None):
    """HubSpot呼び出しを差し替える。

    ★取引側のプロパティも渡せる (2026-09-10)。本体が「生きた取引に紐付いているか」を
      見るようになり、求人→取引の関連だけでなく**取引のステージ**まで読むため。
      deal_props を省いた取引は「生きている」扱いになる。
    """
    seen = {}
    deal_props = deal_props or {}

    def fake_search_all(obj, props, filters):
        seen["obj"] = obj
        seen["props"] = props
        seen["filters"] = filters
        return [{"id": a} for a in apps]

    def fake_assoc(frm, to, ids):
        if (frm, to) == ("0-421", "0-420"):
            return {k: v for k, v in app2listing.items() if k in set(ids)}
        if (frm, to) == ("0-420", "0-3"):
            return {k: v for k, v in listing2deal.items() if k in set(ids)}
        raise AssertionError(f"想定外の関連付け取得: {frm}->{to}")

    def fake_props(obj, ids, props):
        if obj == "0-3":
            return {d: deal_props.get(d, _deal()) for d in ids}
        assert obj == "0-420", f"求人でも取引でもないものを読んでいる: {obj}"
        return {k: v for k, v in listing_props.items() if k in set(ids)}

    monkeypatch.setattr(hc, "search_all", fake_search_all)
    monkeypatch.setattr(hc, "_assoc", fake_assoc)
    monkeypatch.setattr(hc, "_batch_props", fake_props)
    return seen


# ---------------------------------------------------------------- 1. 母数の軸
def test_応募日で検索する_登録日ではない(monkeypatch):
    seen = _wire(monkeypatch, apps=[], app2listing={}, listing2deal={},
                 listing_props={})
    hc.collect_unlinked_customers(days=30)
    assert seen["obj"] == "0-421"
    assert {f["propertyName"] for f in seen["filters"]} == {"yingmuri"}, (
        "hs_createdate(登録日)で数えると過去分の一括取込が母数に混ざる")


def test_応募日の範囲はepochミリ秒の文字列(monkeypatch):
    """yingmuri は date型。ISO文字列を渡すと HubSpot は HTTP 400 を返す。"""
    seen = _wire(monkeypatch, apps=[], app2listing={}, listing2deal={},
                 listing_props={})
    hc.collect_unlinked_customers(days=30)
    v = seen["filters"][0]["value"]
    assert isinstance(v, str) and v.isdigit(), f"epochミリ秒の文字列でない: {v!r}"
    assert len(v) == 13, f"ミリ秒でない(秒になっている?): {v!r}"


def test_応募がゼロなら空のリスト(monkeypatch):
    """★応募0件は「正常」ではなく「静かになった」。フラグを立てて次段へ渡す。

    取込が止まると、この検査は鳴るどころか**未紐付け0件=健全**に見える。
    それは health_check がそもそも直そうとした失敗形なので、母数が0だったことを
    meta["入力ゼロ"] で明示する。
    """
    _wire(monkeypatch, apps=[], app2listing={}, listing2deal={},
          listing_props={})
    res = hc.collect_unlinked_customers()
    assert res["rows"] == []
    assert res["funnel"]["応募が紐付く求人"] == 0
    assert res["meta"]["入力ゼロ"] is True, "0件を黙って正常にしない"


# ---------------------------------------------------------------- 2. 絞り込み
def test_生きた取引に紐付いている求人は出さない(monkeypatch):
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={"L1": ["D1"]},          # L1 は生きた取引に紐付き済み
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", hr="2", shop="S2")},
          deal_props={"D1": _deal(name="生きている取引")})
    res = hc.collect_unlinked_customers()
    assert res["funnel"]["生きた取引に紐付いていない求人"] == 1
    assert [r["入れる鍵"] for r in res["rows"]] == ["S2"]


def test_終了した取引にだけ紐付く求人は母数に入れる(monkeypatch):
    """紐付きの有無だけを見ると、要対応の求人が「健全」として黙って消える。

    納品管理PLの取引は契約更新のたびに新しく作られ、前の取引は継続済へ移る。
    死んだ取引にだけ紐付いている求人は「紐付き在り」なので、従来は母数から
    落ちていた(実測 2026-08-17: 紐付き877求人のうち193件がこれ。応募463件)。
    やることも違う — 鍵を入れるのではなく**生きている取引へ入れ替える**ので、
    対応区分で区別し、現在の紐付け先を証拠として出す。
    """
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={"L1": ["D9"]},
          listing_props={"L1": _listing("L1", hr="1", shop="S1")},
          deal_props={"D9": _deal(dead=True, name="サブスク継続＿サンプル株式会社")})
    res = hc.collect_unlinked_customers()
    assert res["funnel"]["生きた取引に紐付いていない求人"] == 1
    assert res["meta"]["死んだ取引にだけ紐付く求人"] == 1
    r = res["rows"][0]
    assert "生きている取引へ鍵を入れ替える" in r["対応区分"]
    assert "サブスク継続＿サンプル株式会社" in r["現在の紐付け先(終了した取引)"], (
        "どの取引に紐付いているのかを出さないと、人は入れ替え先を探せない")
    assert r["会社名"] == "サブスク継続＿サンプル株式会社", (
        "終了した取引の名前は、シートの逆引きより強い会社名の根拠(推測が入らない)")


def test_公開終了は出さない(monkeypatch):
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1",
                                        status="公開終了"),
                         "L2": _listing("L2", hr="2", shop="S2")})
    res = hc.collect_unlinked_customers()
    assert res["funnel"]["生きた取引に紐付いていない求人"] == 2
    assert res["funnel"]["公開終了を除く"] == 1
    assert [r["入れる鍵"] for r in res["rows"]] == ["S2"]


def test_取得できない求人は除外して件数を残す(monkeypatch):
    """アーカイブ/削除済の求人は鍵を入れても直らない。黙って混ぜない。

    ★件数の置き場が funnel から meta へ移った (2026-08-17)。funnel は
      「各段で何件になったか」だけを並べ、脱落の内訳は meta に分ける。
      同じ列に混ぜると、段の件数と脱落の件数が足し引きできる数字に見えてしまう。
    """
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["Lgone"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1")})
    res = hc.collect_unlinked_customers()
    assert res["meta"]["求人が取得できず除外"] == 1
    assert len(res["rows"]) == 1


# ---------------------------------------------------------------- 3. 顧客集約
def test_同じ店舗の求人は1行に畳む(monkeypatch):
    """鍵は顧客ごとに1つ。人の作業は1回なので1行にする。"""
    _wire(monkeypatch, apps=["a1", "a2", "a3"],
          app2listing={"a1": ["L1"], "a2": ["L2"], "a3": ["L3"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1", name="配送A"),
                         "L2": _listing("L2", hr="2", shop="S1", name="配送B"),
                         "L3": _listing("L3", hr="3", shop="S1", name="配送C")})
    res = hc.collect_unlinked_customers()
    assert len(res["rows"]) == 1, "同一店舗IDは1顧客1行"
    r = res["rows"][0]
    assert r["影響する求人数"] == 3
    assert r["応募数"] == 3
    assert r["求人名の例"] == "配送A / 配送B / 配送C"


def test_求人名の例は3件まで(monkeypatch):
    props = {f"L{i}": _listing(f"L{i}", hr=str(i), shop="S1", name=f"職{i}")
             for i in range(5)}
    _wire(monkeypatch, apps=[f"a{i}" for i in range(5)],
          app2listing={f"a{i}": [f"L{i}"] for i in range(5)},
          listing2deal={}, listing_props=props)
    r = hc.collect_unlinked_customers()["rows"][0]
    assert r["影響する求人数"] == 5
    assert len(r["求人名の例"].split(" / ")) == 3, "例は3件まで(通知が長くなる)"


def test_応募を重複カウントしない(monkeypatch):
    """応募と求人は多対多。1件の応募が2求人に紐付いても応募数は1。"""
    _wire(monkeypatch, apps=["a1"],
          app2listing={"a1": ["L1", "L2"]},      # 同じ応募が2求人に紐付く
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", hr="2", shop="S1")})
    res = hc.collect_unlinked_customers()
    assert len(res["rows"]) == 1
    assert res["rows"][0]["応募数"] == 1, "多対多を素朴に足すと実害を過大報告する"
    assert res["rows"][0]["影響する求人数"] == 2


def test_HRとAWは別の顧客として数える(monkeypatch):
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", login="aw@example.com")})
    res = hc.collect_unlinked_customers()
    assert {r["媒体"] for r in res["rows"]} == {"HRハッカー", "AirWork"}
    assert res["funnel"]["要対応の鍵"] == 2
    assert res["funnel"]["要対応の顧客(会社に畳んだ後)"] == 2


def test_AWログインIDは大小文字を無視して畳む(monkeypatch):
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", login="Shop@Example.com"),
                         "L2": _listing("L2", login="shop@example.com")})
    res = hc.collect_unlinked_customers()
    assert len(res["rows"]) == 1, "同じログインIDを表記違いで二重に依頼しない"


def test_同じ会社の別の鍵は1行に畳む(monkeypatch):
    """会社名が分かった行は会社で畳む。同じ取引を2回開かせない (2026-08-17)。

    1社が複数の店舗IDを持つ / HRとAWの両方で未紐付け、という理由で行が割れる。
    実測: 会社名が引けた30行の実会社数は22社だった。会社名が引けない行は
    同一かどうか判定できないので畳まない(推測しない)。
    """
    monkeypatch.setattr(hc, "_company_hint",
                        lambda: {"idx": {"shop:S1": [("株式会社サンプル", False)],
                                         "shop:S2": [("株式会社サンプル", False)]},
                                 "ok": True, "hr": {}})
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", hr="2", shop="S2")})
    res = hc.collect_unlinked_customers()
    assert res["funnel"]["要対応の鍵"] == 2
    assert res["funnel"]["要対応の顧客(会社に畳んだ後)"] == 1
    r = res["rows"][0]
    assert r["作業回数"] == 2, "1行に畳んでも、人が触る回数は減らさずに伝える"
    assert "S1" in r["入れる鍵"] and "S2" in r["入れる鍵"], "鍵は全部出す"


# ---------------------------------------------------------------- 4. 並び順
def test_応募が多い順に並ぶ(monkeypatch):
    """Slackには上位数件しか出ない。放置の実害が大きい順に並べる。"""
    props, a2l, apps = {}, {}, []
    for i, n in enumerate([1, 9, 4]):
        lid = f"L{i}"
        props[lid] = _listing(lid, hr=str(i), shop=f"S{i}")
        ids = [f"a{i}_{j}" for j in range(n)]
        apps += ids
        for aid in ids:
            a2l[aid] = [lid]
    _wire(monkeypatch, apps=apps, app2listing=a2l, listing2deal={},
          listing_props=props)
    got = [r["応募数"] for r in hc.collect_unlinked_customers()["rows"]]
    assert got == [9, 4, 1]


# ---------------------------------------------------------------- 5. 対応区分
def test_シートに無いAWは別区分にする(monkeypatch):
    """取引を開いても入れる値が存在しない。先にシートを直す別作業。"""
    monkeypatch.setattr(hc, "_manage_mail_hint",
                        lambda: {"exact": {"known@example.com":
                                           "kanri+sample@example.com"},
                                 "lower": {"known@example.com":
                                           "kanri+sample@example.com"},
                                 "ok": True})
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", login="known@example.com"),
                         "L2": _listing("L2", login="unknown@example.com")})
    rows = {r["入れる鍵"]: r for r in hc.collect_unlinked_customers()["rows"]}
    assert rows["known@example.com"]["対応区分"] == "取引に管理用メールを入れる"
    assert "kanri+sample@example.com" in rows["known@example.com"]["入れる場所"]
    assert rows["unknown@example.com"]["対応区分"] == "顧客管理シートに未登録（先にシートを直す）"


def test_索引を作れなければシートに未登録と断定しない(monkeypatch):
    """索引の失敗は「シートに無い」ではない。壊れているのはこちら側。

    断定すると、現場は実在する行を探しに行って空振りする(しかも原因はSheetsの
    認証/クォータ/通信であってシートではない)。判定不能として出し、先に索引の
    失敗を直してもらう。
    """
    monkeypatch.setattr(hc, "_manage_mail_hint",
                        lambda: {"exact": {}, "lower": {}, "ok": False})
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", login="known@example.com")})
    r = hc.collect_unlinked_customers()["rows"][0]
    assert r["対応区分"] == "判定不能（管理用メールの索引を作れず）"
    assert "信頼できません" in r["入れる場所"], "この行の指示に従わせない"


def test_鍵が無い求人は取込の不具合として出す(monkeypatch):
    """人には直せない。現場への依頼に混ぜない。"""
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1")})   # shop も login も空
    r = hc.collect_unlinked_customers()["rows"][0]
    assert r["入れる鍵"] == ""
    assert r["対応区分"] == "求人に鍵が無い（取込の不具合）"


# ---------------------------------------------------------------- 6. 出力の形
def test_通知に出す列は明示され行に実在する(monkeypatch):
    """列は位置ではなく名前で決める (2026-08-17 に実装が変更)。

    以前は「Slackは先頭5列を出す」という位置依存だったため、列を1つ足した瞬間に
    「対応区分」や「入れる鍵」が黙って通知から消えた。実装が slack_cols で出す列を
    明示するようになったので、テストも「先頭6列がこの順」ではなく
    「明示された列が行に実在する」を見る(テスト側からも位置依存を外す)。
    """
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={}, listing_props={"L1": _listing("L1", hr="1", shop="S1")})
    r = hc.check_unlinked_listings_by_customer()
    cols = r["slack_cols"]
    assert cols, "通知に出す列を実装が明示していない(先頭N列に戻っている)"
    row = r["items"][0]
    for c in cols:
        assert c in row, f"通知に出す列が行に無い: {c}"
    assert {"会社名", "対応区分", "入れる鍵"} <= set(cols), (
        "誰の・何を・どこに に当たる列は通知から落とさない")


def test_会社名が引けなくても求人URLで特定できる(monkeypatch):
    """会社名の逆引きは best-effort。空でも行は出し、代わりの導線を持たせる。"""
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1",
                                        url_hr="https://hr-hacker.com/x/1")})
    r = hc.collect_unlinked_customers()["rows"][0]
    assert r["会社名"] == ""
    assert r["求人URL(例)"] == "https://hr-hacker.com/x/1"


def test_会社名が引けた件数を持ち帰る(monkeypatch):
    """空欄が並ぶのを「名前の無い顧客」と誤読させない。

    ★索引の形が {キー: [(会社名, クローズ済か), …]} に変わった (2026-08-17)。
      1キーを複数社が共用する実データがあり、1社に潰すと誤配が起きるため。
    """
    monkeypatch.setattr(hc, "_company_hint",
                        lambda: {"idx": {"shop:S1": [("株式会社サンプル", False)]},
                                 "ok": True, "hr": {}})
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", hr="2", shop="S2")})
    res = hc.collect_unlinked_customers()
    assert res["diag"]["会社名なし"] == 1
    assert res["diag"]["HR店舗索引"] == 1


# ---------------------------------------------------------------- 7. 通知の形
def test_通知に必要な4点が揃う(monkeypatch):
    """やること / 放置すると / 対象一覧(items) / 内訳 が無いと動けない。

    ★collect_unlinked_customers を丸ごと差し替えない (2026-09-10)。以前は戻り値を
      手で組み立てていたため、本体が funnel のキー名を変えても**テストだけが古い形の
      まま**で、最後は KeyError で落ちた。本体を実際に通し、包み方だけを見る。
    """
    _wire(monkeypatch, apps=["a1", "a2"],
          app2listing={"a1": ["L1"], "a2": ["L2"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1"),
                         "L2": _listing("L2", hr="2", shop="S2")})
    r = hc.check_unlinked_listings_by_customer()
    assert r["value"] == 2 and r["want"] == 0
    assert r["action"] and r["impact"] and r["items"]
    assert "内訳" in " ".join(r["detail"])


def test_応募がゼロの日は未紐付けを判定できないと言う(monkeypatch):
    """0件を「異常なし」で通さない。取込が止まると静かになるのがこの検査の弱点。"""
    _wire(monkeypatch, apps=[], app2listing={}, listing2deal={},
          listing_props={})
    r = hc.check_unlinked_listings_by_customer()
    assert r["value"] == -1, "0件(正常)と区別できる値で返す"
    assert "判定はできていません" in " ".join(r["detail"])


def test_HR索引が空なら理由を明示する(monkeypatch):
    """CIには HR求人CSV が無い。空欄の理由を黙らせない。"""
    _wire(monkeypatch, apps=["a1"], app2listing={"a1": ["L1"]},
          listing2deal={},
          listing_props={"L1": _listing("L1", hr="1", shop="S1")})
    d = " ".join(hc.check_unlinked_listings_by_customer()["detail"])
    assert "HR求人CSV" in d and "会社名を逆引きできず" in d


def test_CIでは対象一覧に実行ページのURLを出す(monkeypatch):
    """runner のローカルパスをSlackに出しても誰も開けない。"""
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("HEALTH_ARTIFACT_NAME", "要対応リスト")
    hint = hc._artifact_hint()
    assert hint.startswith(
        "https://github.com/o/r/actions/runs/123 の成果物「要対応リスト」")
    assert "GitHub" in hint, "リポジトリのコラボレーター以外は開けないことも書く"


def test_成果物のステップが無ければ実行ページへ飛ばさない(monkeypatch):
    """GITHUB_* が在るだけで案内すると、成果物の無いページへ読者を飛ばす。

    health_check.py だけが先に本番へ入り、ワークフロー側の upload-artifact が
    まだ無い状態が実在する。ワークフローが成果物名を渡した時に限って案内する。
    """
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.delenv("HEALTH_ARTIFACT_NAME", raising=False)
    assert hc._artifact_hint() == ""


def test_ローカルでは従来どおりファイルパスを使う(monkeypatch):
    for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
              "HEALTH_ARTIFACT_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert hc._artifact_hint() == ""
