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
    plog._take()
    yield
    plog._take()
