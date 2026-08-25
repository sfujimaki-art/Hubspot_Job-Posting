# -*- coding: utf-8 -*-
"""顧客シートへ「読めるかどうか」だけを確かめる (2026-08-26)。

## なぜ要るか

顧客の応募者管理シートへの転記は、いま `CUSTOMER_SHEET_ALLOW` (GitHub Secrets)
に載せた12社だけで動いている。この許可リストをやめて
**「サービスアカウントに共有された時点で許可とみなす」**設計へ移せるかを判断
したい。

その可否は、候補の顧客シートのうち**何社が既にSAへ共有されているか**で決まる:

    ほとんど共有されていない → 共有が許可として機能する (段階的に開く)
    ほとんど共有済み         → 切り替えた瞬間に一斉に開く (別途判断が要る)

転記用SA (`CUSTOMER_SHEET_SA_JSON`) の鍵は Secrets の中にしか無く、手元から
は測れない。だから本番で1回だけ流して数える。

## 安全のために守っていること

- **読み取りだけ**。書き込みも、タブ作成も、一切しない
- **ログに顧客名・シートID・URLを出さない**。本番リポジトリは public であり、
  Actions のログも public になる。出すのは件数と割合だけ
- 対象は Secrets やファイルからでなく **HubSpot の `customer_sheet_url`** から
  組み立てる。許可リストをもう1つ作らないため

使い方:
  python scripts/job_application_sync/probe_customer_sheet_access.py
  python scripts/job_application_sync/probe_customer_sheet_access.py --all-status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")

from scripts.job_application_sync import deal_stages as DS       # noqa: E402
from scripts.job_application_sync.hs_paging import post_retry    # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://api.hubapi.com"
RE_SHEET_ID = re.compile(r"/d/([A-Za-z0-9_-]{20,})")

# 一時的な失敗はここで吸収する。数え間違いは判断を誤らせる。
RETRY_CODES = (429, 500, 502, 503, 504)
RETRIES = 3


def sheet_id(url: str) -> str:
    m = RE_SHEET_ID.search(url or "")
    return m.group(1) if m else ""


def classify(status: int | None, err: str = "") -> str:
    """HTTPの結果を、判断に使える日本語の区分へ落とす."""
    if status is None:
        return "その他の失敗"
    if status == 200:
        return "読める(共有済み)"
    if status == 403:
        return "読めない(共有されていない)"
    if status == 404:
        return "読めない(シートが存在しない)"
    return f"読めない({status})"


def collect_targets(open_only: bool = True) -> dict:
    """HubSpot から {シートID: 公開中の求人数} を組み立てる。

    生きた取引に紐付く求人だけを見る。跡地にぶら下がったままの求人は
    掲載終了の処理待ちで、転記の対象を判断する材料にならない。
    """
    fg = [{"filters": [{"propertyName": "kyuujin_status",
                        "operator": "EQ", "value": "公開中"}]}] if open_only else []
    body_base = {"limit": 100,
                 "properties": ["customer_sheet_url"],
                 "sorts": [{"propertyName": "hs_object_id",
                            "direction": "ASCENDING"}]}
    if fg:
        body_base["filterGroups"] = fg
    total = post_retry(f"{BASE}/crm/v3/objects/0-420/search",
                       {**body_base, "limit": 1}).get("total", 0)
    listings, after = {}, None
    while True:
        b = dict(body_base)
        if after:
            b["after"] = after
        j = post_retry(f"{BASE}/crm/v3/objects/0-420/search", b)
        for o in j.get("results", []):
            listings[o["id"]] = (o.get("properties") or {}).get(
                "customer_sheet_url") or ""
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.04)
    if len(listings) != total:
        raise RuntimeError(
            f"求人の取得漏れ: {len(listings)}/{total}件。"
            "数え落としたまま判断すると、共有済みの社数を過小評価する")
    print(f"公開中の求人 {len(listings):,}件", flush=True)

    # 生きた取引に紐付くものだけに絞る
    ids = sorted(listings)
    l2d: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for row in r.get("results", []):
            l2d[str(row["from"]["id"])] = [str(t["toObjectId"])
                                           for t in row.get("to", [])]
        time.sleep(0.03)
    dids = sorted({x for v in l2d.values() for x in v})
    stage: dict = {}
    for i in range(0, len(dids), 100):
        r = post_retry(f"{BASE}/crm/v3/objects/0-3/batch/read",
                       {"inputs": [{"id": x} for x in dids[i:i + 100]],
                        "properties": ["dealstage"]})
        for o in r.get("results", []):
            stage[str(o["id"])] = (o.get("properties") or {}).get("dealstage")
        time.sleep(0.02)

    out: Counter = Counter()
    for lid, url in listings.items():
        if not any(DS.is_writable(stage.get(d)) for d in l2d.get(lid, [])):
            continue
        sid = sheet_id(url)
        if sid:
            out[sid] += 1
    print(f"  うち生きた取引に紐付き、シートURLを持つ: "
          f"{sum(out.values()):,}件 / シート {len(out):,}種", flush=True)
    return dict(out)


def probe(sheet_ids: list) -> dict:
    """各シートへ metadata 読み取りだけを試す。書き込みは一切しない."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    sa = os.environ.get("CUSTOMER_SHEET_SA_JSON", "")
    if not sa or not os.path.exists(sa):
        raise RuntimeError(
            "CUSTOMER_SHEET_SA_JSON が未設定です。転記に使うSAで測らないと"
            "意味がありません (別のSAで測ると共有状況を読み違えます)")
    info = json.loads(Path(sa).read_text(encoding="utf-8"))
    # ★SAのアドレスはログに出す。どのSAで測ったかが分からないと結果を信用できない。
    #   これは自社のサービスアカウントであり、顧客の情報ではない。
    print(f"測定に使うサービスアカウント: {info.get('client_email')}", flush=True)

    cred = service_account.Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=cred, cache_discovery=False)

    res: Counter = Counter()
    has_tab = 0
    for sid in sheet_ids:
        kind = "その他の失敗"
        for attempt in range(RETRIES + 1):
            try:
                m = svc.spreadsheets().get(
                    spreadsheetId=sid,
                    fields="sheets.properties.title").execute()
                kind = "読める(共有済み)"
                tabs = [s["properties"]["title"] for s in m.get("sheets", [])]
                if "応募一覧(自動)" in tabs:
                    has_tab += 1
                break
            except HttpError as e:      # noqa: PERF203
                code = getattr(e.resp, "status", None)
                if code in RETRY_CODES and attempt < RETRIES:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                kind = classify(code)
                break
            except Exception:           # noqa: BLE001
                if attempt < RETRIES:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
        res[kind] += 1
        time.sleep(0.05)
    return {"result": dict(res), "応募一覧タブあり": has_tab}


def main(open_only: bool) -> int:
    targets = collect_targets(open_only)
    if not targets:
        print("対象のシートがありません", flush=True)
        return 0
    sids = sorted(targets)
    print(f"\n読み取りを試すシート: {len(sids):,}種 (書き込みはしません)",
          flush=True)
    out = probe(sids)
    r = out["result"]
    n = sum(r.values())
    print(f"\n=== 結果 (シート {n:,}種) ===", flush=True)
    for k, v in sorted(r.items(), key=lambda x: -x[1]):
        print(f"   {k:<28} {v:>5,}種 ({v / n * 100:5.1f}%)", flush=True)
    ok = r.get("読める(共有済み)", 0)
    jobs_ok = sum(c for s, c in targets.items())
    print(f"\n   共有済みのうち「応募一覧(自動)」タブが既にある: "
          f"{out['応募一覧タブあり']:,}種", flush=True)
    print(f"\n=== 判断の材料 ===", flush=True)
    print(f"   いま許可されているのは 12種。共有済みは {ok:,}種。", flush=True)
    if n:
        print(f"   共有をもって許可とみなすと、対象は 12種 → {ok:,}種 になります。",
              flush=True)
    print(f"   (対象の求人は延べ {jobs_ok:,}件。個別の顧客名・シートIDは"
          "公開ログに出さないため表示しません)", flush=True)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--all-status", action="store_true",
                   help="公開中でない求人のシートも対象にする")
    return p.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    sys.exit(main(open_only=not a.all_status))
