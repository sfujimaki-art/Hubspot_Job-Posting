"""ログイン後の着地先の判定と、応募者一覧での再ログイン (2026-10-19)。

- 求人一覧 (/job_offers) に着く社を、毎晩「ログイン失敗」にしていた (グリーンパックス)。
- ID/PW 誤り・規約の再同意の社 (毎晩 36 社) が、1 社 45 秒ずつ待ってから失敗していた。
- 2日ぶりの回で、保存セッションの社が応募者一覧で /interaction に飛ばされ、
  5 分後の回まで取れなかった (10/19 朝 4 社)。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from scripts.job_application_sync import alert_text
from scripts.job_application_sync.fetchers import aw_applicant_fetcher as af
from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf

AW = "https://ats.rct.airwork.net"
FAILED = "https://connect.airregi.jp/login?login_failed=true&client_id=AWR"


def test_認証済みの画面():
    assert awf._is_authenticated_url(f"{AW}/job_offers")
    assert awf._is_authenticated_url(f"{AW}/dashboards?x=1")
    assert not awf._is_authenticated_url(f"{AW}/interaction")
    assert not awf._is_authenticated_url(f"{AW}/usage_re_agreement")


def test_着地先ごとの原因():
    assert awf.login_fail_reason(FAILED) == "ID か PW が違います"
    assert awf.login_fail_reason(f"{AW}/usage_re_agreement") == "利用規約の再同意が必要です"
    assert "利用登録" in awf.login_fail_reason(f"{AW}/interaction")
    assert awf._login_stop_url(FAILED) and awf._login_stop_url(f"{AW}/usage_re_agreement")
    assert not awf._login_stop_url(f"{AW}/interaction")   # 途中で通る可能性があるので待つ


def test_Slackの分類():
    e = (f"ログイン後 dashboards に遷移せず: 利用規約の再同意が必要です "
         f"(login_id=x, last_url={AW}/usage_re_agreement)")
    assert "再同意" in alert_text.classify_aw_error(e)[0]
    e = f"ログイン後 dashboards に遷移せず (login_id=x, last_url={AW}/interaction)"
    assert "紐付いていない" in alert_text.classify_aw_error(e)[0]
    e = f"ログイン後 dashboards に遷移せず: ID か PW が違います (login_id=x, last_url={FAILED})"
    assert "ID か PW" in alert_text.classify_aw_error(e)[0]


class _LoginPage:
    """フルログインの画面。送信後に end_url へ着く。"""

    def __init__(self, end_url):
        self.url, self.end_url, self.waited = "", end_url, None

    async def goto(self, url, **k):
        self.url = url

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_url(self, pred, timeout):
        self.url = self.end_url
        self.waited = timeout
        if not pred(self.url):
            raise TimeoutError("45 s")


class _Ctx:
    def __init__(self, page):
        self.page, self.closed = page, False

    async def new_page(self):
        return self.page

    async def storage_state(self, path):
        open(path, "w").write("{}")

    async def close(self):
        self.closed = True


class _Browser:
    def __init__(self, page):
        self.page = page

    async def new_context(self, **k):
        return _Ctx(self.page)


@pytest.fixture
def _no_form(monkeypatch):
    async def yes(*a, **k):
        return True

    for name in ("_wait_any_visible", "_fill_first_match", "_click_first_match"):
        monkeypatch.setattr(awf, name, yes)

    async def no_pace(ctx):
        return None
    monkeypatch.setattr(awf._aw_pace, "install", no_pace)


def test_規約の再同意は待たずに原因つきで失敗(_no_form):
    page = _LoginPage(f"{AW}/usage_re_agreement")
    with pytest.raises(RuntimeError) as ex:
        asyncio.run(awf.establish_aw_session(_Browser(page), "id", "pw"))
    assert "利用規約の再同意が必要です" in str(ex.value)
    assert "遷移せず" in str(ex.value)        # 既存の分類・マスクの手がかりは残す


def test_求人一覧に着いたら成功(_no_form, tmp_path):
    page = _LoginPage(f"{AW}/job_offers")
    sp = tmp_path / "s.json"
    ctx, got = asyncio.run(awf.establish_aw_session(_Browser(page), "id", "pw",
                                                     storage_state_path=sp))
    assert got is page and sp.exists()


def test_応募者一覧でログイン選択に飛ばされたら1回だけログインし直す(monkeypatch, tmp_path):
    sp = tmp_path / "sess.json"
    sp.write_text("{}")
    calls = []

    class _P:
        def __init__(self, entries_url):
            self.url, self.entries_url = "", entries_url

        async def goto(self, url, **k):
            self.url = self.entries_url

    class _C:
        def __init__(self, page):
            self.page = page

        async def close(self):
            pass

    async def est(browser, login_id, password, storage_state_path=None, output_dir=None):
        calls.append(Path_exists(storage_state_path))
        # 1 回目: 保存セッション → /interaction。2 回目: ログインし直し → /entries
        url = f"{AW}/interaction" if len(calls) == 1 else f"{AW}/entries"
        return _C(_P(url)), _P(url)

    def Path_exists(p):
        return p.exists()

    async def dump(*a, **k):
        pass

    class _Stop(Exception):
        pass

    async def click(*a, **k):
        raise _Stop()   # 一覧に入れたことが分かればよい (ダウンロード以降は見ない)

    monkeypatch.setattr(af.awf, "establish_aw_session", est)
    monkeypatch.setattr(af.awf, "_dump_debug", dump)
    monkeypatch.setattr(af, "_click_button_by_partial_text", click)

    class _Br:
        async def close(self):
            pass

    class _PW:
        def __init__(self):
            self.chromium = types.SimpleNamespace(launch=self.launch)

        async def launch(self, **k):
            return _Br()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)

    class _DL:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(_P, "expect_download", lambda self, timeout: _DL(), raising=False)
    with pytest.raises(Exception):
        asyncio.run(af.fetch_aw_applicants("id", "pw", tmp_path / "out",
                                           storage_state_path=sp))
    # 保存セッションで 1 回、捨ててからもう 1 回 (2 回目はセッションのファイルが無い)
    assert calls == [True, False]
