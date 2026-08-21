"""契約をまたぐ暗黙知メモの引き継ぎテスト (2026-08-21)。

## なぜ作ったか

暗黙知メモの正を取引に置いたが、**取引も契約更新のたびに作り替えられIDが変わる**。
求人で起きていた喪失を、規模を小さくして取引へ移しただけの状態だった。

    RL00000030  株式会社みどり産業
       ├─ 取引 60177607584（継続済＝跡地）    ← メモはここにある
       └─ 取引 62939569706（継続_定期実施前） ← 生きている。メモが無い

実測(2026-08-21): **125グループ**で、同じ契約内にメモがあるのに生きている取引へ
届いていなかった。集約は求人の関連先(to[0])へ貼るだけで契約グループを見ない。

## 扱う範囲

契約グループ1,301の内訳: 対象外959 / 既に生存側にある182 / **継承する160**。

**生存が複数のグループも貼る** (2026-08-21 ユーザー指示「継承して欲しい」)。
求人はどの取引にもぶら下がりうるので、片方だけだとその求人経由の応募にメモが
届かない。同じ本文を生きている取引すべてへ貼る。

## このテストが守る性質

1. 跡地のメモが生きている取引へ渡る（本体の目的）
2. **生存側の既存メモを消さない**（単純コピーにせずマージする）
3. **値が食い違っても省略せず両方を残す**（片方だけ更新され、もう片方は
   未更新という状態が普通に起きる。どちらかを落とすと本文から条件が消える）
4. 二度流しても増えない（毎晩走るので冪等でなければ本文が膨らむ）
5. ロールバックできる（実行前の本文を必ず持つ）
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import deal_stages as DS
from scripts.job_application_sync import inherit_rollup_by_contract as IC
from scripts.job_application_sync.notes import ROLLUP_SIGNATURE

TODAY = "2026-08-21"
LIVE = "52016155"                       # 求人出稿完了 (書込可)
LIVE2 = "1049738304"                    # オプション（求人追加・一次対応）
KEIZOKU = next(iter(DS.ENDED_ONLY))
KAIYAKU = next(iter(DS.KAIYAKU_STAGES))


def _deal(stage=LIVE, code="RL0001", name="取引"):
    return {"dealstage": stage, DS.PROP_CODE: code, "dealname": name}


def _memo(*items, asof="2026-08-06"):
    L = [ROLLUP_SIGNATURE, "この取引に紐づく求人へ自動転記されます。", "",
         "■ 求人によって異なる条件"]
    for field, val, label in items:
        L += [f"　{field}:", f"　　{val}　← {label}"]
    L += ["", f"（出典: 求人3件のメモを {asof} に集約）"]
    return "\n".join(L)


def _state(**kw):
    """{deal_id: 本文} → existing_rollup_state 相当."""
    return {d: {"note_id": f"N{d}", "hash": "h", "body": b}
            for d, b in kw.items()}


# --------------------------------------------------------------------------
# 1. 本体: 跡地 → 生きている取引
# --------------------------------------------------------------------------
def test_跡地のメモが生きている取引へ渡る():
    """★これが今回の目的。実測125グループで届いていなかった."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "トレーラー")))
    p = IC.plan_inherit(deals, st, TODAY)
    assert len(p["write"]) == 1
    w = p["write"][0]
    assert w["deal_id"] == "NEW" and "~55歳" in w["body"]
    assert w["引継元"] == ["OLD"]


def test_解約済からも引き継ぐ():
    """継続済だけでなく解約済にもメモは溜まっている."""
    deals = {"OLD": _deal(KAIYAKU), "NEW": _deal(LIVE)}
    p = IC.plan_inherit(deals, _state(OLD=_memo(("必須資格", "大型免許", "A職"))),
                        TODAY)
    assert "大型免許" in p["write"][0]["body"]


def test_跡地が複数でも全部集める():
    """契約が何度も更新されていれば跡地も複数ある."""
    deals = {"O1": _deal(KEIZOKU), "O2": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(O1=_memo(("年齢上限", "~55歳", "A職")),
                O2=_memo(("必須資格", "大型免許", "B職")))
    w = IC.plan_inherit(deals, st, TODAY)["write"][0]
    assert "~55歳" in w["body"] and "大型免許" in w["body"]
    assert sorted(w["引継元"]) == ["O1", "O2"]


def test_新しい取引へNoteが無ければ作る():
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    w = IC.plan_inherit(deals, _state(OLD=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)["write"][0]
    assert w["note_id"] == "", "既存Noteが無いので新規作成になる"


# --------------------------------------------------------------------------
# 2. 生存側の既存メモを消さない
# --------------------------------------------------------------------------
def test_生存側の既存メモが消えない():
    """★単純コピーにすると、新しい取引で書かれた内容が上書きで消える."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "A職")),
                NEW=_memo(("必須資格", "フォークリフト", "C職")))
    body = IC.plan_inherit(deals, st, TODAY)["write"][0]["body"]
    assert "~55歳" in body, "跡地の内容が入る"
    assert "フォークリフト" in body, "生存側の内容が消えない"


def test_同じ項目で値が違えば両方を残す():
    """★契約グループのマージでは**省略しない** (2026-08-21 ユーザー指示)。

    1対1の合流(merge_bodies)なら「古い←新しい」が決まるので新を採り旧は履歴へ
    落とせる。だが同じ契約の中では、跡地と生存のどちらが正しいかは決まらない。
    片方だけ更新されもう片方は未更新、という状態が普通に起きるため、両方を
    残して人が判断できるようにする。"""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "A職")),
                NEW=_memo(("年齢上限", "~60歳", "A職")))
    body = IC.plan_inherit(deals, st, TODAY)["write"][0]["body"]
    from scripts.job_application_sync import rollup_merge as M
    vals = {x[0] for x in M.parse_body(body)["varying"]["年齢上限"]}
    assert vals == {"~55歳", "~60歳"}, "どちらかが消えている"


def test_実行前の本文を必ず持つ():
    """★ロールバックできない変更を本番へ入れない."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "A職")),
                NEW=_memo(("必須資格", "大型免許", "B職")))
    w = IC.plan_inherit(deals, st, TODAY)["write"][0]
    assert "大型免許" in w["before_body"]
    assert w["before_len"] == len(w["before_body"])


# --------------------------------------------------------------------------
# 3. 生存が複数のグループ (本体＋オプション等)
# --------------------------------------------------------------------------
def test_生存が複数なら全部に貼る():
    """★求人はどの取引にもぶら下がりうる。片方だけだとその求人経由の応募に
    メモが届かない (2026-08-21 ユーザー指示「継承して欲しい」)。"""
    deals = {"OLD": _deal(KEIZOKU), "A": _deal(LIVE), "B": _deal(LIVE2)}
    p = IC.plan_inherit(deals, _state(OLD=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)
    assert {w["deal_id"] for w in p["write"]} == {"A", "B"}
    assert all("~55歳" in w["body"] for w in p["write"])


def test_生存が複数でも同じ本文を貼る():
    """別々の内容を貼ると、どちらを見たかで判断が変わってしまう."""
    deals = {"OLD": _deal(KEIZOKU), "A": _deal(LIVE), "B": _deal(LIVE2)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "A職")),
                A=_memo(("必須資格", "大型免許", "B職")))
    bodies = {w["body"] for w in IC.plan_inherit(deals, st, TODAY)["write"]}
    assert len(bodies) == 1


def test_片方だけ更新されていても両方の内容が残る():
    """★これが今回の要点。片方は更新済み・もう片方は未更新という状態で、
    どちらかを落とすと現場が見る本文から条件が消える。"""
    deals = {"OLD": _deal(KEIZOKU), "A": _deal(LIVE, name="本体"),
             "B": _deal(LIVE2, name="オプション")}
    st = _state(OLD=_memo(("年齢上限", "~50歳", "トレーラー")),
                A=_memo(("年齢上限", "~60歳", "トレーラー")),   # 更新された方
                B=_memo(("年齢上限", "~50歳", "トレーラー")))   # 未更新の方
    body = IC.plan_inherit(deals, st, TODAY)["write"][0]["body"]
    assert "~60歳" in body and "~50歳" in body, "片方が消えている"


def test_食い違いには由来の取引名が付く():
    """どちらに従うか人が判断できるよう、どの取引由来かを示す."""
    deals = {"OLD": _deal(KEIZOKU, name="旧契約"),
             "A": _deal(LIVE, name="本体"), "B": _deal(LIVE2, name="オプション")}
    st = _state(OLD=_memo(("年齢上限", "~50歳", "トレーラー")),
                A=_memo(("年齢上限", "~60歳", "トレーラー")))
    body = IC.plan_inherit(deals, st, TODAY)["write"][0]["body"]
    assert "取引:" in body


# --------------------------------------------------------------------------
# 4. 何もしない条件
# --------------------------------------------------------------------------
def test_生きている取引が無ければ何もしない():
    """全部が跡地。貼る先が無い."""
    deals = {"O1": _deal(KEIZOKU), "O2": _deal(KAIYAKU)}
    p = IC.plan_inherit(deals, _state(O1=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)
    assert not p["write"] and not p["deferred"]


def test_跡地にメモが無ければ何もしない():
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    assert not IC.plan_inherit(deals, {}, TODAY)["write"]


def test_生存側にしか無ければ何もしない():
    """引き継ぐ元が無い。生存側のメモをそのまま貼り直すのは無駄な書き込み."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    p = IC.plan_inherit(deals, _state(NEW=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)
    assert not p["write"]


def test_取引先コードが無ければ対象外():
    """★コードが無い取引を勝手に束ねない。別の契約を混ぜると事故になる."""
    deals = {"OLD": _deal(KEIZOKU, code=""), "NEW": _deal(LIVE, code="")}
    p = IC.plan_inherit(deals, _state(OLD=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)
    assert not p["write"] and p["groups"] == 0


def test_別のコードは混ざらない():
    deals = {"OLD": _deal(KEIZOKU, code="RL0001"),
             "NEW": _deal(LIVE, code="RL0002")}
    assert not IC.plan_inherit(
        deals, _state(OLD=_memo(("年齢上限", "~55歳", "A職"))), TODAY)["write"]


def test_不明ステージは貼り先にしない():
    """未知のステージは終了ステージかもしれない。書き込み対象に倒さない."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal("999999999")}
    p = IC.plan_inherit(deals, _state(OLD=_memo(("年齢上限", "~55歳", "A職"))),
                        TODAY)
    assert not p["write"] and not p["deferred"]


# --------------------------------------------------------------------------
# 5. 冪等性 — 毎晩走る
# --------------------------------------------------------------------------
def test_二度流しても増えない():
    """★冪等でなければ毎晩本文が膨らみ、現場が読めなくなる."""
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    st = _state(OLD=_memo(("年齢上限", "~55歳", "トレーラー")))
    p1 = IC.plan_inherit(deals, st, TODAY)
    body1 = p1["write"][0]["body"]
    st2 = dict(st)
    st2["NEW"] = {"note_id": "N1", "hash": "h", "body": body1}
    p2 = IC.plan_inherit(deals, st2, "2026-09-01")
    assert p2["nochange"] == 1 or not p2["write"], "同じ内容なら書かない"
    if p2["write"]:
        assert p2["write"][0]["body"].count("~55歳") == 1


def test_変化が無ければ書き込み対象にしない():
    deals = {"OLD": _deal(KEIZOKU), "NEW": _deal(LIVE)}
    same = _memo(("年齢上限", "~55歳", "A職"))
    p = IC.plan_inherit(deals, _state(OLD=same, NEW=same), TODAY)
    assert not p["write"] and p["nochange"] == 1


# --------------------------------------------------------------------------
# 6. 取得漏れの検知
# --------------------------------------------------------------------------
def test_取引の取得漏れで落ちる(monkeypatch):
    """★少ない件数で走らせると、あるはずのメモを見落として『引き継ぎ不要』と
    誤判定する。黙って通さない."""
    def fake(url, body, **kw):
        if body.get("limit") == 1:
            return {"total": 500}
        return {"results": [{"id": "D1", "properties": {}}]}

    monkeypatch.setattr(IC, "post_retry", fake)
    with pytest.raises(RuntimeError, match="取得漏れ"):
        IC.load_nouhin_deals()
