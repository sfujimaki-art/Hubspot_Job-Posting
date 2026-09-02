# -*- coding: utf-8 -*-
"""応募メモの穴埋め (backfill_appointment_memo) のテスト (2026-09-02)。

守る性質:
1. 求人にメモが届いた後の応募だけがコピー対象になる
2. コピー済み・求人未紐付け・求人にメモ無しは触らない (冪等)
3. FLOOR より前の応募は最初から見ない (検索条件で切る)
"""
from __future__ import annotations

from scripts.job_application_sync import backfill_appointment_memo as B


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
    monkeypatch.setattr(B, "recent_appointments", lambda: ["A1"])
    monkeypatch.setattr(B, "listings_of", lambda ids: {"A1": ["L1"]})
    monkeypatch.setattr(B.N, "has_copied_note", lambda a, **k: False)
    monkeypatch.setattr(B.N, "get_listing_template_note_body", lambda l, **k: "本文")
    monkeypatch.setattr(
        B.N, "copy_listing_note_to_appointment",
        lambda listing_id, appointment_id, **k: calls.append(
            (listing_id, appointment_id)) or "N1")
    assert B.main(["--actual"]) == 0
    assert calls == [("L1", "A1")], "第1引数=求人 / 第2引数=応募"
