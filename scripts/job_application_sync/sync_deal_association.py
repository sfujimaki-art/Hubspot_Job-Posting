"""LISTING → 取引(Deal) 関連付けスイープ — WBS 1.11.9 要件漏れ是正 (§21.1/§3).

日次同期(hrhacker_import/airwork_import)は LISTING を作るが、取引との関連付けを
作っていなかった(要件漏れ)。そのため新規求人はDealに紐付かず、1次対応連動・
求人情報コピー(get_oubosaki_props)が空になっていた。

本スイープは、Deal関連が無いLISTINGを取引に関連付ける:
  HR: LISTING.id_shop_hrhakkaa → Deal.hrhacker_shop_ids(店舗ID群に含む)
  AW: LISTING.airwork_account_login_id → account_loader管理用メール
      → Deal.kanri_mail_address

CLI:
  python sync_deal_association.py [--dry-run|--actual] [--limit N]
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

from scripts.job_application_sync.fetchers import account_loader as al  # noqa: E402

BASE = "https://api.hubapi.com"


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}",
            "Content-Type": "application/json"}


def _search_all(obj: str, props: list, filters: list, limit=None) -> list:
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


def build_shop_to_deal() -> dict:
    """Deal.hrhacker_shop_ids(店舗ID群;区切り) → deal_id。HR用突合索引。"""
    deals = _search_all(
        "0-3", ["hrhacker_shop_ids"],
        [{"propertyName": "hrhacker_shop_ids", "operator": "HAS_PROPERTY"}])
    m = {}
    for d in deals:
        ids = (d.get("properties") or {}).get("hrhacker_shop_ids") or ""
        for sid in ids.replace(",", ";").split(";"):
            sid = sid.strip()
            if sid:
                m.setdefault(sid, d["id"])
    print(f"[deal] 店舗ID索引={len(m)} (hrhacker_shop_ids持ちDeal={len(deals)})",
          flush=True)
    return m


def build_mail_to_deal() -> dict:
    deals = _search_all(
        "0-3", ["kanri_mail_address"],
        [{"propertyName": "kanri_mail_address", "operator": "HAS_PROPERTY"}])
    m = {}
    for d in deals:
        km = ((d.get("properties") or {}).get("kanri_mail_address") or "").strip().lower()
        if km:
            m.setdefault(km, d["id"])
    print(f"[deal] 管理メール索引={len(m)}", flush=True)
    return m


def build_login_to_mail() -> dict:
    m = {}
    for a in al.iter_aw_accounts(active_only=False):
        lid = (a.get("login_id") or "").strip()
        km = (a.get("manage_mail") or "").strip().lower()
        if lid and km:
            m.setdefault(lid, km)
    return m


def _existing_deal_assoc(listing_ids: list) -> dict:
    """LISTING → Deal 関連の有無を batch/read。{lid: bool}."""
    has = {}
    for i in range(0, len(listing_ids), 100):
        chunk = listing_ids[i:i + 100]
        r = requests.post(
            f"{BASE}/crm/v4/associations/0-420/0-3/batch/read", headers=_h(),
            json={"inputs": [{"id": x} for x in chunk]}, timeout=30).json()
        for res in r.get("results", []):
            has[str(res.get("from", {}).get("id"))] = bool(res.get("to"))
        time.sleep(0.1)
    return has


def _associate(listing_id: str, deal_id: str) -> bool:
    """LISTING→Deal の default関連を作成。"""
    url = (f"{BASE}/crm/v4/objects/0-420/{listing_id}"
           f"/associations/default/0-3/{deal_id}")
    r = requests.put(url, headers=_h(), timeout=20)
    return r.status_code in (200, 201)


def run(dry_run: bool = True, limit=None) -> dict:
    listings = _search_all(
        "0-420", ["id_shop_hrhakkaa", "airwork_account_login_id", "id_hrhakkaa"],
        [{"propertyName": "hs_object_id", "operator": "HAS_PROPERTY"}], limit)
    lids = [o["id"] for o in listings]
    print(f"[listing] 対象 {len(lids)}件", flush=True)
    has = _existing_deal_assoc(lids)
    shop2deal = build_shop_to_deal()
    mail2deal = build_mail_to_deal()
    login2mail = build_login_to_mail()

    hr_ok = aw_ok = already = unresolved = 0
    for o in listings:
        lid = o["id"]
        if has.get(lid):
            already += 1
            continue
        p = o.get("properties") or {}
        deal_id = None
        shop = (p.get("id_shop_hrhakkaa") or "").strip()
        login = (p.get("airwork_account_login_id") or "").strip()
        if shop and shop in shop2deal:            # HR
            deal_id = shop2deal[shop]
            path = "hr"
        elif login:                               # AW
            km = login2mail.get(login, "")
            deal_id = mail2deal.get(km) if km else None
            path = "aw"
        if not deal_id:
            unresolved += 1
            continue
        if not dry_run:
            if not _associate(lid, deal_id):
                unresolved += 1
                continue
            time.sleep(0.05)
        if path == "hr":
            hr_ok += 1
        else:
            aw_ok += 1
    summary = {"listings": len(lids), "already_linked": already,
               "hr_associated": hr_ok, "aw_associated": aw_ok,
               "unresolved": unresolved}
    print(f"[sync_deal_association] {summary}", flush=True)
    return summary


def _args(argv=None):
    p = argparse.ArgumentParser(description="LISTING→Deal 関連付けスイープ")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _args()
    run(dry_run=a.dry_run, limit=a.limit)
