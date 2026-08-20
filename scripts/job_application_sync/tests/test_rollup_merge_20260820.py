"""集約メモのマージ（積み上げ）の回帰テスト (2026-08-20)。

## なぜ作ったか

集約メモの日次化が「毎晩すべての求人を読み直して作り直す」実装になっており、
**求人の出し直しで失われたメモを毎晩取引側へ取り込んでいた**。

実測 (サンプル60取引すべてで縮小):

    取引 56896732200: 1,267字 → 200字 (-1,067)
    取引 58134781822: 1,051字 → 156字 (-895)

消えるのは年齢上限・必須資格・想定NG など、一次対応の担当者が電話中に見る
足切り基準そのもの。「メモの正を取引に置く」と決めた理由が、まさに
「求人は作り替えられて失われるから」なので、作り直しは設計と矛盾していた。

## 確定した方針 (2026-08-20 ユーザー判断)

- **C案**: 同じ項目で値が違えば新しい値を採り、古い値は履歴へ残す
  （「同じ条件とは限らない。だから残す。最終的には人間が編集して判断する」）
- **F案**: 今回出てこなかった項目は消さず、「いつ以降 未更新か」を添える

## このテストが守る性質

1. **情報が減らない**（既存にあって今回無いものが消えない）
2. **毎晩書き換わらない**（同じ入力なら本文が変わらない＝no-op）
3. 値が変わったら新しい方が本文に出て、古い方は履歴に残る
4. 未更新の日付は一度付いたら動かない（動くと毎晩の書き換えになる）
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import rollup_merge as M
from scripts.job_application_sync.notes import ROLLUP_SIGNATURE

TODAY = "2026-08-20"


def body(common=(), varying=(), none=(), asof="2026-08-06", n=4):
    """build_rollup_body と同じ書式の本文を組み立てる（テスト用）."""
    L = [ROLLUP_SIGNATURE, "この取引に紐づく求人へ自動転記されます。", ""]
    if common:
        L.append("■ 全求人に共通")
        for g, k, v in common:
            if g:
                L.append(f"〔{g}〕")
            L.append(f"　{k}: {v}")
        L.append("")
    if varying:
        L.append("■ 求人によって異なる条件")
        for f, items in varying:
            L.append(f"　{f}:")
            for v, lab in items:
                L.append(f"　　{v}　← {lab}")
        L.append("")
    if none:
        L += ["■ 制限・指定なし（全求人共通）", "　" + " / ".join(none), ""]
    L.append(f"（出典: 求人{n}件のメモを {asof} に集約）")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 1. 情報が減らない — これが今回の事故の本体
# --------------------------------------------------------------------------
def test_今回出てこない項目が消えない():
    """★実測で1,267字→200字。年齢上限・必須資格が丸ごと消えていた."""
    old = body(varying=[("年齢上限", [("~55歳", "トレーラードライバー")]),
                        ("必須資格", [("大型免許、けん引", "トレーラードライバー")])])
    new = body(common=[("書類回収ルール", "必要書類", "履歴書")], varying=[])
    out = M.merge_bodies(old, new, TODAY)
    assert "~55歳" in out and "大型免許、けん引" in out, "既存の足切り基準が消えた"
    assert "履歴書" in out, "今回の内容も入る"


def test_消えなかった項目には未更新の印が付く():
    """F案。古い情報が黙って居座ると誤った足切りをする."""
    old = body(varying=[("年齢上限", [("~55歳", "トレーラー")])], asof="2026-08-06")
    out = M.merge_bodies(old, body(), TODAY)
    assert "（2026-08-06 以降 未更新）" in out


def test_今回確認できた項目には印を付けない():
    """★現役のものに日付を付けると、毎晩全行が変わって全件書き換えになる."""
    old = body(common=[("優先順位", "急ぎ度", "通常")])
    out = M.merge_bodies(old, body(common=[("優先順位", "急ぎ度", "通常")]), TODAY)
    assert "未更新" not in out


def test_共通の項目も消えない():
    old = body(common=[("書類回収ルール", "回収タイミング", "一次面接前")])
    out = M.merge_bodies(old, body(common=[("優先順位", "急ぎ度", "通常")]), TODAY)
    assert "一次面接前" in out and "通常" in out


def test_既存が空なら今回の内容をそのまま使う():
    new = body(common=[("優先順位", "急ぎ度", "通常")])
    assert M.merge_bodies("", new, TODAY) == new
    assert M.merge_bodies("   ", new, TODAY) == new


# --------------------------------------------------------------------------
# 2. 毎晩書き換わらない — no-op であること
# --------------------------------------------------------------------------
def test_同じ入力なら中身が変わらない():
    """★ここが崩れると毎晩452件を書き換え、Noteの更新日時が無意味になる."""
    b = body(common=[("優先順位", "急ぎ度", "通常")],
             varying=[("年齢上限", [("~55歳", "A職"), ("~60歳", "B職")])])
    once = M.merge_bodies(b, b, TODAY)
    twice = M.merge_bodies(once, b, "2026-09-01")
    assert M.parse_body(once) == M.parse_body(twice)


def test_未更新の日付は後から動かない():
    """一度付いた日付が毎晩今日に変わると、それ自体が書き換えになる."""
    old = body(varying=[("年齢上限", [("~55歳", "A職")])], asof="2026-08-06")
    d1 = M.merge_bodies(old, body(), "2026-08-20")
    d2 = M.merge_bodies(d1, body(), "2026-09-15")
    assert "（2026-08-06 以降 未更新）" in d2
    assert "2026-09-15" not in d2


def test_三日連続で回しても増殖しない():
    old = body(varying=[("年齢上限", [("~55歳", "A職")])])
    cur = old
    for day in ("2026-08-20", "2026-08-21", "2026-08-22"):
        cur = M.merge_bodies(cur, body(), day)
    assert cur.count("~55歳") == 1


# --------------------------------------------------------------------------
# 3. 値が変わったとき — C案
# --------------------------------------------------------------------------
def test_値が変われば新しい方が本文に出る():
    old = body(varying=[("年齢上限", [("~55歳", "トレーラー")])])
    new = body(varying=[("年齢上限", [("~60歳", "トレーラー")])])
    out = M.merge_bodies(old, new, TODAY)
    v = M.parse_body(out)["varying"]["年齢上限"]
    assert [x[0] for x in v] == ["~60歳"], "本文には新しい値だけを出す"


def test_古い値は履歴に残る():
    """★「同じ条件とは限らない。だから残す。人間が最終判断する」(ユーザー)."""
    old = body(varying=[("年齢上限", [("~55歳", "トレーラー")])], asof="2026-08-06")
    new = body(varying=[("年齢上限", [("~60歳", "トレーラー")])])
    out = M.merge_bodies(old, new, TODAY)
    h = M.parse_body(out)["history"]
    assert any(x["value"] == "~55歳" and x["field"] == "年齢上限" for x in h)
    assert M.HISTORY_HEAD in out


def test_履歴の見出しに人がやることが書いてある():
    old = body(common=[("優先順位", "急ぎ度", "急ぎ")])
    new = body(common=[("優先順位", "急ぎ度", "通常")])
    out = M.merge_bodies(old, new, TODAY)
    assert "人が確認して整理" in out


def test_共通項目の変更も履歴に残る():
    old = body(common=[("優先順位", "急ぎ度", "急ぎ")])
    new = body(common=[("優先順位", "急ぎ度", "通常")])
    out = M.merge_bodies(old, new, TODAY)
    assert "急ぎ度: 通常" in out
    assert any(x["value"] == "急ぎ" for x in M.parse_body(out)["history"])


def test_同じ値なら履歴に積まない():
    b = body(varying=[("年齢上限", [("~55歳", "A職")])])
    assert not M.parse_body(M.merge_bodies(b, b, TODAY))["history"]


def test_履歴は項目ごとに上限で打ち切る():
    """増え続けると本文が読めなくなる。読み手は一次対応中の担当者."""
    cur = body(varying=[("年齢上限", [("~50歳", "A職")])])
    for i, v in enumerate(["~51歳", "~52歳", "~53歳", "~54歳", "~55歳"]):
        cur = M.merge_bodies(
            cur, body(varying=[("年齢上限", [(v, "A職")])]),
            f"2026-09-0{i + 1}")
    h = [x for x in M.parse_body(cur)["history"] if x["field"] == "年齢上限"]
    assert len(h) <= M.HISTORY_MAX


def test_別ラベルは別物として両方残す():
    """職種が違えば条件が違って当然。片方で上書きしない."""
    old = body(varying=[("年齢上限", [("~55歳", "トレーラー")])])
    new = body(varying=[("年齢上限", [("~40歳", "事務")])])
    out = M.merge_bodies(old, new, TODAY)
    vals = {x[0] for x in M.parse_body(out)["varying"]["年齢上限"]}
    assert vals == {"~55歳", "~40歳"}


# --------------------------------------------------------------------------
# 4. 解析（parse_body）の頑健さ
# --------------------------------------------------------------------------
def test_HTMLのbrタグを読める():
    """HubSpotのNote本文は <br> 区切りで返る."""
    b = body(common=[("優先順位", "急ぎ度", "通常")]).replace("\n", "<br>")
    assert M.parse_body(b)["common"][("優先順位", "急ぎ度")][0] == "通常"


def test_出典日を読める():
    assert M.parse_body(body(asof="2026-08-06"))["asof"] == "2026-08-06"


def test_出典行が無くても落ちない():
    assert M.parse_body(ROLLUP_SIGNATURE)["asof"] == ""


def test_制限なしの項目が現役に戻れば外れる():
    """「なし」と書かれていた項目に値が入ったら、制限なし欄から消す."""
    old = body(none=["年齢上限", "学歴NG"])
    new = body(varying=[("年齢上限", [("~55歳", "A職")])])
    out = M.merge_bodies(old, new, TODAY)
    p = M.parse_body(out)
    assert "年齢上限" not in p["none"] and "学歴NG" in p["none"]


def test_署名が残る():
    """署名が消えると集約メモとして検出できなくなり、二重に積まれる."""
    out = M.merge_bodies(body(common=[("優先順位", "急ぎ度", "通常")]),
                         body(common=[("優先順位", "急ぎ度", "急ぎ")]), TODAY)
    assert ROLLUP_SIGNATURE in out


def test_転記の案内文が残る():
    """求人側に「修正は取引側で」と案内する文。消すと運用が崩れる."""
    out = M.merge_bodies(body(common=[("優先順位", "急ぎ度", "通常")]),
                         body(common=[("優先順位", "急ぎ度", "急ぎ")]), TODAY)
    assert "自動転記されます" in out


# --------------------------------------------------------------------------
# 5. 実データ由来の再現（取引 56896732200 の縮小事故）
# --------------------------------------------------------------------------
def test_実データの縮小事故が再現しない():
    """★1,267字→200字 の事故。マージ後は既存の足切り基準が全部残ること."""
    old = body(varying=[
        ("年齢上限", [("~55歳", "トレーラー（完成車配送）ドライバー")]),
        ("必須資格", [("大型免許、けん引", "トレーラー（完成車配送）ドライバー"),
                      ("フォークリフト、大型免許", "コンテナ作業スタッフ")]),
        ("想定NG・特殊事情", [("免許なし、経験なくて年齢高い", "中型配送ドライバー")]),
        ("顧客が重視するポイント", [("年齢、経験、保有資格", "中型配送ドライバー")]),
    ], asof="2026-08-06")
    new = body(common=[("書類回収ルール", "必要書類", "履歴書 / 職務経歴書"),
                       ("書類回収ルール", "回収タイミング", "一次面接前"),
                       ("一次対応で聞くこと", "面接希望時期（曜日・時間帯）", "3つほど回収")])
    out = M.merge_bodies(old, new, TODAY)
    for keep in ("~55歳", "大型免許、けん引", "フォークリフト、大型免許",
                 "免許なし、経験なくて年齢高い", "年齢、経験、保有資格"):
        assert keep in out, f"消えてはいけない値が消えた: {keep}"
    assert "履歴書 / 職務経歴書" in out and "3つほど回収" in out
    assert len(out) > len(new), "マージ後が今回ぶんより短いのはおかしい"
