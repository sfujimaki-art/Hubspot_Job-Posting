# -*- coding: utf-8 -*-
"""テスト全体の共通設定。

private_log (非公開ログ) がテスト中に本物のスプレッドシートへ書きに行かない
ようにする。開発者の .env に JAS_SHEET_ID があっても、テストでは未設定扱い
(=「書けなかった」経路) になる。溜まった行はテストごとに捨てる
(終了時の atexit で data/ 配下にローカルファイルを作らないため)。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_private_sheet(monkeypatch):
    from scripts.job_application_sync import private_log as plog

    monkeypatch.delenv("JAS_SHEET_ID", raising=False)
    monkeypatch.delenv("JAS_APPLICANT_QUEUE_SHEET_ID", raising=False)
    monkeypatch.delenv("PRIVATE_LOG_SHEET_ID", raising=False)
    plog._take()
    yield
    plog._take()


@pytest.fixture(autouse=True)
def _no_real_aw_ledger(monkeypatch, tmp_path):
    """AW の取得台帳 (data/ 配下) を、テスト中は一時ディレクトリへ向ける。"""
    from scripts.job_application_sync import end_vanished_listings as ev

    monkeypatch.setattr(ev, "AW_LEDGER_PATH", tmp_path / "aw_fetch_ledger.json")


@pytest.fixture(autouse=True)
def _no_real_aw_pace(monkeypatch, tmp_path):
    """AirWork のアクセス間隔・403 停止のファイルを、テストでは一時フォルダに置く。

    実機の PC の %TEMP% のファイルを読み書きしない (本番の停止状態や間隔に影響しない)。
    社と社の間の待ちはテストでは 0 秒にする。
    """
    from scripts.job_application_sync.fetchers import aw_pace

    monkeypatch.setattr(aw_pace, "PACE_FILE", tmp_path / "pace.txt")
    monkeypatch.setattr(aw_pace, "COMPANY_PACE_FILE", tmp_path / "company_pace.txt")
    monkeypatch.setattr(aw_pace, "BLOCK_FILE", tmp_path / "blocked_until.txt")
    monkeypatch.setattr(aw_pace, "COMPANY_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(aw_pace, "COMPANY_INTERVAL_MIN_SEC", 0.0)
    monkeypatch.setattr(aw_pace, "COMPANY_INTERVAL_MAX_SEC", 0.0)
    # 求人を最後に取れた時刻の記録 (巡回の順番) も一時フォルダへ
    from scripts.job_application_sync.fetchers import aw_orchestrator as orch
    monkeypatch.setattr(orch, "LAST_OK_PATH", tmp_path / "aw_crawl_last_ok.json")
    from scripts.job_application_sync.fetchers import aw_urgent_queue
    monkeypatch.setattr(aw_urgent_queue, "QUEUE_PATH", tmp_path / "aw_urgent_jobs.json")
    # 巡回の中の画像の取得 (公開ページと HubSpot への通信) はテストでは動かさない
    monkeypatch.setenv("JAS_AW_IMAGES", "0")
    monkeypatch.setenv("JAS_AW_BILLING", "0")
    monkeypatch.setattr(orch, "BILLING_QUEUE_PATH", tmp_path / "aw_billing_queue.json")
    monkeypatch.setattr(orch, "BILLING_EMPTY_PATH", tmp_path / "aw_billing_empty.json")
