# -*- coding: utf-8 -*-
"""billing-collect: 同じ社の受け取りの失敗を 5 分ごとに繰り返さない (2026-10-27)。"""
from datetime import datetime, timedelta

from scripts.job_application_sync.fetchers import aw_orchestrator as orch


def test_mark_error_backs_off_and_drops_on_third():
    now = datetime(2026, 10, 27, 13, 0)
    meta = {"week_start": "2026-10-19"}
    assert orch._billing_mark_error(meta, now) is False
    assert meta["errors"] == 1
    assert meta["retry_after"] == (now + timedelta(minutes=30)).isoformat()
    assert orch._billing_mark_error(meta, now) is False
    assert meta["retry_after"] == (now + timedelta(minutes=60)).isoformat()
    assert orch._billing_mark_error(meta, now) is True
    assert meta["errors"] == 3


def test_retry_wait():
    now = datetime(2026, 10, 27, 13, 0)
    assert orch._billing_retry_wait({}, now) is False
    assert orch._billing_retry_wait({"retry_after": "bad"}, now) is False
    later = (now + timedelta(minutes=5)).isoformat()
    assert orch._billing_retry_wait({"retry_after": later}, now) is True
    assert orch._billing_retry_wait({"retry_after": later}, now + timedelta(minutes=6)) is False
