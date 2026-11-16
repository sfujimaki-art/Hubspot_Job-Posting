"""2026-11-16: PC の AirWork 作業を 11/13〜11/16 朝まで止めた。再開時に、止めていた間の
応募を求人なしで作らないこと・夜の突き合わせが止めていた期間も見ることを確かめる。

ユーザー指示: 「止めていた期間もデータをとるような設計にしてほしい。データの連携は特に注意」
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync import aw_applicant_check as ck
from scripts.job_application_sync.fetchers import aw_urgent_queue as uq


# ---- 求人を待つ判定 ---------------------------------------------------------------------
def test_順番待ちの求人は止めていた時間がたっても待つ():
    now = 1_000_000.0
    three_days = 3 * 24 * 3600
    got = S.hold_job_ids(["J1", "J2"], {"J1": now - three_days, "J2": now - three_days},
                         tried=set(), now=now, pending={"J1"})
    assert got == {"J1"}, "順番待ちの J1 は待つ。順番待ちに無い J2 は従来どおり時間で打ち切る"


def test_取りに行き終えた求人は順番待ちに残っていても待たない():
    now = 1_000_000.0
    assert S.hold_job_ids(["J1"], {}, tried={"J1"}, now=now, pending={"J1"}) == set()


def test_順番待ちの社のWAIT_JOBは何日たっても取り直さない():
    now = datetime(2026, 11, 16, 9)
    e = {"wait_since": (now - timedelta(days=3)).isoformat(), "wait_company": "A社"}
    assert S.wait_job_due(e, {"A社"}, now=now) is False
    assert S.wait_job_due(e, set(), now=now) is True


def test_順番待ちの求人IDを読む(tmp_path):
    p = tmp_path / "q.json"
    uq.add("L1", "A社", ["J1", "J2"], path=p)
    uq.add("L2", "B社", ["J3"], path=p)
    assert uq.pending_job_ids(path=p) == {"J1", "J2", "J3"}
    uq.finish("L1", ok=True, path=p)
    assert uq.pending_job_ids(path=p) == {"J3"}


def test_順番待ちが読めなければ時間の上限だけで判断する(monkeypatch):
    def boom(path=None):
        raise OSError("x")
    monkeypatch.setattr(uq, "pending_job_ids", boom)
    assert S._urgent_pending_job_ids() == set()


# ---- 夜の突き合わせ -----------------------------------------------------------------------
def test_突き合わせは前回の続きから見る():
    today = date(2026, 11, 16)
    # 11/12 の夜に 11/11 まで確かめ、11/13〜15 の夜は止めていた
    assert ck.window(today, 3, prev_to="2026-11-11") == ("2026-11-08", "2026-11-15")
    # 毎晩動いていれば窓は少しだけ広がる (前回見送った応募を見直す)
    assert ck.window(today, 3, prev_to="2026-11-14") == ("2026-11-11", "2026-11-15")
    # 前回の記録が無ければ従来どおり
    assert ck.window(today, 3) == ("2026-11-13", "2026-11-15")


def test_突き合わせは14日より前には戻らない():
    assert ck.window(date(2026, 11, 16), 3, prev_to="2026-09-01")[0] == "2026-11-02"


def _row(d, job, email):
    return NS(apply_date=d, email=email, phone="", media_job_id=job, name="n", media="AirWork")


def test_取り込みの途中の応募は知らせず次の夜に確かめる(tmp_path, monkeypatch):
    from scripts.job_application_sync import applicant_import as ai
    from scripts.job_application_sync.fetchers import aw_applicant_fetcher as af

    async def _fetch(*a, **k):
        return tmp_path / "x.csv"
    monkeypatch.setattr(af, "fetch_aw_applicants", _fetch)
    y = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(ai, "load_applicants_csv", lambda p: [
        _row(y, "WAITING", "a@x.jp"), _row(y, "J9", "b@x.jp")])

    class _Cli:
        def __init__(self, t): pass
        def find_existing_appointment(self, *a, **k): return None
    monkeypatch.setattr(ai, "RealHubSpotClient", _Cli)
    monkeypatch.setattr(ck, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(ck, "CHECKED_PATH", tmp_path / "c.json")
    monkeypatch.setattr(ck, "in_progress_job_ids", lambda: {"WAITING"})

    out = ck.check_company("L1", "pw", "A社", tmp_path, "tok")
    assert out["missing"] == 1 and out["deferred"] == 1
    import json
    st = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert [v["media_job_id"] for v in st.values()] == ["J9"]
    assert ck.last_checked_to("L1") == y
    assert ck.last_checked_to("other") is None


def test_社の記録が無ければ既定の日から見る(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(ck, "CHECKED_PATH", tmp_path / "c.json")
    (tmp_path / "c.json").write_text(json.dumps({"__default__": "2026-11-11"}), encoding="utf-8")
    assert ck.last_checked_to("L1") == "2026-11-11"
    ck.set_checked_to("L1", "2026-11-15")
    assert ck.last_checked_to("L1") == "2026-11-15"
    assert ck.last_checked_to("L2") == "2026-11-11"
