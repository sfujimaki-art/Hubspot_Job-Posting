# -*- coding: utf-8 -*-
"""応募メモの穴埋め (backfill_appointment_memo) のテスト (2026-09-02)。

守る性質:
1. 求人にメモが届いた後の応募だけがコピー対象になる
2. コピー済み・求人未紐付け・求人にメモ無しは触らない (冪等)
3. FLOOR より前の応募は最初から見ない (検索条件で切る)
"""
from __future__ import annotations

from scripts.job_application_sync import backfill_appointment_memo as B
from scripts.job_application_sync.notes import (
    COPIED_NOTE_MARKER as COPIED, TEMPLATE_SIGNATURE as TEMPLATE,
    TRANSFER_SIGNATURE as TRANSFER)


def _plan(appts, l_of, copied=(), memo=()):
    return B.plan_backfill(
        appts, l_of,
        has_copy=lambda a: a in copied,
        listing_memo=lambda l: l in memo)


def test_メモが後から届いた応募をコピーする():
    p = _plan(["A1"], {"A1": ["L1"]}, copied=(), memo=("L1",))
    assert p["copy"] == [("A1", "L1")]


def test_コピー済みは触らない():
    p = _plan(["A1"], {"A1": ["L1"]}, copied=("A1",), memo=("L1",))
    assert not p["copy"] and p["done"] == 1


def test_求人にまだメモが無ければ翌晩へ回す():
    p = _plan(["A1"], {"A1": ["L1"]}, copied=(), memo=())
    assert not p["copy"] and p["no_memo"] == 1


def test_求人未紐付けは対象外():
    """未紐付けは relink/assoc の仕事。ここで無理に埋めない."""
    p = _plan(["A1"], {}, copied=(), memo=("L1",))
    assert not p["copy"] and p["no_listing"] == 1


def test_複数求人ならメモを持つ方を採る():
    p = _plan(["A1"], {"A1": ["L1", "L2"]}, copied=(), memo=("L2",))
    assert p["copy"] == [("A1", "L2")]


def test_二度流しても増えない():
    """1回目のコピー後 (copied に入る) は2回目で done になる."""
    l_of = {"A1": ["L1"], "A2": ["L1"]}
    p1 = _plan(["A1", "A2"], l_of, copied=(), memo=("L1",))
    assert len(p1["copy"]) == 2
    p2 = _plan(["A1", "A2"], l_of, copied=("A1", "A2"), memo=("L1",))
    assert not p2["copy"] and p2["done"] == 2


def test_窓はFLOORと直近30日の新しい方(monkeypatch):
    """★2026-09-15/16 に本番が60分のtimeoutで2晩打ち切られた。
    固定窓だと対象が毎日増え続ける (1,209→1,940→2,017件)。
    メモは遅くとも翌晩には求人へ届くので30日あれば取りこぼさない."""
    from datetime import datetime, timezone
    # FLOORの直後は FLOOR が効く (まだ30日経っていない)
    assert B.window_start(datetime(2026, 9, 10, tzinfo=timezone.utc)) == B.FLOOR
    # 十分あとでは移動窓が効き、FLOORより新しくなる
    late = B.window_start(datetime(2027, 3, 1, tzinfo=timezone.utc))
    assert late > B.FLOOR, "固定のままだと対象が無限に増える"
    assert late.startswith("2027-01-30")


def test_検索は窓の下限で絞る(monkeypatch):
    """recent_appointments が window_start を使っていること."""
    seen = {}
    monkeypatch.setattr(B, "search_all_by_id",
                        lambda o, p, f, **k: seen.update(filters=f) or [])
    B.recent_appointments("2026-09-05T00:00:00Z")
    assert {"propertyName": "hs_createdate", "operator": "GTE",
            "value": "2026-09-05T00:00:00Z"} in seen["filters"]


def test_コピー済み判定はバッチで取る(monkeypatch):
    """★1件1GETだと2,000件で1万回近いAPI呼び出しになり timeout する。
    100件まとめて関連を取り、本文もまとめて読む."""
    calls = []

    def fake_post(url, body, **_kw):
        calls.append(url)
        if "associations" in url:
            return {"results": [{"from": {"id": "A1"}, "to": [{"toObjectId": "N1"}]},
                                {"from": {"id": "A2"}, "to": [{"toObjectId": "N2"}]}]}
        return {"results": [
            {"id": "N1", "properties": {"hs_note_body": COPIED + " 本文"}},
            {"id": "N2", "properties": {"hs_note_body": "ただのメモ"}}]}

    monkeypatch.setattr(B, "post_retry", fake_post)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)
    assert B.copied_appointments(["A1", "A2"]) == {"A1"}
    assert len(calls) == 2, "応募100件までなら関連1回+本文1回で済む"


def test_求人のメモ有無もバッチで取る(monkeypatch):
    """転記メモでも旧テンプレでも「貼れる材料あり」とみなす."""
    def fake_post(url, body, **_kw):
        if "associations" in url:
            return {"results": [{"from": {"id": "L1"}, "to": [{"toObjectId": "N1"}]},
                                {"from": {"id": "L2"}, "to": [{"toObjectId": "N2"}]},
                                {"from": {"id": "L3"}, "to": [{"toObjectId": "N3"}]}]}
        return {"results": [
            {"id": "N1", "properties": {"hs_note_body": TRANSFER + " 本文"}},
            {"id": "N2", "properties": {"hs_note_body": TEMPLATE + " 本文"}},
            {"id": "N3", "properties": {"hs_note_body": "無関係"}}]}

    monkeypatch.setattr(B, "post_retry", fake_post)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)
    assert B.listings_with_memo(["L1", "L2", "L3"]) == {"L1", "L2"}


def test_FLOORは決定した日付そのもの():
    """「応募はこれから完全になればよい」(2026-09-02) — 過去の空きは救済しない。
    範囲比較 (>=) だと未来へずらす変異 (全応募を無視) がすり抜けるので値で固定する。
    この日付を変えるのはユーザー決定が変わったときだけ."""
    assert B.FLOOR == "2026-09-01T00:00:00Z"


def test_検索はFLOORで絞っている(monkeypatch):
    """逆証明Fで発覚: フィルタを外しても純関数テストは通る。検索条件そのものを守る."""
    seen = {}

    def fake_search(obj_type, props, filters, **_kw):
        seen.update(obj_type=obj_type, filters=filters)
        return [{"id": "A1"}]

    monkeypatch.setattr(B, "search_all_by_id", fake_search)
    assert B.recent_appointments() == ["A1"]
    assert seen["obj_type"] == B.APPOINTMENT
    assert {"propertyName": "hs_createdate", "operator": "GTE",
            "value": B.FLOOR} in seen["filters"]


def test_mainは求人を第1引数で応募を第2引数で渡す(monkeypatch):
    """逆証明Gで発覚: copy_listing_note_to_appointment(listing, appointment) の
    引数を入れ替えても純関数テストは通る。実配線を守る."""
    calls = []
    monkeypatch.setattr(B, "recent_appointments", lambda since="": ["A1"])
    monkeypatch.setattr(B, "listings_of", lambda ids: {"A1": ["L1"]})
    # ★2026-09-17: 判定はバッチ関数へ移した (1件ずつだと timeout する)
    monkeypatch.setattr(B, "copied_appointments", lambda ids: set())
    monkeypatch.setattr(B, "listings_with_memo", lambda ids: {"L1"})
    monkeypatch.setattr(
        B.N, "copy_listing_note_to_appointment",
        lambda listing_id, appointment_id, **k: calls.append(
            (listing_id, appointment_id)) or "N1")
    assert B.main(["--actual"]) == 0
    assert calls == [("L1", "A1")], "第1引数=求人 / 第2引数=応募"
