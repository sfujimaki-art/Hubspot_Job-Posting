# -*- coding: utf-8 -*-
"""AirWork ログインの描画待ちと、欄未検出時の診断ログ (安全な情報のみ) のテスト."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync.fetchers import aw_csv_fetcher as aw  # noqa: E402


class FakeLoc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.page.visible_now(self.sel) else 0

    async def is_visible(self):
        return self.page.visible_now(self.sel)

    async def fill(self, v):
        self.page.filled[self.sel] = v

    async def click(self):
        self.page.clicked.append(self.sel)


class FakePage:
    """appear_after: 呼び出し回数(locator生成回数)がこれを超えると欄が出る."""

    def __init__(self, appear_after=0, never=False):
        self.calls = 0
        self.appear_after = appear_after
        self.never = never
        self.filled, self.clicked = {}, []
        self.url = "https://connect.airregi.jp/login?token=SECRET123&x=1#frag"

    def visible_now(self, sel):
        if self.never:
            return False
        return self.calls > self.appear_after and sel in (
            "input[name='password']", "input[type='submit']",
            "input[type='text']:visible")

    def locator(self, sel):
        self.calls += 1
        return FakeLoc(self, sel)

    async def title(self):
        return "ログイン | AirID"

    async def evaluate(self, script, words):
        return {"counts": {"hidden": 2, "text": 0},
                "found": {w: (w in ("ログイン", "AirID")) for w in words}}


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def fast(_):
        await real(0)
    monkeypatch.setattr(aw.asyncio, "sleep", fast)


def test_wait_finds_field_after_delay():
    page = FakePage(appear_after=12)
    assert asyncio.run(aw._wait_any_visible(
        page, aw.SSO_PW_SELECTOR_CANDIDATES)) is True
    assert asyncio.run(aw._fill_first_match(
        page, aw.SSO_PW_SELECTOR_CANDIDATES, "pw")) is True
    assert page.filled == {"input[name='password']": "pw"}


def test_wait_times_out_when_never_shown():
    page = FakePage(never=True)
    assert asyncio.run(aw._wait_any_visible(
        page, aw.SSO_ID_SELECTOR_CANDIDATES, timeout_ms=1000)) is False


def test_diag_line_has_only_safe_info():
    line = aw.format_login_diag(
        "https://connect.airregi.jp/login?token=SECRET123#frag",
        "ログイン | AirID", {"hidden": 2, "text": 1},
        {"ログイン": True, "AirID": True, "403": False})
    assert line == ("[aw-login-diag] url=connect.airregi.jp/login "
                    "title='ログイン | AirID' inputs=hidden=2,text=1 "
                    "words=ログイン,AirID")
    assert "SECRET123" not in line and "frag" not in line


def test_diag_logged_to_stderr_without_query_or_body(capsys):
    asyncio.run(aw._log_login_diag(FakePage(never=True), "form_not_found"))
    err = capsys.readouterr().err
    assert "url=connect.airregi.jp/login " in err
    assert "inputs=hidden=2,text=0" in err
    assert "words=ログイン,AirID" in err
    assert "stage=form_not_found" in err
    assert "SECRET123" not in err and "x=1" not in err


def test_safe_url_non_http():
    assert aw._safe_url("about:blank") == "(non-http)"
