# -*- coding: utf-8 -*-
"""公開ログにパスワードの断片も文字数も出さない (2026-10-13)。

10/10 以降エアワークが Actions からの接続を拒否し、毎回「ID/PWフィールド未検出」
の例外を通るようになった。その例外文に `pw=SK********0!` の形で、パスワードの
先頭2文字・末尾2文字・文字数が入っており、公開の Applicant Sync ログに出続けていた。
scrub_secrets は完全一致の値しか伏せないので、この断片はすり抜けていた。
"""
from __future__ import annotations

import asyncio

import pytest

from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf
from scripts.job_application_sync.fetchers import aw_orchestrator as orch

PW = "Qz7#Xk9!Wp2$"


def _leaks(text: str, pw: str = PW) -> list:
    """パスワード由来の情報 (2文字以上の断片・*の連なりによる文字数) を探す。"""
    hits = [pw[i:i + 2] for i in range(len(pw) - 1) if pw[i:i + 2] in text]
    if "*" * (len(pw) - 4) in text:
        hits.append("文字数")
    return hits


def test_maskは文字も文字数も出さない():
    for v in (PW, "abcd", "ab", "a" * 40):
        assert awf._mask(v) == "***"
    assert awf._mask("") == ""


class _Loc:
    first = property(lambda self: self)

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _Page:
    url = "https://connect.airregi.jp/login"

    async def goto(self, *a, **k):
        return None

    async def wait_for_load_state(self, *a, **k):
        return None

    def locator(self, sel):
        return _Loc()

    async def title(self):
        return ""

    async def evaluate(self, *a, **k):
        raise RuntimeError("no dom")


class _Ctx:
    async def new_page(self):
        return _Page()


class _Browser:
    async def new_context(self, **k):
        return _Ctx()


def test_欄未検出の例外文にパスワード由来の情報が入らない(monkeypatch, capsys):
    async def _no_wait(*a, **k):
        return False
    monkeypatch.setattr(awf, "_wait_any_visible", _no_wait)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(awf.establish_aw_session(_Browser(), "rpo_user", PW))
    msg = str(ei.value)
    assert "未検出" in msg
    assert "pw=" not in msg
    assert _leaks(msg) == [], msg
    out = capsys.readouterr()
    assert _leaks(out.out + out.err) == []


def test_scrub_secretsはパスワードの先頭2文字も残さない():
    msg = f"RuntimeError: ログイン失敗 (login_id=rpo_user, pw={PW})"
    out = S.scrub_secrets(msg, ["rpo_user"], passwords=[PW])
    assert _leaks(out) == [], out
    assert "***" in out and "rp…" in out, "ログインIDは従来どおり先頭2文字だけ残す"


def test_orchestratorの例外捕捉でパスワードを消す(monkeypatch):
    async def _boom(login_id, password, out_dir, **k):
        raise RuntimeError(f"失敗 login_id={login_id} raw={password}")
    monkeypatch.setattr(orch, "fetch_aw_xlsx", _boom)
    r = asyncio.run(orch.process_one(asyncio.Semaphore(1),
                                     {"login_id": "rpo_user", "password": PW,
                                      "company_name": "A社"},
                                     None, True, True))
    assert r["status"] == "error"
    assert _leaks(r["error"]) == [] and _leaks("\n".join(r["trace"])) == []
