"""1次対応フラグ 連動 (Deal → LISTING) — WBS 1.11.9 データ整備.

取引(Deal)の itijitaiou(一次対応オプション true/false) を、紐づく求人(LISTING)の
ichijitaiounoumu_deforuto(一次対応の有無_デフォルト 必要/不要) に反映する。
その後 応募連携の link/relink が LISTING → APPOINTMENT へコピーする。

連携キー(実データ確認済 2026-07-08):
  LISTING(0-420) と Deal(0-3) は **直接の Association** で連携済み
  (実HR求人は Deal に関連あり)。本スクリプトはこの association を辿る。

マッピング: 関連Dealの itijitaiou に true があれば 必要 / false のみなら 不要 /
           設定なしなら 触らない(unset維持)。

CLI:
  python sync_ichijitaiou.py [--dry-run|--actual] [--limit N]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_ENV = _REPO / ".env"
if _ENV.exists():
    load_dotenv(_ENV)

BASE = "https://api.hubapi.com"


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}",
            "Content-Type": "application/json"}


def _search_all(obj: str, props: list[str], filters: list[dict],
                limit: int | None = None) -> list[dict]:
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}],
                "properties": props, "limit": 100}
        if after:
            body["after"] = after
        r = requests.post(f"{BASE}/crm/v3/objects/{obj}/search",
                          headers=_h(), json=body, timeout=30).json()
        out += r.get("results", [])
        after = r.get("paging", {}).get("next", {}).get("after")
        if not after or (limit and len(out) >= limit):
            break
        time.sleep(0.1)
    return out


def _batch_assoc(listing_ids: list[str]) -> dict:
    """LISTING → Deal の関連を batch/read で取得. {listing_id: [deal_id,...]}."""
    m: dict = {}
    for i in range(0, len(listing_ids), 100):
        chunk = listing_ids[i:i + 100]
        r = requests.post(
            f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
            headers=_h(),
            json={"inputs": [{"id": x} for x in chunk]}, timeout=30).json()
        for res in r.get("results", []):
            fid = str(res.get("from", {}).get("id"))
            m[fid] = [str(t.get("toObjectId")) for t in res.get("to", [])]
        time.sleep(0.1)
    return m


def _batch_deal_itijitaiou(deal_ids: list[str]) -> dict:
    """Deal の itijitaiou を batch/read. {deal_id: 'true'/'false'/None}."""
    m: dict = {}
    ids = sorted(set(deal_ids))
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = requests.post(
            f"{BASE}/crm/v3/objects/0-3/batch/read", headers=_h(),
            json={"properties": ["itijitaiou"],
                  "inputs": [{"id": x} for x in chunk]}, timeout=30).json()
        for o in r.get("results", []):
            m[str(o["id"])] = (o.get("properties") or {}).get("itijitaiou")
        time.sleep(0.1)
    return m


def run(dry_run: bool = True, limit: int | None = None) -> dict:
    # 1) 全LISTING (現状の ichijitaiounoumu_deforuto 込み)
    listings = _search_all(
        "0-420", ["ichijitaiounoumu_deforuto"],
        [{"propertyName": "hs_object_id", "operator": "HAS_PROPERTY"}], limit)
    lids = [o["id"] for o in listings]
    print(f"[listing] 対象 {len(lids)}件", flush=True)
    # 2) LISTING→Deal 関連 + Deal.itijitaiou をバッチ取得
    assoc = _batch_assoc(lids)
    all_deals = [d for ds in assoc.values() for d in ds]
    deal_flag = _batch_deal_itijitaiou(all_deals)
    print(f"[assoc] Deal関連ありLISTING={sum(1 for v in assoc.values() if v)} "
          f"/ 参照Deal={len(set(all_deals))}", flush=True)
    # 3) 各LISTINGの想定値を決定 (関連Dealに true→必要 / false→不要 / なし→触らない)
    updates = []
    no_deal = matched = 0
    for o in listings:
        deals = assoc.get(o["id"], [])
        if not deals:
            no_deal += 1
            continue
        flags = [deal_flag.get(d) for d in deals]
        if "true" in flags:
            want = "必要"
        elif "false" in flags:
            want = "不要"
        else:
            continue                          # Deal未設定 → 触らない
        matched += 1
        if (o.get("properties") or {}).get("ichijitaiounoumu_deforuto") == want:
            continue                          # 既に一致
        updates.append({"id": o["id"],
                        "properties": {"ichijitaiounoumu_deforuto": want}})
    # 4) batch update
    applied = 0
    if not dry_run:
        for i in range(0, len(updates), 100):
            r = requests.post(f"{BASE}/crm/v3/objects/0-420/batch/update",
                              headers=_h(),
                              json={"inputs": updates[i:i + 100]}, timeout=60)
            if r.status_code in (200, 201, 207):
                applied += len(updates[i:i + 100])
            time.sleep(0.15)
    summary = {"listings": len(lids), "deal_matched": matched,
               "no_deal_assoc": no_deal, "to_update": len(updates),
               "applied": applied}
    print(f"[sync_ichijitaiou] {summary}", flush=True)
    return summary


def _args(argv=None):
    p = argparse.ArgumentParser(description="1次対応 Deal→LISTING 連動 (association経由)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _args()
    run(dry_run=a.dry_run, limit=a.limit)
