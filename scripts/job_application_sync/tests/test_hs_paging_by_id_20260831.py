"""search_all_by_id (hs_object_id カーソルで search を全件回す) のテスト。

## なぜ作ったか

HubSpot の Search API は `after` でページを送ると **10,000件で HTTP 400** を返す。
rollup_memo_to_deal.py の対象探しが 9,800件に達して 2026-08-23 から4晩連続で
クラッシュした。

list API (iter_all) に逃げると上限は無いが、Note は 30万件超あり **35分**
かかる。それを毎晩走る inherit_rollup_by_contract に入れてしまい、
deal_hygiene の60分をほぼ食いつぶす状態にした (2026-08-31 に発見・撤回)。

そこで「前ページの最後の ID より大きいもの」を昇順で取る方式にする。
毎回が独立した検索なので上限に当たらず、速さは search のまま。

## このテストが守る性質

1. カーソルが前ページの最後の ID から進む
2. 10,000件を超えても完走する (これが目的)
3. 空ページで止まる / 端数ページで止まる
4. 同じ ID を二度取らない
5. ★カーソルが進まなければ例外にする (黙って無限ループしない)
6. 呼び出し側が hs_object_id の条件を渡しても二重にならない
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import hs_paging as HP


def _fake_source(total: int, page: int = 100):
    """ID 1..total を持つ偽の HubSpot。GT カーソルに従ってページを返す."""
    calls = []

    def fake_post(url, body, **kw):
        calls.append(body)
        flt = body["filterGroups"][0]["filters"]
        gt = [f for f in flt if f["propertyName"] == "hs_object_id"]
        assert len(gt) == 1, "hs_object_id の条件はちょうど1つ"
        assert gt[0]["operator"] == "GT"
        last = int(gt[0]["value"])
        assert body["sorts"][0]["propertyName"] == "hs_object_id"
        ids = [i for i in range(last + 1, total + 1)][:body["limit"]]
        return {"results": [{"id": str(i), "properties": {}} for i in ids]}

    return fake_post, calls


FILTER = [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
           "value": "暗黙知メモ"}]


# --------------------------------------------------------------------------
# 1. カーソルが進む
# --------------------------------------------------------------------------
def test_カーソルが前ページの最後のIDから進む(monkeypatch):
    fake, calls = _fake_source(250)
    monkeypatch.setattr(HP, "post_retry", fake)
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    out = HP.search_all_by_id("notes", ["hs_note_body"], FILTER)
    assert len(out) == 250
    gts = [int([f for f in c["filterGroups"][0]["filters"]
                if f["propertyName"] == "hs_object_id"][0]["value"])
           for c in calls]
    assert gts == [0, 100, 200], "GT の値が 0 → 100 → 200 と進む"


# --------------------------------------------------------------------------
# 2. ★10,000件を超えても完走する — これが目的
# --------------------------------------------------------------------------
def test_一万件を超えても完走する(monkeypatch):
    fake, calls = _fake_source(10_350)
    monkeypatch.setattr(HP, "post_retry", fake)
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    out = HP.search_all_by_id("notes", ["hs_note_body"], FILTER,
                              progress_every=0)
    assert len(out) == 10_350, "★after 方式なら 10,000 で 400 になる件数"
    assert len(calls) == 104


# --------------------------------------------------------------------------
# 3. 止まり方
# --------------------------------------------------------------------------
def test_端数ページで止まる(monkeypatch):
    """最後が 100件未満なら、次の空ページを取りに行かない (1回節約)."""
    fake, calls = _fake_source(230)
    monkeypatch.setattr(HP, "post_retry", fake)
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    out = HP.search_all_by_id("notes", [], FILTER)
    assert len(out) == 230 and len(calls) == 3


def test_ちょうど割り切れるときは空ページで止まる(monkeypatch):
    fake, calls = _fake_source(200)
    monkeypatch.setattr(HP, "post_retry", fake)
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    out = HP.search_all_by_id("notes", [], FILTER)
    assert len(out) == 200 and len(calls) == 3, "3回目が空で止まる"


def test_ゼロ件なら空を返す(monkeypatch):
    fake, calls = _fake_source(0)
    monkeypatch.setattr(HP, "post_retry", fake)
    assert HP.search_all_by_id("notes", [], FILTER) == [] and len(calls) == 1


# --------------------------------------------------------------------------
# 4. 同じ ID を二度取らない
# --------------------------------------------------------------------------
def test_同じIDが重なって返っても一度しか入れない(monkeypatch):
    """HubSpot 側の索引遅延で境界の1件が重複して返ることがある."""
    pages = [
        [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        [{"id": "3"}, {"id": "4"}],          # 3 が重複
        [],
    ]
    it = iter(pages)
    monkeypatch.setattr(HP, "post_retry", lambda *a, **k: {"results": next(it)})
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    out = HP.search_all_by_id("notes", [], FILTER, page=3)
    assert [o["id"] for o in out] == ["1", "2", "3", "4"]


# --------------------------------------------------------------------------
# 5. ★カーソルが進まなければ例外
# --------------------------------------------------------------------------
def test_カーソルが進まなければ落ちる(monkeypatch):
    """黙って同じページを回すと無限ループになり、CI のタイムアウトまで
    気づけない。進んでいないことを即座に例外にする."""
    monkeypatch.setattr(HP, "post_retry",
                        lambda *a, **k: {"results": [{"id": "5"}, {"id": "6"}]})
    monkeypatch.setattr(HP.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="進まない"):
        HP.search_all_by_id("notes", [], FILTER, page=2)


# --------------------------------------------------------------------------
# 6. 呼び出し側の hs_object_id 条件は捨てる
# --------------------------------------------------------------------------
def test_呼び出し側のhs_object_id条件は二重にしない(monkeypatch):
    fake, calls = _fake_source(50)
    monkeypatch.setattr(HP, "post_retry", fake)
    extra = FILTER + [{"propertyName": "hs_object_id", "operator": "GT",
                       "value": "999"}]
    out = HP.search_all_by_id("notes", [], extra)
    # 呼び出し側の 999 は無視され、0 から回るので全50件取れる
    assert len(out) == 50
