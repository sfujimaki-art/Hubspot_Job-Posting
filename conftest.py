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
