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
from scripts.job_application_sync.hs_paging import list_all  # noqa: E402


# Windowsローカルの既定は cp932。ログ出力の1文字で処理全体が落ちるのは
# 本末転倒なので明示的に固定する (CIは PYTHONIOENCODING=utf-8 で問題ない)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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
        raw = ((d.get("properties") or {}).get("kanri_mail_address") or "")
        # ★1取引に複数アドレスが入る (";" or "," 区切り)。丸ごと1キーにすると
        #   個別アドレスで引けない (2026-08-10 実測: 複数持ちの取引27件=全体の1%、
        #   分割しないことで引けなくなるキーが15個)。sync_ichijitaiou は
        #   既に分割しており、こちらだけ揃っていなかった。
        for km in raw.replace(",", ";").split(";"):
            km = km.strip().lower()
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


def _post_retry(url: str, body: dict, retries: int = 5) -> dict:
    """通信断・レート制限で落ちないPOST。

    34,718件の走査は347回のリクエストになるため、途中で1回でも
    DNS解決やコネクションが落ちると全体が失敗する。実際に
    2026-08-06 の実行が getaddrinfo failed で中断した。
    ネットワーク例外とHubSpot側の一時エラーは待って再試行する。
    """
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=60)
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
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


def _existing_deal_assoc(listing_ids: list) -> dict:
    """LISTING → Deal 関連を batch/read。{lid: deal_id} (無関連は載らない)."""
    has = {}
    for i in range(0, len(listing_ids), 100):
        chunk = listing_ids[i:i + 100]
        r = _post_retry(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                        {"inputs": [{"id": x} for x in chunk]})
        if (i // 100) % 50 == 0:
            print(f"  [assoc] {i:,}/{len(listing_ids):,}", flush=True)
        for res in r.get("results", []):
            to = res.get("to") or []
            if to:
                ids = [str(t.get("toObjectId")) for t in to if t.get("toObjectId")]
                # ★最新の取引を採用 (2026-08-06 是正): association配列は
                # 作成順(最古が先頭)のため [0] だと終了した契約を掴む。
                # owner継承が古い担当者を引くのを防ぐ。
                has[str(res.get("from", {}).get("id"))] = (
                    _latest_of(ids) if len(ids) > 1 else ids[0])
        time.sleep(0.1)
    return has


def _latest_of(deal_ids: list) -> str:
    """複数取引から最新を選ぶ。取得失敗時は先頭(従来動作)へフォールバック。"""
    try:
        from . import deal_series as _ds
    except ImportError:  # pragma: no cover — CIはスクリプト直実行
        import deal_series as _ds  # type: ignore
    try:
        r = requests.post(f"{BASE}/crm/v3/objects/0-3/batch/read", headers=_h(),
                          json={"inputs": [{"id": x} for x in deal_ids],
                                "properties": ["dealname", "createdate",
                                               "contract_start_date"]},
                          timeout=30)
        r.raise_for_status()
        props = {o["id"]: (o.get("properties") or {})
                 for o in r.json().get("results", [])}
        return _ds.latest_from_associations(deal_ids, props) or deal_ids[0]
    except Exception:  # noqa: BLE001
        return deal_ids[0]


def _associate(listing_id: str, deal_id: str, retries: int = 4) -> bool:
    """LISTING→Deal の default関連を作成。

    ★リトライ必須: 数千件を1件ずつPUTするため、途中の1回が通信断で落ちると
      それまでの処理ごと失われる。実際 2026-08-06 の実行が
      ConnectionResetError(10054) で中断した。
    """
    url = (f"{BASE}/crm/v4/objects/0-420/{listing_id}"
           f"/associations/default/0-3/{deal_id}")
    for i in range(retries + 1):
        try:
            r = requests.put(url, headers=_h(), timeout=30)
        except requests.RequestException as e:
            if i < retries:
                print(f"    [retry {i+1}/{retries}] {type(e).__name__}: "
                      f"{2 ** i}秒待って再試行", flush=True)
                time.sleep(2 ** i)
                continue
            return False
        if r.status_code in (200, 201):
            return True
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        return False
    return False


def run(dry_run: bool = True, limit=None) -> dict:
    _props = ["id_shop_hrhakkaa", "airwork_account_login_id", "id_hrhakkaa",
              "hubspot_owner_id"]
    # Search API は10,000件で HTTP 400 になり、素朴な実装では「もう次が無い」と
    # 区別できず静かに完走扱いになる。以前は「直近14日分を別窓で足す」緩和策を
    # 入れていたが(2026-07-27)、15日以上前に作られて紐付いていない求人は永久に
    # 拾えないままだった。上限の無い list API に替えて根本を断つ(2026-08-06)。
    listings = list_all("0-420", _props, limit=limit)
    lids = [o["id"] for o in listings]
    print(f"[listing] 対象 {len(lids)}件", flush=True)
    has = _existing_deal_assoc(lids)
    shop2deal = build_shop_to_deal()
    mail2deal = build_mail_to_deal()
    login2mail = build_login_to_mail()

    hr_ok = aw_ok = already = unresolved = 0
    created = {}   # 今回作成した lid→deal_id (所有者継承パスで使用)
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
        created[lid] = str(deal_id)
        if path == "hr":
            hr_ok += 1
        else:
            aw_ok += 1

    # ── 所有者継承 (2026-07-27 ユーザー要望): LISTING.owner が空なら親Deal.owner ──
    # 取引→求人→応募者チェーンの1段目。2段目(求人→応募者)は applicant_import/relink。
    pairs = dict(has)
    pairs.update(created)
    deal_ids = sorted(set(pairs.values()))
    downer = {}
    for i in range(0, len(deal_ids), 100):
        chunk = deal_ids[i:i + 100]
        r = requests.post(
            f"{BASE}/crm/v3/objects/0-3/batch/read", headers=_h(),
            json={"inputs": [{"id": d} for d in chunk],
                  "properties": ["hubspot_owner_id"]}, timeout=30).json()
        for o in r.get("results", []):
            v = (o.get("properties") or {}).get("hubspot_owner_id")
            if v:
                downer[str(o["id"])] = v
        time.sleep(0.1)
    lowner = {o["id"]: (o.get("properties") or {}).get("hubspot_owner_id")
              for o in listings}
    to_set = [(lid, downer[did]) for lid, did in pairs.items()
              if did in downer and not lowner.get(lid)]
    owner_set = 0
    if not dry_run:
        for i in range(0, len(to_set), 100):
            chunk = to_set[i:i + 100]
            r = requests.post(
                f"{BASE}/crm/v3/objects/0-420/batch/update", headers=_h(),
                json={"inputs": [
                    {"id": lid, "properties": {"hubspot_owner_id": ow}}
                    for lid, ow in chunk]}, timeout=30)
            if r.ok:
                owner_set += len(chunk)
            time.sleep(0.2)
    summary = {"listings": len(lids), "already_linked": already,
               "hr_associated": hr_ok, "aw_associated": aw_ok,
               "unresolved": unresolved,
               "owner_target": len(to_set), "owner_set": owner_set}
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
