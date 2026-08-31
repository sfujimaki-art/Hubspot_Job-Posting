"""取引→求人→応募のメモ連鎖 3段目の回帰テスト (2026-08-18)。

## なぜ作ったか

暗黙知メモの正を取引に置く設計にした。

    取引(正) ──転記──> 求人 ──コピー──> 応募

1段目・2段目は動いていたが、**3段目が署名の不一致で切れていた**。

| 段 | 実装 | 実測 |
|---|---|---|
| ① 求人メモ→取引に集約 | rollup_memo_to_deal | 454取引 |
| ② 取引→求人へ転記 | sync_deal_memo_to_listing | 公開中1,539件中1,537件 ✅ |
| ③ 求人→応募へコピー | notes.copy_listing_note_to_appointment | **応募に紐付く転記メモ 0件** ❌ |

原因: 転記メモの署名は `📎 取引から転記された暗黙知メモ` なのに、コピー側の
`get_listing_template_note_body()` は `暗黙知入力テンプレート` しか探していなかった。

転記済み求人12件でコピー関数を呼んだ実測:

    10件 → None (応募へ何も渡らない)
     2件 → 本文は返るが、それは**求人に直接書かれた旧メモ**

後者のほうが害が大きい。求人側の転記メモには「修正は取引側で行ってください」と
書いてあるのに、応募には旧メモが届き続け、取引で直した内容が永久に反映されない。

## このテストが守る性質

1. 転記メモがあればそれを応募へ渡す
2. **転記メモと旧メモが両方あれば転記メモを優先する**(取引が正)
3. 転記メモが無いときだけ旧メモにフォールバックする(移行期の互換)
4. どちらも無ければ None(無関係なNoteを応募へ複製しない)
5. 署名の定義はただ1箇所。二重定義に戻さない
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import notes as N

LISTING = "560174532237"


@pytest.fixture
def wire(monkeypatch):
    """求人にぶら下がるNote群を差し替える。戻り値で本文を設定する."""
    store: dict = {}

    def _set(bodies: dict):
        """bodies: {note_id: 本文} — _listing_note_ids の順序をそのまま使う."""
        store.clear()
        store.update(bodies)
        monkeypatch.setattr(N, "_listing_note_ids",
                            lambda lid, **k: list(bodies))
        monkeypatch.setattr(N, "_note_body",
                            lambda nid, **k: bodies.get(nid))
    return _set


def _transfer(extra="面接希望時期を必ず聞く"):
    return (f"{N.TRANSFER_SIGNATURE}\n出典: サブスク継続①＿A社\n"
            f"この内容は取引レコードで管理されています。\n\n{extra}")


def _legacy(extra="旧: 年齢は45歳まで"):
    return f"{N.TEMPLATE_SIGNATURE}\n{extra}"


# --------------------------------------------------------------------------
# 3段目が繋がること
# --------------------------------------------------------------------------
def test_転記メモを応募へ渡す(wire):
    """★これが0件だった。転記メモ1,875件が応募に1件も届いていなかった."""
    wire({"n1": _transfer()})
    body = N.get_listing_template_note_body(LISTING, token="t")
    assert body and N.TRANSFER_SIGNATURE in body


def test_転記メモが旧メモより優先される(wire):
    """★求人側の転記メモは「修正は取引側で」と書いてある。旧メモを渡すと
    その約束が嘘になり、取引で直した内容が永久に反映されない。"""
    wire({"pin": _legacy(), "n2": _transfer()})
    body = N.get_listing_template_note_body(LISTING, token="t")
    assert N.TRANSFER_SIGNATURE in body
    assert N.TEMPLATE_SIGNATURE not in body


def test_順序が逆でも転記メモが勝つ(wire):
    """pin留めの順に依存しない。走査順で結果が変わってはいけない."""
    wire({"n1": _transfer(), "n2": _legacy()})
    assert N.TRANSFER_SIGNATURE in N.get_listing_template_note_body(
        LISTING, token="t")


def test_転記が無ければ旧メモを使う(wire):
    """移行期の互換。まだ転記されていない求人でも応募への提供を止めない."""
    wire({"n1": _legacy()})
    body = N.get_listing_template_note_body(LISTING, token="t")
    assert body and N.TEMPLATE_SIGNATURE in body


def test_どちらも無ければNone(wire):
    """署名の無いNoteを応募へ複製しない (F4)."""
    wire({"n1": "打ち合わせ議事録。来週再訪。"})
    assert N.get_listing_template_note_body(LISTING, token="t") is None


def test_Noteが1件も無ければNone(wire):
    wire({})
    assert N.get_listing_template_note_body(LISTING, token="t") is None


def test_本文が取れないNoteを飛ばす(wire):
    """dangling pin (削除済Noteを指すpin) で止まらない (F3)."""
    wire({"dead": None, "n2": _transfer()})
    assert N.TRANSFER_SIGNATURE in N.get_listing_template_note_body(
        LISTING, token="t")


def test_転記メモの中身がそのまま渡る(wire):
    """署名だけでなく本体(足切り基準・ヒアリング項目)が届くこと."""
    wire({"n1": _transfer("〔一次対応で聞くこと〕面接希望時期（曜日・時間帯）")})
    body = N.get_listing_template_note_body(LISTING, token="t")
    assert "面接希望時期" in body and "曜日・時間帯" in body


# --------------------------------------------------------------------------
# 冪等判定も同じ経路を通ること
# --------------------------------------------------------------------------
def test_転記メモだけでもテンプレ有りと判定する(wire):
    """listing_has_template_note は同じ関数を使う。転記メモを見落とすと
    ②が「まだメモが無い」と誤認して貼り直しを繰り返す."""
    wire({"n1": _transfer()})
    assert N.listing_has_template_note(LISTING, token="t") is True


def test_署名が無ければテンプレ無しと判定する(wire):
    wire({"n1": "ただのメモ"})
    assert N.listing_has_template_note(LISTING, token="t") is False


# --------------------------------------------------------------------------
# 署名の定義が1箇所であること
# --------------------------------------------------------------------------
def test_署名の定義は1箇所():
    """★以前 ROLLUP_SIGNATURE が2ファイルに二重定義されていた。片方を直すと
    もう片方が黙って一致しなくなる。同一オブジェクトであることを固定する."""
    from scripts.job_application_sync import rollup_memo_to_deal as R
    from scripts.job_application_sync import sync_deal_memo_to_listing as S
    assert S.ROLLUP_SIGNATURE is N.ROLLUP_SIGNATURE
    assert R.ROLLUP_SIGNATURE is N.ROLLUP_SIGNATURE
    assert S.TRANSFER_SIGNATURE is N.TRANSFER_SIGNATURE


def test_3つの署名が互いに混ざらない():
    """部分一致で取り違えないこと。どれも他方を含んではいけない."""
    sigs = [N.TEMPLATE_SIGNATURE, N.ROLLUP_SIGNATURE, N.TRANSFER_SIGNATURE]
    for a in sigs:
        for b in sigs:
            if a is not b:
                assert a not in b, f"署名が包含関係にある: {a!r} ⊂ {b!r}"


def test_転記メモは集約メモの署名を含まない():
    """②は本文から ROLLUP_SIGNATURE を除去して貼る。両方を含むと
    取引側の集約メモを数えるコードが求人側の転記まで数えてしまう."""
    from scripts.job_application_sync import sync_deal_memo_to_listing as S
    # ★2026-08-31: 印 (<!--memo:hash-->) は HubSpot が保存時に消すので埋めなくなった。
    #   引数は (取引メモ, 出典) の2つ。
    body = S.transfer_body(f"{N.ROLLUP_SIGNATURE}\n本文", "取引A")
    assert N.ROLLUP_SIGNATURE not in body
    assert N.TRANSFER_SIGNATURE in body
