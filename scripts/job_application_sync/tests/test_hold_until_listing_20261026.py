"""応募の求人が HubSpot に無いときは、至急の取得が求人を作るまで応募を作らずに待つ (2026-10-26)。

ユーザー指示「求人なしで入るのがだめ (基本的には)」。実例: 2026-10-26 14:20 に新しい求人への
AW の応募が求人なしで作られ、14:29 に紐付くまで担当者から「求人に紐づいていない」と見えた。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync.fetchers import aw_urgent_queue as uq


def _row(job="AW-NEW", lineno=1):
    return ai.ApplicantRow(
        name="X", kana="", phone="0120000000", email="x@example.com",
        apply_date="2026-10-26", media="AirWork", media_job_id=job,
        airwork_login_id="", raw_lineno=lineno)


# ---- 1 行の処理 ------------------------------------------------------------------------
def test_待つ求人IDで求人が無ければ作らない():
    client = ai.DryRunClient()
    res = ai.process_applicant(_row(), client, hold_job_ids={"AW-NEW"})
    assert res.status == "held"
    assert client.created_appts == [] and client.associations == []


def test_待つ求人でも求人があれば紐付けて作る():
    client = ai.DryRunClient(listings_aw={("AW-NEW", "login_user_a"): "L-1"})
    res = ai.process_applicant(_row(), client, hold_job_ids={"AW-NEW"})
    assert res.status == "linked" and res.listing_id == "L-1"
    assert len(client.created_appts) == 1


def test_待つ対象でない求人は従来どおり求人なしで作る():
    client = ai.DryRunClient()
    res = ai.process_applicant(_row(job="AW-OLD"), client, hold_job_ids={"AW-NEW"})
    assert res.status == "unlinked"
    assert len(client.created_appts) == 1


def test_既にある応募は待たずに重複として飛ばす():
    client = ai.DryRunClient(existing_appts={
        ("AirWork", "2026-10-26", "email", "x@example.com"): "A-1"})
    res = ai.process_applicant(_row(), client, hold_job_ids={"AW-NEW"})
    assert res.status == "skip_duplicate"


# ---- どの求人IDを待つか ------------------------------------------------------------------
def test_待つ求人ID_取りに行き終えた求人と待ちすぎた求人は待たない():
    now = 1_000_000.0
    lim = S.HOLD_MAX_MIN * 60
    got = S.hold_job_ids(["new", "tried", "long", "recent"],
                         {"long": now - lim, "recent": now - lim + 60},
                         tried={"tried"}, now=now)
    assert got == {"new", "recent"}


# ---- 台帳の取り直し ---------------------------------------------------------------------
def test_WAIT_JOBは至急の取得が終わるか待ちすぎたら取り直す():
    now = datetime(2026, 10, 26, 15, 0)
    e = {"wait_since": (now - timedelta(minutes=5)).isoformat(), "wait_company": "A社"}
    assert S.wait_job_due(e, {"A社"}, now=now) is False      # まだ取りに行っていない
    assert S.wait_job_due(e, {"B社"}, now=now) is True       # 取り終えた
    old = dict(e, wait_since=(now - timedelta(minutes=S.HOLD_MAX_MIN)).isoformat())
    assert S.wait_job_due(old, {"A社"}, now=now) is True     # 待ちすぎ


def test_待ち始めは最初の1回だけ記録し_試行回数を数えない(tmp_path):
    led = S.Ledger(tmp_path / "l.json")
    led.mark_wait("r1", "A社", "待ち")
    first = led.data["r1"]["wait_since"]
    led.mark_wait("r1", "A社", "待ち")
    assert led.data["r1"]["wait_since"] == first
    assert led.status("r1") == "WAIT_JOB" and led.attempts("r1") == 0


def test_至急の待ち行列の社と取りに行き終えた求人を返す(tmp_path):
    p = tmp_path / "q.json"
    uq.add("lid1", "A社", ["J1"], path=p)
    assert uq.pending_companies(path=p) == {"A社"}
    uq.finish("lid1", ok=True, path=p)
    assert uq.pending_companies(path=p) == set()
    assert uq.tried_ids(path=p) == {"J1"}


# ---- 社ごとの処理: 無い求人は待つ ID として返し、作る処理へ渡す ----------------------------
def test_社の処理は無い求人を待つIDとして作る処理へ渡す(monkeypatch):
    async def fake_fetch(bid, pw, out_dir):
        return Path("x.csv")
    monkeypatch.setattr(S, "_fetch_aw_csv", fake_fetch)
    monkeypatch.setattr(ai, "load_applicants_csv", lambda p: [
        SimpleNamespace(media_job_id=j, media="AirWork") for j in ("J1", "J2")])
    monkeypatch.setattr(ai, "append_apply_time_log", lambda p: 0)
    monkeypatch.setattr(uq, "tried_ids", lambda path=None: set())

    class _Cli:
        stats: dict = {}
        def prefetch_listings(self, j): return True
        def prefetch_appointments(self, m, j): return True
        def missing_listings(self, j): return ["J2"]
    monkeypatch.setattr(S, "_new_aw_client", lambda token: _Cli())
    seen = {}

    def fake_run(rows, cli, default_login_id="", hold_job_ids=None):
        seen["hold"] = hold_job_ids
        return [ai.ProcessResult(status="linked", applicant_key="a", media="AirWork",
                                 media_job_id="J1"),
                ai.ProcessResult(status="held", applicant_key="b", media="AirWork",
                                 media_job_id="J2")]
    monkeypatch.setattr(ai, "run_import", fake_run)
    r = S.process_aw_account("A社", ["bid1"], "pw", Path("."), dry_run=False,
                             allow_acquire=False, known_absent=set(), hold_since={})
    assert seen["hold"] == {"J2"}
    assert r["held"] == 1 and r["unlinked"] == 0 and r["linked"] == 1 and r["ok"]
    assert r["missing_job_ids"] == ["J2"]           # 至急の取得の待ち行列には積む
