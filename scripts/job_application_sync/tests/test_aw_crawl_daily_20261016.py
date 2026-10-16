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
