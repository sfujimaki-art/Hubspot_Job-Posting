# -*- coding: utf-8 -*-
"""シートURLを取引から辿る経路のテスト (2026-09-09)。

守る性質:
1. 取引にURLがあれば、求人票にURLが無くてもその配下の応募が転記される
2. 移行期は両方を見る (求人票側だけにある古いデータも拾う)
3. 両方から同じ求人票が来ても二重にならない
4. 取引にURLが無くても、求人票側にあれば従来どおり動く
"""
from __future__ import annotations

from scripts.job_application_sync import customer_sheet_sync as C

SHEET = "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _fake_hs(monkeypatch, listings_by_prop, assoc):
    """_post_hs を差し替える。listings_by_prop: {(obj, prop): [id]}"""
    calls = []

    def fake(url, body, **_kw):
        if url.endswith("/search"):
            obj = url.split("/objects/")[1].split("/")[0]
            prop = body["filterGroups"][0]["filters"][0]["propertyName"]
            calls.append((obj, prop))
            return {"results": [{"id": i}
                                for i in listings_by_prop.get((obj, prop), [])]}
        if "associations/0-3/0-420" in url:
            return {"results": [{"from": {"id": x["id"]},
                                 "to": [{"toObjectId": l}
                                        for l in assoc.get(x["id"], [])]}
                                for x in body["inputs"]]}
        raise AssertionError(f"想定外のURL: {url}")

    monkeypatch.setattr(C, "_post_hs", fake)
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    return calls


def test_取引から求人票を辿れる(monkeypatch):
    """★求人票にURLが無くても、親の取引にあれば転記対象になる (移行の目的)."""
    _fake_hs(monkeypatch, {("0-3", "customer_sheet_url"): ["D1"]},
             {"D1": ["L1", "L2"]})
    assert C._listing_ids_via_deal(SHEET) == {"L1", "L2"}


def test_求人票側の直接指定も拾う(monkeypatch):
    """移行期。求人票側にだけ入っている古いデータを落とさない."""
    _fake_hs(monkeypatch, {("0-420", "customer_sheet_url"): ["L9"]}, {})
    assert C._listing_ids_direct(SHEET) == {"L9"}


def test_両方から来ても重複しない(monkeypatch):
    """同じ求人票が両経路から来る (移行直後の通常状態)。集合なので1つ."""
    _fake_hs(monkeypatch,
             {("0-3", "customer_sheet_url"): ["D1"],
              ("0-420", "customer_sheet_url"): ["L1"]},
             {"D1": ["L1", "L2"]})
    got = C._listing_ids_via_deal(SHEET) | C._listing_ids_direct(SHEET)
    assert got == {"L1", "L2"}


def test_取引にURLが無ければ求人票側だけで動く(monkeypatch):
    _fake_hs(monkeypatch, {("0-420", "customer_sheet_url"): ["L1"]}, {})
    assert C._listing_ids_via_deal(SHEET) == set()
    assert C._listing_ids_direct(SHEET) == {"L1"}


def test_どちらにも無ければ空(monkeypatch):
    _fake_hs(monkeypatch, {}, {})
    assert not (C._listing_ids_via_deal(SHEET) | C._listing_ids_direct(SHEET))


def test_検索は両方のオブジェクトを見る(monkeypatch):
    """★片方の経路を消す変異を検出する (取引だけ/求人票だけになっていないか)."""
    calls = _fake_hs(monkeypatch,
                     {("0-3", "customer_sheet_url"): ["D1"],
                      ("0-420", "customer_sheet_url"): ["L1"]},
                     {"D1": ["L2"]})
    C._listing_ids_via_deal(SHEET)
    C._listing_ids_direct(SHEET)
    assert ("0-3", "customer_sheet_url") in calls
    assert ("0-420", "customer_sheet_url") in calls


def _fetch(monkeypatch, listings_by_prop, assoc, appts=None):
    """fetch_applicants を丸ごと通す。応募のbatch/readも差し替える。"""
    appts = appts or {}
    calls = []

    def fake(url, body, **_kw):
        if url.endswith("/search"):
            obj = url.split("/objects/")[1].split("/")[0]
            prop = body["filterGroups"][0]["filters"][0]["propertyName"]
            calls.append((obj, prop))
            return {"results": [{"id": i}
                                for i in listings_by_prop.get((obj, prop), [])]}
        if "associations/0-3/0-420" in url:
            return {"results": [{"from": {"id": x["id"]},
                                 "to": [{"toObjectId": l}
                                        for l in assoc.get(x["id"], [])]}
                                for x in body["inputs"]]}
        if "associations/0-420/0-421" in url:
            return {"results": [{"from": {"id": x["id"]},
                                 "to": [{"toObjectId": a}
                                        for a in appts.get(x["id"], [])]}
                                for x in body["inputs"]]}
        if url.endswith("/0-421/batch/read"):
            return {"results": [{"id": x["id"],
                                 "properties": {"hs_object_id": x["id"],
                                                "hs_createdate": "2026-09-09T01:00:00Z"}}
                                for x in body["inputs"]]}
        raise AssertionError(f"想定外のURL: {url}")

    monkeypatch.setattr(C, "_post_hs", fake)
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    got = C.fetch_applicants(SHEET, "2026-09-01T00:00:00Z")
    return [p["hs_object_id"] for p in got], calls


def test_本体は取引経路の応募を返す(monkeypatch):
    """★逆証明Aで発覚: ヘルパー単体だけ見ていると、本体で取引経路を消しても通る."""
    got, _ = _fetch(monkeypatch, {("0-3", "customer_sheet_url"): ["D1"]},
                    {"D1": ["L1"]}, {"L1": ["A1"]})
    assert got == ["A1"], "取引にURLがあれば、その配下の応募が転記対象になる"


def test_本体は求人票経路の応募も返す(monkeypatch):
    """★逆証明Bで発覚: 移行期に求人票側だけの分を落とさない."""
    got, _ = _fetch(monkeypatch, {("0-420", "customer_sheet_url"): ["L9"]},
                    {}, {"L9": ["A9"]})
    assert got == ["A9"]


def test_本体は両経路を合わせ重複させない(monkeypatch):
    got, calls = _fetch(monkeypatch,
                        {("0-3", "customer_sheet_url"): ["D1"],
                         ("0-420", "customer_sheet_url"): ["L1"]},
                        {"D1": ["L1", "L2"]}, {"L1": ["A1"], "L2": ["A2"]})
    assert sorted(got) == ["A1", "A2"], "同じ求人票が両経路から来ても応募は1回"
    assert ("0-3", "customer_sheet_url") in calls
    assert ("0-420", "customer_sheet_url") in calls


def _no_api(monkeypatch):
    """APIを叩いたら即分かるようにする (ガードが効いていれば呼ばれない)。
    ★差し替えないと、ガードを外す変異のときに本番APIを叩く
      (逆証明Dで実際に4分かかった)."""
    def boom(*_a, **_k):
        raise RuntimeError("ガードを抜けてAPIを叩いた")
    monkeypatch.setattr(C, "_post_hs", boom)
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)


def test_短いsheet_idは拒否したまま(monkeypatch):
    """逆証明A5のガードを移行で壊していないこと (別顧客の求人票を拾う事故)。
    ★ValueError 限定で受ける。RuntimeError も許すとガードを外す変異がすり抜ける
      (逆証明Dで発覚)."""
    import pytest
    _no_api(monkeypatch)
    with pytest.raises(ValueError):
        C.fetch_applicants("docs", "2026-09-09T00:00:00Z")


def test_cutoffが空なら止まる(monkeypatch):
    """逆証明A4のガード。空だと過去の全応募が顧客シートへ流入する."""
    import pytest
    _no_api(monkeypatch)
    with pytest.raises(ValueError):
        C.fetch_applicants(SHEET, "")


def test_cutoffがUTC形式でなければ止まる(monkeypatch):
    """'+09:00' 付きを渡すと hs_createdate との文字列比較が壊れる."""
    import pytest
    _no_api(monkeypatch)
    with pytest.raises(ValueError):
        C.fetch_applicants(SHEET, "2026-09-09T00:00:00+09:00")
