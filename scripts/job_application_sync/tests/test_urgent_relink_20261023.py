# -*- coding: utf-8 -*-
"""至急の取得で求人が取れた社は、その応募の求人IDだけすぐ紐付け直す (2026-10-23 ユーザー指示)。"""
from __future__ import annotations

from scripts.job_application_sync import applicant_sync as aps
from scripts.job_application_sync.fetchers import aw_orchestrator as orch


def test_取れた社の求人IDだけで紐付け直し_失敗しても止めない(monkeypatch):
    calls = []
    monkeypatch.setattr(aps, "relink", lambda dry_run=False, target_job_ids=None, **k:
                        calls.append((dry_run, target_job_ids)) or {"relinked": 1})
    results = [{"login_id": "a", "status": "ok"}, {"login_id": "b", "status": "error"},
               {"login_id": "c", "status": "ok"}]
    jobs = {"a": ["11", "12"], "b": ["21"], "c": ["12", "31"]}
    assert orch._relink_urgent(results, jobs) == {"relinked": 1}
    assert calls == [(False, ["11", "12", "31"])], "取れなかった社 b の求人は引かない・重複は 1 回"

    calls.clear()
    assert orch._relink_urgent([{"login_id": "b", "status": "error"}], jobs) == {}
    assert calls == [], "取れた社が無ければ HubSpot を呼ばない"

    def boom(**k):
        raise RuntimeError("secret-id")
    monkeypatch.setattr(aps, "relink", boom)
    assert orch._relink_urgent(results, jobs) == {}


def test_止める時間の既定は10時間(monkeypatch):
    import inspect
    assert '"AW_LOGIN_STOP_HOURS", "10"' in inspect.getsource(orch)
