"""取引メモ → 求人 転記 (作り直し版) のテスト (2026-08-31)。

## なぜ作ったか

旧実装は毎晩 1,800件前後の転記Noteを作り、同じ求人に最大45件が溜まっていた。
テストが1つも無く、3つの不具合 (印が消える / 作り直し+削除 / (取引,求人)ごとに
作る) が本番で7晩以上気づかれなかった。

## このテストが守る性質

1. 1求人=1本文。親が何件でも書くのは1回
2. 生きている取引を優先。跡地は生存が無いときだけ
3. ★別の取引先コードが混在したら書かない (A社の基準がB社の求人に載る事故)
4. 既存があれば PATCH、無ければ作成。削除しない
5. 本文で比較する。出典の違いは無視、1文字の違いは検知
6. ★応募へのコピーを転記メモと誤認しない
7. 二度流しても増えない
"""
from __future__ import annotations

from scripts.job_application_sync import deal_stages as DS
from scripts.job_application_sync import sync_deal_memo_to_listing as S
from scripts.job_application_sync.notes import (
    COPIED_NOTE_MARKER, ROLLUP_SIGNATURE, TRANSFER_SIGNATURE)

LIVE = "52016155"
LIVE2 = "1049738304"
ENDED = next(iter(DS.ENDED_ONLY))
TODAY = "2026-08-31"


def _memo(age="~55歳", extra=""):
    return (f"{ROLLUP_SIGNATURE}\nこの取引に紐づく求人へ自動転記されます。\n\n"
            f"■ 全求人に共通\n〔足切り基準〕\n　年齢: {age}\n{extra}"
            f"（出典: 求人3件のメモを 2026-08-06 に集約）")


def _deal(memo=None, code="RL0001", live=True, name="取引A"):
    return {"memo": memo or _memo(), "name": name, "code": code,
            "live": live, "stage": "求人出稿完了" if live else "継続済"}


def _note(nid, body, created="2026-08-20T00:00:00Z"):
    return {"note_id": nid, "body": body, "created": created}


def _stored(body: str) -> str:
    """HubSpot に保存された形 (改行→<br>、印は消える) を模す。"""
    import re
    return re.sub(r"<!--memo:[0-9a-f]+-->", "", body).replace("\n", "<br>")


OPEN = {"status": "公開中", "name": "求人X"}
CLOSED = {"status": "公開終了", "name": "求人Y"}


def _plan(memos, d2l, notes=None, status=None, **kw):
    status = status or {l: OPEN for ls in d2l.values() for l in ls}
    return S.plan_transfers(memos, d2l, status, notes or {}, today=TODAY, **kw)


# --------------------------------------------------------------------------
# 1. 作成 / 更新 / 変化なし
# --------------------------------------------------------------------------
def test_既存が無ければ作る():
    p = _plan({"D": _deal()}, {"D": ["L"]})
    assert [r["listing_id"] for r in p["create"]] == ["L"]
    assert not p["patch"] and not p["nochange"]
    assert TRANSFER_SIGNATURE in p["create"][0]["body"]
    assert "~55歳" in p["create"][0]["body"]


def test_既存が同じ内容なら触らない():
    body = S.transfer_body(_memo(), "取引A")
    p = _plan({"D": _deal()}, {"D": ["L"]}, {"L": [_note("N1", _stored(body))]})
    assert p["nochange"] == ["L"] and not p["patch"] and not p["create"]


def test_既存が違う内容なら既存をPATCHする():
    """★作り直さない。関連付けが保たれ、増えない."""
    old = S.transfer_body(_memo("~50歳"), "取引A")
    p = _plan({"D": _deal(_memo("~55歳"))}, {"D": ["L"]}, {"L": [_note("N1", _stored(old))]})
    assert [r["note_id"] for r in p["patch"]] == ["N1"]
    assert "~55歳" in p["patch"][0]["body"]
    assert p["patch"][0]["before_body"] == _stored(old), "戻せるように前の本文を持つ"
    assert not p["create"]


def test_印が消えていても比較できる():
    """★旧実装の敗因。保存時に <!--memo:--> が消えても、本文で比べれば同じと分かる."""
    body = S.transfer_body(_memo(), "取引A")
    stored = body.replace("\n", "<br>")   # 印を消さずに保存されたと仮定しても
    p1 = _plan({"D": _deal()}, {"D": ["L"]}, {"L": [_note("N1", stored)]})
    p2 = _plan({"D": _deal()}, {"D": ["L"]}, {"L": [_note("N1", _stored(body))]})
    assert p1["nochange"] == ["L"] and p2["nochange"] == ["L"]


def test_出典が違うだけなら同じ扱い():
    """親の取引名が変わっても内容が同じなら書き直さない (毎晩の無駄を防ぐ)."""
    old = S.transfer_body(_memo(), "旧・取引A")
    p = _plan({"D": _deal(name="新・取引A")}, {"D": ["L"]}, {"L": [_note("N1", _stored(old))]})
    assert p["nochange"] == ["L"]


def test_一文字違えば更新する():
    old = S.transfer_body(_memo("~55歳"), "取引A")
    p = _plan({"D": _deal(_memo("~56歳"))}, {"D": ["L"]}, {"L": [_note("N1", _stored(old))]})
    assert [r["note_id"] for r in p["patch"]] == ["N1"]


# --------------------------------------------------------------------------
# 2. ★1求人=1本文 — 増殖の根
# --------------------------------------------------------------------------
def test_跡地と生存の両方に紐づいても書くのは1回():
    """★旧実装は (取引,求人) ごとに作り、一晩で +1 していた."""
    memos = {"OLD": _deal(live=False, name="旧"), "NEW": _deal(live=True, name="新")}
    p = _plan(memos, {"OLD": ["L"], "NEW": ["L"]})
    assert len(p["create"]) == 1
    assert p["create"][0]["出典取引"] == ["NEW"], "生存を優先"


def test_生存が無ければ跡地から転記する():
    """relink 未了で跡地にしか紐づいていない公開中求人にもメモを届ける."""
    p = _plan({"OLD": _deal(live=False)}, {"OLD": ["L"]})
    assert len(p["create"]) == 1 and p["create"][0]["出典取引"] == ["OLD"]


def test_跡地と生存で本文が違えば生存を採る():
    """継承で生存側に積み上がった内容が正。跡地の古い内容で上書きしない."""
    memos = {"OLD": _deal(_memo("~50歳"), live=False),
             "NEW": _deal(_memo("~55歳"), live=True)}
    p = _plan(memos, {"OLD": ["L"], "NEW": ["L"]})
    assert "~55歳" in p["create"][0]["body"] and "~50歳" not in p["create"][0]["body"]


def test_生存が複数で本文が違えば両方残す():
    """同じ契約 (本体＋オプション) で片方だけ更新された状態。省略しない."""
    memos = {"A": _deal(_memo("~55歳"), name="本体"),
             "B": _deal(_memo("~60歳"), name="オプション", live=True)}
    p = _plan(memos, {"A": ["L"], "B": ["L"]})
    body = p["create"][0]["body"]
    assert "~55歳" in body and "~60歳" in body


def test_生存が複数で本文が同じなら1本文():
    memos = {"A": _deal(name="本体"), "B": _deal(name="オプション")}
    p = _plan(memos, {"A": ["L"], "B": ["L"]})
    assert len(p["create"]) == 1


def test_別の取引先コードが混在したら書かずに人へ回す():
    """★A社の足切り基準がB社の求人に載る事故。実測6件 (誤紐付けの残骸)."""
    memos = {"A": _deal(_memo("~55歳"), code="RL0001", name="A社"),
             "B": _deal(_memo("~40歳"), code="RL0002", name="B社")}
    p = _plan(memos, {"A": ["L"], "B": ["L"]})
    assert not p["create"] and not p["patch"]
    assert [d["listing_id"] for d in p["deferred"]] == ["L"]
    assert {t["取引先コード"] for t in p["deferred"][0]["取引"]} == {"RL0001", "RL0002"}


def test_別コードでも跡地側なら生存優先で解決する():
    """跡地が別コードでも、生存が1契約だけなら混在にならない."""
    memos = {"OLD": _deal(code="RL0001", live=False), "NEW": _deal(code="RL0002", live=True)}
    p = _plan(memos, {"OLD": ["L"], "NEW": ["L"]})
    assert len(p["create"]) == 1 and not p["deferred"]


# --------------------------------------------------------------------------
# 3. 既存が複数あるとき (溜まった重複)
# --------------------------------------------------------------------------
def test_既存が複数なら一致するものを残し他は余剰():
    body = S.transfer_body(_memo(), "取引A")
    old = S.transfer_body(_memo("~50歳"), "取引A")
    notes = {"L": [_note("N1", _stored(old), "2026-08-20T00:00:00Z"),
                   _note("N2", _stored(body), "2026-08-21T00:00:00Z"),
                   _note("N3", _stored(old), "2026-08-22T00:00:00Z")]}
    p = _plan({"D": _deal()}, {"D": ["L"]}, notes)
    assert p["nochange"] == ["L"]
    assert sorted(p["surplus"]["L"]) == ["N1", "N3"]


def test_一致するものが無ければ最新をPATCHし他は余剰():
    old = S.transfer_body(_memo("~50歳"), "取引A")
    notes = {"L": [_note("N1", _stored(old), "2026-08-20T00:00:00Z"),
                   _note("N2", _stored(old), "2026-08-25T00:00:00Z")]}
    p = _plan({"D": _deal(_memo("~55歳"))}, {"D": ["L"]}, notes)
    assert [r["note_id"] for r in p["patch"]] == ["N2"], "最新を残す"
    assert p["surplus"]["L"] == ["N1"]


def test_余剰は日次では消さない():
    """掃除は --cleanup だけ。計画には surplus として載るが create/patch には影響しない."""
    body = S.transfer_body(_memo(), "取引A")
    notes = {"L": [_note("N1", _stored(body)), _note("N2", _stored(body), "2026-08-22T00:00:00Z")]}
    p = _plan({"D": _deal()}, {"D": ["L"]}, notes)
    assert p["nochange"] == ["L"] and "L" in p["surplus"]


# --------------------------------------------------------------------------
# 4. ★応募へのコピーを転記メモと誤認しない
# --------------------------------------------------------------------------
def test_応募コピーは転記メモとして数えない():
    """②は転記本文を丸ごと複製するので同じ署名を含む。実測 395件。
    これを余剰と誤認して消すと、一次対応の応募カードからメモが消える."""
    body = S.transfer_body(_memo(), "取引A")
    copied = COPIED_NOTE_MARKER + "<br>" + _stored(body)
    assert S.is_transfer_note(_stored(body)) is True
    assert S.is_transfer_note(copied) is False
    assert S.is_transfer_note("人が書いたメモ") is False


# --------------------------------------------------------------------------
# 5. 公開終了・冪等性
# --------------------------------------------------------------------------
def test_公開終了の求人は既定で触らない():
    p = _plan({"D": _deal()}, {"D": ["L"]}, status={"L": CLOSED})
    assert not p["create"] and p["skipped_closed"] == 1


def test_include_closedで公開終了にも書く():
    p = _plan({"D": _deal()}, {"D": ["L"]}, status={"L": CLOSED}, include_closed=True)
    assert len(p["create"]) == 1


def test_二度流しても増えない():
    """★1回目の結果を保存された形にして2回目へ渡すと、何も起きない."""
    memos = {"OLD": _deal(_memo("~50歳"), live=False), "NEW": _deal(_memo("~55歳"))}
    d2l = {"OLD": ["L"], "NEW": ["L"]}
    p1 = _plan(memos, d2l)
    assert len(p1["create"]) == 1
    notes = {"L": [_note("N1", _stored(p1["create"][0]["body"]))]}
    p2 = _plan(memos, d2l, notes)
    assert not p2["create"] and not p2["patch"] and p2["nochange"] == ["L"]
    assert "L" not in p2["surplus"]


def test_transfer_bodyに印を埋めない():
    """印は HubSpot が消す。埋めても意味が無く、残っていると比較を混乱させる."""
    assert "<!--" not in S.transfer_body(_memo(), "取引A")
