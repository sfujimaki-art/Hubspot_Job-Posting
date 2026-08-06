# -*- coding: utf-8 -*-
"""HubSpot オブジェクトの全件列挙 (2026-08-06).

なぜこれが要るか (実測):
  Search API (`POST /crm/v3/objects/{type}/search`) は **10,000件で頭打ち**になる。
  after=10000 を渡すと HTTP 400 が返り、レスポンスに results も paging も無い:

      after=9900  -> HTTP 200 results=100 next=10000
      after=10000 -> HTTP 400 "This endpoint is limited to 10000 total results"

  よくある実装は `r.json()` をそのまま使って `results` と `paging.next.after` を
  読むため、**400 が「もう次は無い」と区別できず、静かに10,000件で完走扱いになる**。
  実際 sync_ichijitaiou は対象34,666件のうち10,000件(28.8%)しか処理しておらず、
  エラーも警告も出していなかった。

  list API (`GET /crm/v3/objects/{type}`) にはこの上限が無いので、
  **フィルタ不要の全件列挙は必ずこちらを使う**。

使い分け:
  - 全件が欲しい            → list_all()          (このモジュール)
  - 条件で絞って1万件未満   → Search API でよい   (ただし total を必ず検査する)
  - 条件で絞るが1万件超     → list_all() + ローカルで絞る
"""
from __future__ import annotations

import os
import time
from typing import Iterator

import requests

BASE = "https://api.hubapi.com"


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def post_retry(url: str, body: dict, retries: int = 5, timeout: int = 60) -> dict:
    """通信断・レート制限で落ちないPOST。

    なぜ要るか: 求人34,718件の走査は batch/read 347回になる。途中で1回でも
    DNS解決やコネクションが切れると全体が失敗する。実際 2026-08-06 の実行が
    `getaddrinfo failed` で12分ぶんを捨てた。長い走査ほど1回の瞬断に弱い。

    HTTP 207 (Multi-Status) は association の batch/read が正常に返す
    コードなので成功扱いにする。
    """
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=timeout)
        except requests.RequestException as e:
            if i < retries:
                wait = 2 ** i
                print(f"    [retry {i+1}/{retries}] {type(e).__name__}: "
                      f"{wait}秒待って再試行", flush=True)
                time.sleep(wait)
                continue
            raise
        if r.status_code in (200, 201, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("retry exhausted")


def iter_all(obj_type: str, props: list, page: int = 100,
             archived: bool = False, retries: int = 4,
             progress_every: int = 5000) -> Iterator[dict]:
    """list API で全件を順に返す。10,000件上限は無い。

    obj_type: "0-420" (求人) / "0-3" (取引) など
    props   : 取得するプロパティ名
    """
    after, n = None, 0
    while True:
        params = {"limit": page, "properties": ",".join(props),
                  "archived": str(bool(archived)).lower()}
        if after:
            params["after"] = after
        for i in range(retries + 1):
            try:
                r = requests.get(f"{BASE}/crm/v3/objects/{obj_type}",
                                 headers=_h(), params=params, timeout=60)
            except requests.RequestException:
                if i < retries:
                    time.sleep(2 ** i)
                    continue
                raise
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503, 504) and i < retries:
                time.sleep(2 ** i * 2)
                continue
            # ここで黙って抜けない。静かな打ち切りこそが直したい欠陥。
            raise RuntimeError(
                f"list {obj_type} HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        results = j.get("results") or []
        if not results:
            return
        for o in results:
            yield o
        n += len(results)
        if progress_every and n % progress_every < page:
            print(f"  [list {obj_type}] {n:,}件", flush=True)
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return
        time.sleep(0.08)


def list_all(obj_type: str, props: list, limit: int | None = None,
             **kw) -> list:
    """iter_all をリストで返す薄いラッパ。limit は打ち切り件数(検証用)。"""
    out = []
    for o in iter_all(obj_type, props, **kw):
        out.append(o)
        if limit and len(out) >= limit:
            break
    return out


def search_all(obj_type: str, props: list, filters: list,
               limit: int | None = None, page: int = 100,
               retries: int = 4) -> list:
    """Search API 版。**10,000件を超える見込みなら使わないこと**。

    従来実装との違いは、上限に触れたら黙って完走扱いにせず例外を投げること。
    呼び出し側が「絞り込みが足りない」と気づけるようにするため。
    """
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}],
                "properties": props, "limit": page}
        if after:
            body["after"] = after
        for i in range(retries + 1):
            r = requests.post(f"{BASE}/crm/v3/objects/{obj_type}/search",
                              headers=_h(), json=body, timeout=60)
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503, 504) and i < retries:
                time.sleep(2 ** i * 2)
                continue
            if r.status_code == 400 and "10000" in r.text:
                raise RuntimeError(
                    f"search {obj_type}: 10,000件上限に到達 (取得済 {len(out):,}件)。"
                    " 絞り込みを足すか list_all() を使うこと")
            raise RuntimeError(
                f"search {obj_type} HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        out += j.get("results") or []
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after or (limit and len(out) >= limit):
            return out
        time.sleep(0.1)
