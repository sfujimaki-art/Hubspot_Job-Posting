"""1次対応フラグ 連動 (Deal → LISTING) — WBS 1.11.9 データ整備.

取引(Deal)の itijitaiou(一次対応オプション true/false) を、紐づく求人(LISTING)の
ichijitaiounoumu_deforuto(一次対応の有無_デフォルト 必要/不要) に反映する。
その後 応募連携の link/relink が LISTING → APPOINTMENT へコピーする。

連携キー(実データ確認済 2026-07-08):
  LISTING(0-420) と Deal(0-3) は **直接の Association** で連携済み
  (実HR求人は Deal に関連あり)。本スクリプトはこの association を辿る。

マッピング (2026-10-02 改): 求人に紐付く取引 **と同じ取引先コードの取引** の
  うち、生きている取引の最新 (deal_master.latest_live) の itijitaiou に合わせる。
  true → 必要 / false → 不要 / 空 → 触らない(unset維持)。

  旧: 紐付く取引のどれか1件が true なら必要 (true優先)。
      求人を同じ取引先コードの取引すべてに紐付ける方針 (2026-10-01 定例MTG)
      にすると、旧契約が「必要」・新契約が「不要」でも必要のまま残る
      (2026-10-01 実測: 33コードで発生)。今の契約の値だけを見る。

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

from scripts.job_application_sync.fetchers import account_loader as al  # noqa: E402
from scripts.job_application_sync.hs_paging import (  # noqa: E402
    list_all, post_retry, search_all_by_id)
from scripts.job_application_sync import deal_master as DM  # noqa: E402


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
        # 347回のbatch/readを走らせるので、1回の瞬断で全体を落とさない
        r = post_retry(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                       {"inputs": [{"id": x} for x in chunk]})
        for res in r.get("results", []):
            fid = str(res.get("from", {}).get("id"))
            m[fid] = [str(t.get("toObjectId")) for t in res.get("to", [])]
        time.sleep(0.1)
    return m


PIPELINE = "21596025"   # 納品管理
DEAL_PROPS = ["itijitaiou", "dealstage", "contract_start_date", "createdate",
              DM.PROP_CODE, "kanri_mail_address"]


def _batch_deals(deal_ids: list[str]) -> dict:
    """Deal を batch/read. {deal_id: properties}."""
    m: dict = {}
    ids = sorted(set(deal_ids))
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = post_retry(f"{BASE}/crm/v3/objects/0-3/batch/read",
                       {"properties": DEAL_PROPS,
                        "inputs": [{"id": x} for x in chunk]})
        for o in r.get("results", []):
            m[str(o["id"])] = o.get("properties") or {}
        time.sleep(0.1)
    return m


def load_pipeline_deals() -> dict:
    """納品管理PLの全取引 {deal_id: properties}。同コードの取引を引くのに使う。"""
    ds = search_all_by_id("0-3", DEAL_PROPS, [
        {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE}])
    return {str(d["id"]): d.get("properties") or {} for d in ds}


def expand_by_code(deal_ids, deals: dict, by_code: dict) -> list:
    """紐付く取引 + それと同じ取引先コードの取引 (納品管理PL内)。"""
    out = list(dict.fromkeys(deal_ids))
    for d in list(out):
        c = str((deals.get(d) or {}).get(DM.PROP_CODE) or "").strip()
        for x in by_code.get(c, []) if c else []:
            if x not in out:
                out.append(x)
    return out


def decide_want(deal_ids, deals: dict) -> str | None:
    """取引群 → 必要/不要/None(触らない)。生きている取引の最新の値だけを見る。"""
    latest = DM.latest_live(deal_ids, deals)
    v = (deals.get(latest) or {}).get("itijitaiou") if latest else None
    if v == "true":
        return "必要"
    if v == "false":
        return "不要"
    return None


def build_mail_to_deals(deals: dict) -> dict:
    """管理用メールアドレス(小文字) → [deal_id]。複数アドレスは ; , で分割。

    旧実装は同一メールに複数Dealがあれば true を優先していた。
    今は取引IDを全部持ち、decide_want (latest_live) で今の契約を選ぶ。
    """
    m: dict = {}
    for did, p in deals.items():
        raw = (p.get("kanri_mail_address") or "")
        for km in raw.replace(",", ";").split(";"):
            km = km.strip().lower()
            if km:
                m.setdefault(km, []).append(did)
    print(f"[deal] 管理用メール索引={len(m)}", flush=True)
    return m


def build_login_to_mail() -> dict:
    """AW login_id(企業ID) → 管理用メールアドレス(小文字)。account_loaderから。"""
    m: dict = {}
    for a in al.iter_aw_accounts(active_only=False):
        lid = (a.get("login_id") or "").strip()
        km = (a.get("manage_mail") or "").strip().lower()
        if lid and km:
            m.setdefault(lid, km)
    print(f"[sheet] AW login_id->管理用メール索引={len(m)}", flush=True)
    return m


def run(dry_run: bool = True, limit: int | None = None) -> dict:
    # 1) 全LISTING (現状値 + AW判定用 login_id)
    # Search API は10,000件で HTTP 400 になり、素朴な実装だと「もう次が無い」と
    # 区別できず静かに完走扱いになる。実測では対象34,666件のうち10,000件
    # (28.8%)しか処理していなかった (2026-08-06 発見)。上限の無い list API へ。
    listings = list_all(
        "0-420", ["ichijitaiounoumu_deforuto", "airwork_account_login_id"],
        limit=limit)
    lids = [o["id"] for o in listings]
    print(f"[listing] 対象 {len(lids)}件", flush=True)
    # 2) HR経路: LISTING→Deal 関連(HubSpotの関連付け)
    assoc = _batch_assoc(lids)
    deals = load_pipeline_deals()
    missing = sorted({d for ds in assoc.values() for d in ds} - set(deals))
    deals.update(_batch_deals(missing))     # 納品管理PL外の取引も値は読む
    by_code = DM.group_by_code(
        {d: p for d, p in deals.items() if d not in set(missing)})
    print(f"[assoc] Deal関連ありLISTING={sum(1 for v in assoc.values() if v)} "
          f"/ 納品管理PL取引={len(deals) - len(missing)} / PL外={len(missing)}",
          flush=True)
    # 2b) AW経路: 管理用メールアドレス経由の索引 (login_id→メール→取引)
    login2mail = build_login_to_mail()
    mail2deals = build_mail_to_deals(
        {d: p for d, p in deals.items() if d not in set(missing)})
    # 3) 各LISTINGの想定値を決定
    updates = []
    hr_matched = aw_matched = unresolved = 0
    for o in listings:
        p = o.get("properties") or {}
        linked = assoc.get(o["id"], [])
        want = None
        if linked:                             # HR経路: 関連付けをたどる
            want = decide_want(expand_by_code(linked, deals, by_code), deals)
            if want:
                hr_matched += 1
        else:                                  # AW経路: 管理用メールで取引を探す
            login = (p.get("airwork_account_login_id") or "").strip()
            km = login2mail.get(login, "")
            cands = mail2deals.get(km, []) if km else []
            want = (decide_want(expand_by_code(cands, deals, by_code), deals)
                    if cands else None)
            if want:
                aw_matched += 1
        if not want:
            unresolved += 1
            continue
        if p.get("ichijitaiounoumu_deforuto") == want:
            continue                           # 既に一致=スキップ
        updates.append({"id": o["id"],
                        "properties": {"ichijitaiounoumu_deforuto": want}})
    # 4) batch update
    applied = 0
    if not dry_run:
        for i in range(0, len(updates), 100):
            post_retry(f"{BASE}/crm/v3/objects/0-420/batch/update",
                       {"inputs": updates[i:i + 100]})
            applied += len(updates[i:i + 100])
            time.sleep(0.15)
    summary = {"listings": len(lids), "hr_matched": hr_matched,
               "aw_matched": aw_matched, "unresolved": unresolved,
               "to_update": len(updates), "applied": applied}
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
