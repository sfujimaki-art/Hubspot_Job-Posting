"""2026-11-12: 求人なしで作られた AW 応募 (11/10、ある社の拠点) の原因 3 つの修正。

1. 求人を待つ判定が、本番の呼び方 (allow_acquire に関数・枠 0) では通らなかった。
2. 求人一覧ファイルを、AirWork に残っている前日のものをそのまま落としていた。
3. 応募の担当者を求人から写していた。ユーザー指示「取引→求人→応募 の順で連携している。
   求人作成時と応募獲得時の担当者は違う時があり、取引の担当者は常に最新」→ 取引から都度読む。
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf
from scripts.job_application_sync.fetchers import aw_urgent_queue as uq


# ---- 1. 本番の呼び方でも求人を待つ ----------------------------------------------------
def _setup_account(monkeypatch, seen):
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

    def fake_run(rows, cli, default_login_id="", hold_job_ids=None):
        seen["hold"] = hold_job_ids
        return [ai.ProcessResult(status="linked", applicant_key="a", media="AirWork",
                                 media_job_id="J1"),
                ai.ProcessResult(status="held", applicant_key="b", media="AirWork",
                                 media_job_id="J2")]
    monkeypatch.setattr(ai, "run_import", fake_run)


def test_枠が取れないとき_無い求人の応募は待つ(monkeypatch):
    """本番は allow_acquire に関数を渡し、枠 (JAS_SELFHEAL_MAX_ACQUIRE) は 0。"""
    seen: dict = {}
    _setup_account(monkeypatch, seen)
    r = S.process_aw_account("A社", ["bid1"], "pw", Path("."), dry_run=False,
                             allow_acquire=lambda: False, known_absent=set(),
                             hold_since={})
    assert seen["hold"] == {"J2"}, "10/26 以降はここが空で、待たずに作っていた"
    assert r["missing_job_ids"] == ["J2"]
    assert r["held"] == 1 and r["unlinked"] == 0


def test_待ちの上限を過ぎた求人は待たない(monkeypatch):
    seen: dict = {}
    _setup_account(monkeypatch, seen)
    old = datetime.now().timestamp() - (S.HOLD_MAX_MIN + 1) * 60
    S.process_aw_account("A社", ["bid1"], "pw", Path("."), dry_run=False,
                         allow_acquire=lambda: False, known_absent=set(),
                         hold_since={"J2": old})
    assert seen["hold"] == set()


# ---- 2. 古い求人一覧ファイルは作り直す -----------------------------------------------
def _zip(tmp_path, inner):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(inner, b"x")
    return p


def test_求人一覧の作成日時はzipの中のファイル名から読む(tmp_path):
    p = _zip(tmp_path, "Joblist_1321047_20261111141712.xlsx")
    assert awf.export_created_at(p) == datetime(2026, 11, 11, 14, 17, 12)


def test_作成日時が読めなければNone(tmp_path):
    assert awf.export_created_at(_zip(tmp_path, "other.xlsx")) is None
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert awf.export_created_at(bad) is None


def test_古さの判定():
    now = datetime(2026, 11, 12, 12, 22)
    made = datetime(2026, 11, 11, 14, 17)          # 実測: 22 時間前のファイルを使っていた
    assert awf.export_is_stale(made, 360, now) is True
    assert awf.export_is_stale(now - timedelta(minutes=5), 10, now) is False
    assert awf.export_is_stale(now - timedelta(minutes=11), 10, now) is True
    assert awf.export_is_stale(None, 10, now) is False      # 分からなければ作り直さない
    assert awf.export_is_stale(made, None, now) is False


def test_至急の取得は既定より短い上限を使う():
    assert awf.URGENT_EXPORT_MAX_AGE_MIN < awf.EXPORT_MAX_AGE_MIN


# ---- 3. 応募の担当者は取引から読む ----------------------------------------------------
class _Resp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def _client(deals, active, listing_owner="OLD"):
    """求人 L1 → 取引 D1/D2。求人自身の担当者 (OLD) は使わないことを確かめる。"""
    calls = []

    class H:
        def get(self, url, **k):
            calls.append(url)
            if "/crm/v3/owners" in url:
                return _Resp({"results": [{"id": a} for a in active]})
            if "/associations/0-3" in url:
                return _Resp({"results": [{"toObjectId": d} for d in deals]})
            if "/0-420/" in url:
                return _Resp({"properties": {"hubspot_owner_id": listing_owner}})
            raise AssertionError(url)

        def post(self, url, **k):
            calls.append(url)
            if url.endswith("/0-3/batch/read"):
                return _Resp({"results": [{"id": i, "properties": p}
                                          for i, p in deals.items()]})
            if url.endswith("/0-3/search"):
                return _Resp({"results": []})
            raise AssertionError(url)

    cli = ai.RealHubSpotClient("t")
    cli._requests = H()
    return cli, calls


def _deal(owner, stage="x", start="2026-01-01"):
    return {"hubspot_owner_id": owner, "dealstage": stage,
            "contract_start_date": start, "createdate": start}


def test_担当者は今の契約の取引の担当者(monkeypatch):
    monkeypatch.setattr(ai.DM, "is_live", lambda p: True)
    cli, _ = _client({"D1": _deal("U_OLD", start="2025-01-01"),
                      "D2": _deal("U_NEW", start="2026-04-01")},
                     active={"U_OLD", "U_NEW"})
    assert cli.get_deal_owner("L1", "AirWork", "") == "U_NEW"


def test_無効なユーザーは書かない(monkeypatch):
    monkeypatch.setattr(ai.DM, "is_live", lambda p: True)
    cli, _ = _client({"D1": _deal("U_GONE")}, active={"U_OTHER"})
    assert cli.get_deal_owner("L1", "AirWork", "") is None


def test_取引の担当者が空なら求人の担当者で埋めない(monkeypatch):
    monkeypatch.setattr(ai.DM, "is_live", lambda p: True)
    cli, _ = _client({"D1": _deal("")}, active={"OLD"})
    assert cli.get_deal_owner("L1", "AirWork", "") is None


def test_取引群の解決は求人ごとに1回(monkeypatch):
    monkeypatch.setattr(ai.DM, "is_live", lambda p: True)
    cli, calls = _client({"D1": _deal("U1")}, active={"U1"})
    cli.get_deal_owner("L1", "AirWork", "")
    n = len(calls)
    cli.get_deal_owner("L1", "AirWork", "")
    assert len(calls) == n


def test_応募作成時の担当者は取引から():
    row = ai.ApplicantRow(name="n", kana="", phone="", email="a@x.jp",
                          apply_date="2026-11-10", media="AirWork",
                          media_job_id="J1", raw_lineno=2)
    client = ai.DryRunClient(listings_aw={("J1", "me"): "L1"})
    client.listing_owners = {"L1": "LISTING_OWNER"}
    client.deal_owners = {"L1": "DEAL_OWNER"}
    res = ai.process_applicant(row, client, default_login_id="me")
    assert res.listing_id == "L1"
    assert client.created_appts[0]["properties"]["hubspot_owner_id"] == "DEAL_OWNER"
