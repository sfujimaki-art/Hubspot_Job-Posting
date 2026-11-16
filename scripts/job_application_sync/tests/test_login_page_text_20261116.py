"""2026-11-16: AirWork のログインに失敗した画面の文字を抜き出す。

ユーザー「AirWork 側がランダムでログインパスワードを初期化してくるので、ログインできない案件も
あると思う。その時の画面のテキスト抽出もしてほしい」
"""
import asyncio

import pytest

from scripts.job_application_sync import alert_text as at
from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf


def test_IDとPWは伏せる():
    t = awf.mask_page_text("ようこそ user01 さん pass!word", ("user01", "pass!word"))
    assert "user01" not in t and "pass!word" not in t


def test_抜粋は1行にまとめて切る():
    assert awf.compact_text("あ\n\n い  う", 10) == "あ い う"
    assert awf.compact_text("x" * 200, 5) == "xxxxx…"


@pytest.mark.parametrize("text,want", [
    ("パスワードの有効期限が切れました。パスワードを再設定してください", "パスワードの再設定"),
    ("セキュリティのため、パスワードを変更してください", "パスワードの再設定"),
    ("アカウントがロックされました", "ロック"),
    ("現在ご利用いただけません", "現在ご利用いただけません"),
    ("ようこそ", ""),
])
def test_画面の文字から原因(text, want):
    got = awf.login_text_reason(text)
    assert (want in got) if want else got == ""


class _Page:
    url = "https://connect.airregi.jp/login?login_failed=true"

    def __init__(self, text):
        self._t = text

    async def evaluate(self, js, *a):
        return self._t


def test_画面の文字を取る_取れなければ空():
    assert asyncio.run(awf.page_text(_Page("本文 ID1"), ("ID1",))) == "本文 ****"

    class Bad(_Page):
        async def evaluate(self, js, *a):
            raise RuntimeError("closed")
    assert asyncio.run(awf.page_text(Bad(""))) == ""


def test_文字をファイルに残す(tmp_path):
    p = awf.save_page_text(tmp_path, "login_failed_idpw", "画面")
    assert p.read_text(encoding="utf-8") == "画面" and p.name.endswith(".txt")


def test_分類は画面の文字に引きずられない():
    e = ("ログイン後 dashboards に遷移せず: ID か PW が違います "
         "(login_id=x, last_url=https://connect.airregi.jp/login?login_failed=true)"
         " / 画面の文字: お問い合わせ 0120-403-000 Timeout")
    cause, _ = at.classify_aw_error(e)
    assert cause.startswith("ID か PW が違います")


def test_PWの再設定を求める画面は専用の分類():
    e = ("ログイン後 dashboards に遷移せず: 管理画面に入れません / パスワードの再設定を求める画面です"
         " (AirWork 側で初期化された可能性) (login_id=x, last_url=https://connect.airregi.jp/x)"
         " / 画面の文字: パスワードを再設定してください")
    cause, action = at.classify_aw_error(e)
    assert "パスワードの再設定" in cause and "顧客管理シート" in action


def test_描画前で空なら待って取り直す():
    class Slow(_Page):
        def __init__(self):
            self.n = 0

        async def evaluate(self, js, *a):
            self.n += 1
            return "" if self.n == 1 else "一致しません"

        async def wait_for_load_state(self, *a, **k):
            pass

        async def wait_for_timeout(self, ms):
            pass
    assert asyncio.run(awf.page_text(Slow())) == "一致しません"
