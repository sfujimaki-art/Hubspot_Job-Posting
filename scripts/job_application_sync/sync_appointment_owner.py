# -*- coding: utf-8 -*-
"""HRハッカー応募の担当者を、取引の「コンサル担当」に毎晩合わせる (2026-11-12)。

## なぜ要るか

担当の変更は取引で行う。応募の担当者 (hubspot_owner_id) はこれまで求人の Owner
(夜間に取引担当者へ合わせた値) を作成時に写していたため、今のコンサル担当ではなかった
(2026-11-12 実測: 11/03 以降の HR 応募 1,000 件のうち 272 件で担当者と担当コンサルタントが別の人)。
作成時は applicant_import.applicant_owner_id が取引のコンサル担当を入れる。
このスクリプトは、作成後に取引の担当が変わった応募を追いかける。

## 何をするか (毎晩)

直近 WINDOW_DAYS 日に作られた HRハッカー応募について、
応募 → 求人 → 取引群 (作成時と同じ規則: backfill_appointment_anmokuchi.deal_groups) の
「今の契約」(deal_master.latest_live) のコンサル担当を求め、

  - 担当者 (hubspot_owner_id)        ← コンサル担当
  - 担当コンサルタント (dandangkonsarutanto) ← コンサル担当の姓名

が違えば書き換える。

## 守ること

- コンサル担当が決まらない (求人が無い・取引が無い・別会社にまたがる・空) 応募は触らない。
- コンサル担当が無効なユーザー (退職など) なら触らない (HubSpot が受け付けないため)。
- 窓は直近 WINDOW_DAYS 日 (既定 90 日、環境変数 JAS_OWNER_SYNC_DAYS)。
- 既定はドライラン。--actual で書き込む。失敗があれば非0で終わる。
- ログに氏名を出さない (リポジトリと Actions のログは公開)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:  # CI は env 直渡しなので dotenv 無しでも動く
    pass

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import requests  # noqa: E402

try:
    from scripts.job_application_sync.hs_paging import (  # noqa: E402
        search_all_by_id, post_retry, _h)
    from scripts.job_application_sync import applicant_import as AI  # noqa: E402
    from scripts.job_application_sync import backfill_appointment_anmokuchi as BA  # noqa: E402
except ImportError:  # script直実行
    from hs_paging import search_all_by_id, post_retry, _h  # type: ignore
    import applicant_import as AI  # type: ignore
    import backfill_appointment_anmokuchi as BA  # type: ignore

BASE = "https://api.hubapi.com"
APPOINTMENT = "0-421"
WINDOW_DAYS = int(os.environ.get("JAS_OWNER_SYNC_DAYS", "90"))
APPT_PROPS = ["hubspot_owner_id", "dandangkonsarutanto"]


def window_start(now=None, days: int = WINDOW_DAYS) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_for_appt(appt: dict, group_ids: list, deals: dict,
                  active: dict) -> tuple:
    """純関数: (書くプロパティ, 理由)。理由は change / same / no_consultant / inactive。

    appt: 応募の現在値 / group_ids: 応募の取引群 / deals: {id: props}
    active: 有効なユーザー {owner_id: 姓名} (姓名が取れなければ None)
    """
    cid = AI.consultant_owner_id(group_ids, deals)
    if not cid:
        return {}, "no_consultant"
    if cid not in active:
        return {}, "inactive"
    out: dict = {}
    if str(appt.get("hubspot_owner_id") or "").strip() != cid:
        out["hubspot_owner_id"] = cid
    name = active.get(cid)
    if name and str(appt.get("dandangkonsarutanto") or "").strip() != name:
        out["dandangkonsarutanto"] = name
    return out, ("change" if out else "same")


def _get_retry(url: str, retries: int = 4) -> dict:
    """GET。通信断・429・5xx は待ってやり直す (post_retry と同じ考え方)。"""
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=_h(), timeout=30)
        except requests.RequestException:
            if i < retries:
                time.sleep(2 ** i)
                continue
            raise
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}")
    raise RuntimeError("retry exhausted")


def _why(e: Exception) -> str:
    """ログ用の失敗理由。HTTP の状態コードだけ (本文は値を含み得るので出さない)。"""
    head = str(e).split(":", 1)[0]
    return head if head.startswith("HTTP ") else type(e).__name__


def active_owners() -> dict:
    """有効なユーザー {owner_id: 姓名}。owners API (archived=false) を全ページ読む。"""
    out: dict = {}
    after = None
    while True:
        url = f"{BASE}/crm/v3/owners?limit=500&archived=false"
        if after:
            url += f"&after={after}"
        j = _get_retry(url)
        for o in j.get("results") or []:
            out[str(o["id"])] = AI.owner_display_name(o)
        after = ((j.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return out


def collect(since: str) -> tuple:
    """(対象応募{id: props}, 応募→取引群, 取引{id: props}, 内訳)。"""
    apps = {str(r["id"]): r.get("properties") or {} for r in search_all_by_id(
        APPOINTMENT, APPT_PROPS,
        [{"propertyName": "hs_createdate", "operator": "GTE", "value": since},
         {"propertyName": "oubobaitaimei", "operator": "EQ",
          "value": AI.HR_MEDIA}])}
    groups, deals, stat = BA.deal_groups(list(apps))
    stat["window"] = len(apps)
    return apps, groups, deals, stat


def _update(items: list) -> tuple:
    """100件ずつ batch/update。バッチが失敗したら1件ずつやり直す。(成功, 失敗)。"""
    ok = failed = 0
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        try:
            r = post_retry(f"{BASE}/crm/v3/objects/{APPOINTMENT}/batch/update",
                           {"inputs": [{"id": k, "properties": v} for k, v in chunk]})
            done = {str(x["id"]) for x in r.get("results", [])}
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] バッチ更新失敗 {len(chunk)}件 → 1件ずつやり直す: "
                  f"{_why(e)}", flush=True)
            done = set()
            for k, v in chunk:
                try:
                    post_retry(f"{BASE}/crm/v3/objects/{APPOINTMENT}/batch/update",
                               {"inputs": [{"id": k, "properties": v}]})
                    done.add(k)
                except Exception as e2:  # noqa: BLE001
                    print(f"  [warn] 更新できず 応募={k}: {_why(e2)}", flush=True)
        ok += sum(1 for k, _ in chunk if k in done)
        failed += sum(1 for k, _ in chunk if k not in done)
        time.sleep(0.2)
    return ok, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="既定 (書き込まない)")
    a = ap.parse_args(argv)

    since = window_start()
    print(f"=== HR応募の担当者を取引のコンサル担当に合わせる ({since[:10]} 以降 / "
          f"{'actual' if a.actual else 'dry-run'}) ===", flush=True)
    apps, groups, deals, stat = collect(since)
    active = active_owners()
    plans: dict = {}
    for appt_id, p in apps.items():
        pl, why = plan_for_appt(p, groups.get(appt_id, []), deals, active)
        stat[why] += 1
        if pl:
            plans[appt_id] = pl
            if "hubspot_owner_id" in pl:
                stat["owner_change"] += 1
                if not str(p.get("hubspot_owner_id") or "").strip():
                    stat["owner_fill"] += 1     # 空だった担当者を埋める
            if "dandangkonsarutanto" in pl:
                stat["name_change"] += 1
    print(f"対象期間の HR 応募 {stat['window']:,}件 / 合っている {stat['same']:,}"
          f" / 書き換える {stat['change']:,} (担当者 {stat['owner_change']:,}"
          f" [うち空を埋める {stat['owner_fill']:,}]・"
          f"担当コンサルタント {stat['name_change']:,})", flush=True)
    print(f"  触らない: コンサル担当が決まらない {stat['no_consultant']:,}"
          f" (求人未紐付け {stat['no_listing']} / 求人に取引なし {stat['no_deal']}"
          f" / 別会社とまたがる {stat['shared_shop']}"
          f" / 取引はあるがコンサル担当が空など"
          f" {stat['no_consultant'] - stat['no_listing'] - stat['no_deal'] - stat['shared_shop']})"
          f" / コンサル担当が無効なユーザー {stat['inactive']:,}", flush=True)
    if not a.actual:
        for appt_id, pl in list(plans.items())[:20]:
            print(f"  [dry] 応募 {appt_id} ← {sorted(pl)}", flush=True)
        return 0
    ok, failed = _update(list(plans.items()))
    print(f"=== 結果 === 更新 {ok} / 失敗 {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
