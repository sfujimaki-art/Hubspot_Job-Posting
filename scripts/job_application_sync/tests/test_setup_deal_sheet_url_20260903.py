# -*- coding: utf-8 -*-
"""取引への応募者管理シートURL集約のテスト (2026-09-03)。

守る性質:
1. 求人票の値を親の取引へ集める。同じシートが複数求人票から来ても1回
2. ★既に値がある取引は触らない (人の入力を機械が上書きしない)
3. ★1取引に2種類のシートが集まったら書かずに人へ回す (勝手に選ばない)
4. URLの表記ゆれ (/edit#gid=0 の有無) は同じシートとして扱う
5. 二度流しても増えない・変わらない
"""
from __future__ import annotations

from scripts.job_application_sync import setup_deal_sheet_url as S

U1 = "https://docs.google.com/spreadsheets/d/1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit#gid=0"
U1B = "https://docs.google.com/spreadsheets/d/1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
U2 = "https://docs.google.com/spreadsheets/d/2BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB/edit"


def _deal(url="", live=True, code="RL0001", name="取引A"):
    return {"name": name, "stage": "x", "code": code, "url": url, "live": live}


def test_求人票の値を親の取引へ集める():
    p = S.plan_rollup({"L1": {"url": U1}}, {"L1": ["D1"]}, {"D1": _deal()})
    assert [(r["deal_id"], r["url"]) for r in p["write"]] == [("D1", U1)]


def test_同じシートが複数求人票から来ても1件():
    p = S.plan_rollup({"L1": {"url": U1}, "L2": {"url": U1}},
                      {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert len(p["write"]) == 1
    assert sorted(p["write"][0]["from_listings"]) == ["L1", "L2"]


def test_既に値がある取引は触らない():
    """★人が入れた値を機械が上書きしない."""
    p = S.plan_rollup({"L1": {"url": U1}}, {"L1": ["D1"]},
                      {"D1": _deal(url=U2)})
    assert not p["write"] and [r["deal_id"] for r in p["skip_has"]] == ["D1"]


def test_2種類のシートが混在したら人へ回す():
    """★勝手にどちらかを選ばない (別顧客のシートへ書く事故を防ぐ)."""
    p = S.plan_rollup({"L1": {"url": U1}, "L2": {"url": U2}},
                      {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert not p["write"]
    assert [d["deal_id"] for d in p["defer"]] == ["D1"]
    assert len(p["defer"][0]["sheets"]) == 2


def test_公開中の求人票を優先して混在を解く():
    """★顧客がシートを引っ越すと、古いシートが公開終了の求人票にだけ残る。
    転記先は「今応募が来る求人票が指すシート」が正 (実測: 混在5件中4件がこれ)."""
    p = S.plan_rollup(
        {"L1": {"url": U1, "status": "公開中"}, "L2": {"url": U2, "status": "公開終了"}},
        {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert [r["url"] for r in p["write"]] == [U1] and not p["defer"]


def test_公開中が2種類なら人へ回す():
    """公開中どうしで割れているものは機械が選べない (実測: 大分港運1件)."""
    p = S.plan_rollup(
        {"L1": {"url": U1, "status": "公開中"}, "L2": {"url": U2, "status": "公開中"}},
        {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert not p["write"] and [d["deal_id"] for d in p["defer"]] == ["D1"]


def test_全部公開終了なら公開終了どうしで判断する():
    """跡地だけの契約でもシートURLは資産として引き継ぐ."""
    p = S.plan_rollup(
        {"L1": {"url": U1, "status": "公開終了"}, "L2": {"url": U2, "status": "公開終了"}},
        {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert not p["write"] and len(p["defer"]) == 1


def test_表記ゆれは同じシートとみなす():
    """/edit#gid=0 の有無で「2種類」と誤判定しない."""
    assert S.sheet_id(U1) == S.sheet_id(U1B)
    p = S.plan_rollup({"L1": {"url": U1}, "L2": {"url": U1B}},
                      {"L1": ["D1"], "L2": ["D1"]}, {"D1": _deal()})
    assert len(p["write"]) == 1 and not p["defer"]


def test_跡地と生存の両方に紐づくなら両方に入れる():
    """求人票は relink 前だと跡地にも紐づく。取引ごとに1つ持つのが正なので両方書く。
    生きている取引を先に処理する (途中で止まっても実害が小さい順)."""
    p = S.plan_rollup({"L1": {"url": U1}}, {"L1": ["OLD", "NEW"]},
                      {"OLD": _deal(live=False), "NEW": _deal(live=True)})
    assert [r["deal_id"] for r in p["write"]] == ["NEW", "OLD"]


def test_二度流しても変わらない():
    """1回目で入った値は2回目で skip_has になる (冪等)."""
    lst, d_of = {"L1": {"url": U1}}, {"L1": ["D1"]}
    p1 = S.plan_rollup(lst, d_of, {"D1": _deal()})
    assert len(p1["write"]) == 1
    p2 = S.plan_rollup(lst, d_of, {"D1": _deal(url=p1["write"][0]["url"])})
    assert not p2["write"] and len(p2["skip_has"]) == 1


def test_紐づく取引が取れなければ何もしない():
    p = S.plan_rollup({"L1": {"url": U1}}, {}, {})
    assert not p["write"] and not p["defer"]


def test_プロパティ定義は求人票と同じ型():
    """型が違うと突合できない。グループは納品管理."""
    assert S.PROP_DEF["name"] == "customer_sheet_url"
    assert (S.PROP_DEF["type"], S.PROP_DEF["fieldType"]) == ("string", "text")
    assert S.PROP_DEF["groupName"] == "納品管理"
