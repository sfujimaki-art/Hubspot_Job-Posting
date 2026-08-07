"""AWセッション有効判定の回帰テスト (2026-08-07 の本番障害に対応).

## 何が起きたか

applicant_sync の AW 経路が丸一日 `done: 0` を出し続けた。
ログ上のエラーは全社とも:

    RuntimeError: 「応募者一覧をダウンロード」ボタン未検出

そのため「ボタンのセレクタが変わった」「レスポンシブでDOMから消えた」と
読んでビューポート固定やセレクタ追加を入れたが、**症状は消えなかった**。

## 真因

期限切れセッションで `/dashboards` を開くと、AirWork は

    https://ats.rct.airwork.net/interaction   (「アカウント登録・ログイン選択」)

へ飛ばす。旧判定は「URLに login も airplf も含まれなければセッション有効」
という**除外リスト方式**だったため、この URL を有効と誤判定し、
**再ログインせずに** `/entries` へ進んでいた。到達先は interaction のままで
button が 0 個 → 「ボタン未検出」として失敗する。

つまり原因は認証切れなのに、症状はボタン探索の失敗として現れる。
ボタン側をいくら直しても直らない類の故障。

実測 (2026-08-07):
  - 期限切れ  → /interaction  button=0  title='アカウント登録・ログイン選択'
  - 正常      → /dashboards   → /entries で button=10 (DLボタンあり)
  - 1社4秒で失敗 = フルログイン(約11秒)を通っていない = 再利用していた証拠

## 守る不変条件

判定は**ホワイトリスト**であること。「知らないURL」は無効側に倒す。
新しいリダイレクト先が増えても、勝手に有効と見なさない。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync.fetchers.aw_csv_fetcher import (
    _is_authenticated_url,
)


@pytest.mark.parametrize("url", [
    "https://ats.rct.airwork.net/dashboards",
    "https://ats.rct.airwork.net/dashboards/",
    "https://ats.rct.airwork.net/dashboards?from=login",
    "https://ats.rct.airwork.net/entries",
    "https://ats.rct.airwork.net/entries?entryTab=all",
    "https://ats.rct.airwork.net/entries#top",
])
def test_認証済み画面は有効と判定する(url):
    assert _is_authenticated_url(url) is True


@pytest.mark.parametrize("url", [
    # ★本番障害の実際の着地先。login も airplf も含まないのがポイント
    "https://ats.rct.airwork.net/interaction",
    "https://ats.rct.airwork.net/interaction/",
    "https://ats.rct.airwork.net/airplf/login",
    "https://connect.airregi.jp/login?client_id=x",
    "https://ats.rct.airwork.net/",
    "https://ats.rct.airwork.net/job_offers/bulk_download",
    # 知らない画面は無効側に倒す (ホワイトリスト方式の要点)
    "https://ats.rct.airwork.net/some_new_screen",
    "https://ats.rct.airwork.net/maintenance",
])
def test_認証済みでない画面は無効と判定する(url):
    assert _is_authenticated_url(url) is False


def test_除外リスト方式なら誤判定していたことを明示する():
    """旧判定を再現し、これが interaction を通してしまうことを記録する。

    「なぜホワイトリストにしたのか」を後から読んで分かるようにするためのテスト。
    """
    url = "https://ats.rct.airwork.net/interaction"
    old_judgement = ("login" not in url and "airplf" not in url)
    assert old_judgement is True, "旧判定は interaction を有効と誤判定していた"
    assert _is_authenticated_url(url) is False, "新判定は正しく無効にする"


def test_部分一致で誤って通さない():
    """'/dashboards' を含むだけの別URLを通さない (endswith で判定している)."""
    assert _is_authenticated_url(
        "https://evil.example.com/dashboards/redirect?to=x") is False
    assert _is_authenticated_url(
        "https://ats.rct.airwork.net/dashboards_old") is False
