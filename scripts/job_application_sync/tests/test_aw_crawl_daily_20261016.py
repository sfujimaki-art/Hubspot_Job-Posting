# -*- coding: utf-8 -*-
"""求人の巡回: 取れていない社から回る・受注中と応募起点を足す (2026-10-16)。外部には触らない。"""
from __future__ import annotations

import asyncio

from scripts.job_application_sync.fetchers import aw_orchestrator as orch


def test_最後に取れたのが古い社から回る():
    accts = [{"login_id": x} for x in ("a", "b", "c", "d")]
    last = {"a": 300.0, "b": 100.0, "d": 200.0}          # c は一度も取れていない
    assert [a["login_id"] for a in orch._order_stale_first(accts, last)] == ["c", "b", "d", "a"]


def test_取れた時刻を社ごとに記録し_読み直して足す(tmp_path):
    p = tmp_path / "last.json"
    orch._record_last_ok("a", 1.0, path=p)
    orch._record_last_ok("b", 2.0, path=p)
    orch._record_last_ok("a", 3.0, path=p)
    assert orch._load_last_ok(p) == {"a": 3.0, "b": 2.0}


def _stub_targets(monkeypatch, deal_ids, app_ids):
    from scripts.job_application_sync import applicant_queue as aq
    monkeypatch.setattr(orch, "_fetch_active_deal_mails", lambda: ["m"])
    monkeypatch.setattr(orch, "resolve_accounts_for_mails",
                        lambda mails, **k: ([{"login_id": i, "password": "p", "company_name": i}
                                             for i in deal_ids], []))
    monkeypatch.setattr(aq, "read_new_items_from_sheet1", lambda **k: list(app_ids))

    class _R:
        def build(self):
            return self

        def resolve(self, it):
            from types import SimpleNamespace
            return SimpleNamespace(closed=False, b_ids=[it], b_pw="p", company=it)
    monkeypatch.setattr(aq, "AccountResolver", _R)
    seen = []

    async def fake_one(sem, acc, out_dir, dry_run, headless, phase="full"):
        seen.append(acc["login_id"])
        return {"login_id": acc["login_id"], "company": "", "status": "ok"}
    monkeypatch.setattr(orch, "process_one", fake_one)
    monkeypatch.setattr(orch, "_write_account_results", lambda *a, **k: {})
    monkeypatch.setattr(orch, "_slack_notify", lambda m: None)
    return seen


def test_受注中と応募起点を足し_重複は1回(monkeypatch, tmp_path):
    seen = _stub_targets(monkeypatch, ["d1", "x"], ["x", "a1"])
    asyncio.run(orch.orchestrate(parallel=1, dry_run=False, out_dir=tmp_path, log_dir=tmp_path,
                                 from_active_deals=True, from_applications=True))
    assert sorted(seen) == ["a1", "d1", "x"]
    assert set(orch._load_last_ok()) == {"a1", "d1", "x"}   # 取れた社は記録される


def test_上限つきでも取れていない社が先(monkeypatch, tmp_path):
    seen = _stub_targets(monkeypatch, ["d1", "d2", "d3"], [])
    orch._record_last_ok("d1", 100.0)
    orch._record_last_ok("d3", 50.0)
    asyncio.run(orch.orchestrate(parallel=1, limit=2, dry_run=False, out_dir=tmp_path,
                                 log_dir=tmp_path, from_active_deals=True))
    assert seen == ["d2", "d3"]


# ---- すぐ取りに行く待ち行列 -------------------------------------------------------
from scripts.job_application_sync.fetchers import aw_urgent_queue as U  # noqa: E402


def test_待ち行列は社ごとにまとめ_取りに行った求人は24時間積み直さない():
    assert U.add("L1", "A社", ["J1", "J2"]) == ["J1", "J2"]
    assert U.add("L1", "A社", ["J2", "J3"]) == ["J2", "J3"]
    assert U.pending()["L1"]["job_ids"] == ["J1", "J2", "J3"]
    U.finish("L1", ok=True)
    assert U.pending() == {}
    assert U.add("L1", "A社", ["J1", "J4"]) == ["J4"]      # J1 は取りに行ったばかり


def test_失敗は3回まで待ち行列に残す():
    U.add("L2", "B社", ["J9"])
    U.finish("L2", ok=False)
    U.finish("L2", ok=False)
    assert "L2" in U.pending()
    U.finish("L2", ok=False)
    assert "L2" not in U.pending()


def test_待ち行列の社だけを取り_取れたら外す(monkeypatch, tmp_path):
    U.add("q1", "Q社", ["J1"])
    U.add("gone", "消えた社", ["J2"])
    monkeypatch.setattr(orch, "iter_aw_accounts",
                        lambda active_only=False, prefer="A": iter(
                            [{"login_id": "q1", "password": "p", "company_name": "Q社"},
                             {"login_id": "other", "password": "p", "company_name": "他社"}]))
    seen = []

    async def fake_one(sem, acc, out_dir, dry_run, headless, phase="full"):
        seen.append(acc["login_id"])
        return {"login_id": acc["login_id"], "company": "", "status": "ok"}
    monkeypatch.setattr(orch, "process_one", fake_one)
    monkeypatch.setattr(orch, "_write_account_results", lambda *a, **k: {})
    monkeypatch.setattr(orch, "_slack_notify", lambda m: None)
    asyncio.run(orch.orchestrate(parallel=1, dry_run=False, out_dir=tmp_path, log_dir=tmp_path,
                                 from_urgent_queue=True))
    assert seen == ["q1"]
    assert U.pending()["gone"]["attempts"] == 1    # シートに無い社は失敗として数える
    assert "q1" not in U.pending()


def test_応募の回は求人を取りに行かず_無い求人IDを返す(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from scripts.job_application_sync import applicant_import as ai
    from scripts.job_application_sync import applicant_sync as S

    async def fake_fetch(bid, pw, out_dir):
        return Path("x.csv")
    monkeypatch.setattr(S, "_fetch_aw_csv", fake_fetch)
    monkeypatch.setattr(ai, "load_applicants_csv", lambda p: [
        SimpleNamespace(media_job_id=j, media="AirWork") for j in ("J1", "J2", "J3")])
    monkeypatch.setattr(ai, "append_apply_time_log", lambda p: 0)

    class _Cli:
        stats: dict = {}
        def prefetch_listings(self, j): return True
        def prefetch_appointments(self, m, j): return True
        def missing_listings(self, j): return ["J2", "J3"]
    monkeypatch.setattr(S, "_new_aw_client", lambda token: _Cli())
    monkeypatch.setattr(S, "_ensure_aw_jobs", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("応募の回で求人を取りに行ってはいけない")))
    monkeypatch.setattr(ai, "run_import", lambda rows, cli, default_login_id="": [
        ai.ProcessResult(status="unlinked", applicant_key="k", media="AirWork", media_job_id="J2")])
    r = S.process_aw_account("A社", ["bid1"], "pw", Path("."), dry_run=False,
                             allow_acquire=False, known_absent={"J3"})
    assert r["missing_job_ids"] == ["J2"]           # 一覧に無いと確認済みの J3 は積まない
    assert r["_urgent_login_id"] == "bid1" and r["login_id"] != "bid1"   # 表示用は伏せる


# ---- 課金: 求人と同じ回で、週に 1 回だけ作成する -----------------------------------------
def test_課金は週に1回だけ作成し_データ無しは記録して押さない(monkeypatch, tmp_path):
    import datetime as dt
    from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
    monkeypatch.setattr(bf, "DEFAULT_OUT_DIR", tmp_path / "billing")
    week = bf.last_full_week()
    calls = []

    async def fake_fetch(lid, pw, weeks, out_dir, headless=True, mode="full"):
        calls.append((lid, mode))
        return [(weeks[0], "queued" if lid == "A" else "empty", None)]
    monkeypatch.setattr(bf, "fetch_aw_billing_weeks", fake_fetch)
    assert asyncio.run(orch._billing_create_if_due("A", "p", "A社")) == "queued"
    assert asyncio.run(orch._billing_create_if_due("A", "p", "A社")) == "skip"     # 待ち行列にある
    assert asyncio.run(orch._billing_create_if_due("B", "p", "B社")) == "empty"
    assert asyncio.run(orch._billing_create_if_due("B", "p", "B社")) == "skip"     # データ無し確定
    assert calls == [("A", "create"), ("B", "create")]
    q = orch._load_json(orch.BILLING_QUEUE_PATH)
    assert q["A"]["week_start"] == week.isoformat()
    assert orch._load_json(orch.BILLING_EMPTY_PATH) == {"B": [week.isoformat()]}


def test_課金の待ち行列は20時間を過ぎたら外す(monkeypatch, tmp_path):
    import datetime as dt
    old = (dt.datetime.now() - dt.timedelta(hours=21)).isoformat()
    new = dt.datetime.now().isoformat()
    orch._save_json(orch.BILLING_QUEUE_PATH, {
        "old": {"week_start": "2026-10-05", "triggered_at": old},
        "new": {"week_start": "2026-10-05", "triggered_at": new}})
    seen = []

    async def fake_one(sem, acc, weeks, out_dir, headless, mode):
        seen.append(acc["login_id"])
        return {"login_id": acc["login_id"], "company": "", "status": "not_ready"}
    monkeypatch.setattr(orch, "process_billing_one", fake_one)
    monkeypatch.setattr(orch, "find_account_by_login_id",
                        lambda lid: {"login_id": lid, "password": "p", "company_name": ""})
    monkeypatch.setattr(orch, "_write_account_results", lambda *a, **k: {})
    monkeypatch.setattr(orch, "_slack_notify", lambda m: None)
    asyncio.run(orch.orchestrate_billing("billing-collect", [], out_dir=tmp_path, log_dir=tmp_path))
    assert seen == ["new"]
    assert set(orch._load_json(orch.BILLING_QUEUE_PATH)) == {"new"}


def test_同じ夜のやり直しは取れた社を飛ばす(monkeypatch, tmp_path):
    seen = _stub_targets(monkeypatch, ["d1", "d2", "d3"], [])
    orch._record_last_ok("d1", 500.0)   # この夜 (400 以降) に取れた
    orch._record_last_ok("d2", 100.0)   # 前の夜
    asyncio.run(orch.orchestrate(parallel=1, dry_run=False, out_dir=tmp_path, log_dir=tmp_path,
                                 from_active_deals=True, skip_ok_since=400.0))
    assert sorted(seen) == ["d2", "d3"]


def test_課金の作成のやり直しは受け取り待ちの週を作り直さない(monkeypatch, tmp_path):
    from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
    week = bf.last_full_week()
    orch._save_json(orch.BILLING_QUEUE_PATH, {"q": {"week_start": week.isoformat(),
                                                    "triggered_at": "2099-01-01T00:00:00"}})
    monkeypatch.setattr(orch, "_select_billing_accounts", lambda fad, t: [
        {"login_id": x, "password": "p", "company_name": x} for x in ("q", "n")])
    monkeypatch.setattr(bf, "has_saved", lambda d, lid, w: False)
    seen = []

    async def fake_one(sem, acc, weeks, out_dir, headless, mode):
        seen.append((acc["login_id"], [w.isoformat() for w in weeks]))
        return {"login_id": acc["login_id"], "company": "", "status": "queued",
                "week_status": {w.isoformat(): "queued" for w in weeks}}
    monkeypatch.setattr(orch, "process_billing_one", fake_one)
    monkeypatch.setattr(orch, "_write_account_results", lambda *a, **k: {})
    monkeypatch.setattr(orch, "_slack_notify", lambda m: None)
    asyncio.run(orch.orchestrate_billing("billing-create", [week], out_dir=tmp_path, log_dir=tmp_path,
                                         from_active_deals=True))
    assert seen == [("n", [week.isoformat()])]
