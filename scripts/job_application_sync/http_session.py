"""HubSpot などへの HTTP 送信で、接続を使い回すための部品。

★2026-10-13 (専用PCの実測): `requests.post` などモジュールの関数を直接呼ぶと、
  呼ぶたびに Session を作り直し、新しい TCP + TLS 接続を張る。そのたびに
  certifi の CA 束をファイルから読み直す (OpenSSL 3 で 1 回 約 0.3 秒)。
  実測: 新規接続の requests.get 1 回 1.1 秒 / Session 使い回し 0.46 秒 (同じ問い合わせ 30 回)。
  py-spy では取り込み中の時間の 48% が TLS 接続の確立だった。

スレッドごとに 1 つの Session を持つ (requests.Session はスレッド間で
共有しない方が安全)。呼び出し側は `requests` モジュールと同じ形で使える。
"""
from __future__ import annotations

import threading

import requests

_local = threading.local()


def session() -> requests.Session:
    """このスレッドの Session。無ければ作る。"""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


class _Facade:
    """`requests` モジュールの代わりに渡せる薄い入れ物 (post/get/put/patch/delete)。"""

    def post(self, *a, **k):
        return session().post(*a, **k)

    def get(self, *a, **k):
        return session().get(*a, **k)

    def put(self, *a, **k):
        return session().put(*a, **k)

    def patch(self, *a, **k):
        return session().patch(*a, **k)

    def delete(self, *a, **k):
        return session().delete(*a, **k)


http = _Facade()
