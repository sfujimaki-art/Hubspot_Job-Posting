# -*- coding: utf-8 -*-
"""HR求人の持ち主 (取引先コード) を判定し、求人のシステム専用項目へ書く (2026-10-05)。

## なぜ要るか

HRハッカーでは1つの店舗に複数の会社の求人が入る。取引の店舗IDも
backfill_deal_shop_id_via_email が通知先メールから足していくため、同じ店舗IDを
複数の会社の取引が持つ (実測95店舗)。店舗IDだけでは求人の持ち主が決まらず、
夜間処理はそうした求人を一律で触らずにいた (担当者・要否・一次対応の条件が届かない)。

求人ごとの「応募時通知先メールアドレス」は、97%が会社・拠点ごとの別名
(rpo.medica+…) で、取引の管理用メールと同じ値。現場が求人登録時に既に入れている
値なので、**現場の入力を増やさずに**持ち主を決められる (ユーザー決定 2026-10-05・案A)。

判定の規則は deal_master.resolve_owner (通知先メールを主・店舗IDを補助)。
通知先メールそのものは HubSpot に保存しない。判定結果だけを書く。

## いつ動くか

HRハッカーの全求人CSVが手元にある実行 (job_daily の hr、1日3回) の
hr_watcher の後。夜間処理 (付け替え・担当者・要否・応募転記) はこの値を読む。

CLI: python resolve_listing_owner.py [--dry-run|--actual] [--hr-csv <path>]
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:  # CI は env 直渡し
    pass
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    from . import deal_master as DM
    from .hs_paging import list_all, search_all_by_id, post_retry
except ImportError:  # スクリプト直実行
    import deal_master as DM  # type: ignore
    from hs_paging import list_all, search_all_by_id, post_retry  # type: ignore

BASE = "https://api.hubapi.com"
PIPELINE = "21596025"
HR_DIR = "scratchpad/csv_fetched/hr"      # hr_watcher の保存先 (backfill_deal_shop_id_via_email と同じ)
COL_JOB = ("求人id", "求人ID")
COL_SHOP = ("店舗id", "店舗ID")
COL_MAIL_PREFIX = "応募時通知先メールアドレス"   # 括弧書きの注記は現場が変えうるので前方一致


def latest_hr_csv() -> str:
    c = sorted(glob.glob(str(_REPO / HR_DIR / "hr_offers_all_*.csv")))
    return c[-1] if c else ""


def _col(header: list, names) -> str:
    for n in names:
        if n in header:
            return n
    return ""


def load_csv_owners_input(path: str) -> dict:
    """{求人ID: (店舗ID, [通知先メール])}。HRハッカーのCSVは Shift-JIS。"""
    csv.field_size_limit(10**9)
    out = {}
    with open(path, encoding="cp932", errors="replace", newline="") as f:
        r = csv.DictReader(f)
        h = r.fieldnames or []
        cj, cs = _col(h, COL_JOB), _col(h, COL_SHOP)
        cm = next((x for x in h if x.startswith(COL_MAIL_PREFIX)), "")
        if not (cj and cs and cm):
            raise SystemExit(f"CSVの列が見つかりません (求人id/店舗id/通知先): {cj!r} {cs!r} {cm!r}")
        for row in r:
            jid = (row.get(cj) or "").strip()
            if not jid:
                continue
            mails = [m.strip().lower() for m in
                     (row.get(cm) or "").replace("、", ",").replace(";", ",").split(",") if "@" in m]
            out[jid] = ((row.get(cs) or "").strip(), mails)
    return out


def plan(listings: list, csv_input: dict, shop_index: dict, mail_index: dict,
         live: set = None) -> tuple:
    """純関数: [(listing_id, code, basis)] — 今と違うものだけ。CSVに無い求人は触らない。"""
    updates, stat = [], Counter()
    for o in listings:
        p = o.get("properties") or {}
        jid = str(p.get("id_hrhakkaa") or "").strip()
        if not jid or jid not in csv_input:
            stat["CSVに無い(触らない)"] += 1
            continue
        shop, mails = csv_input[jid]
        code, basis = DM.resolve_owner(shop, mails, shop_index, mail_index, live)
        stat[basis] += 1
        code = code or ""
        if code != str(p.get(DM.LISTING_OWNER) or "") or basis != str(p.get(DM.LISTING_OWNER_BASIS) or ""):
            updates.append((str(o["id"]), code, basis))
    return updates, stat


def apply(updates: list, sleep: float = 0.25) -> tuple:
    ok = fail = 0
    for i in range(0, len(updates), 100):
        chunk = updates[i:i + 100]
        try:
            post_retry(f"{BASE}/crm/v3/objects/0-420/batch/update",
                       {"inputs": [{"id": lid, "properties": {DM.LISTING_OWNER: code,
                                                              DM.LISTING_OWNER_BASIS: basis}}
                                   for lid, code, basis in chunk]})
            ok += len(chunk)
        except Exception as e:  # noqa: BLE001
            fail += len(chunk)
            print(f"    ★持ち主の書き込み失敗 {len(chunk)}件: {type(e).__name__}: {str(e)[:120]}", flush=True)
        time.sleep(sleep)
    return ok, fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    ap.add_argument("--hr-csv", default="")
    a = ap.parse_args(argv)
    path = a.hr_csv or latest_hr_csv()
    if not path:
        print("HRハッカーのCSVが見つかりません (hr_watcher が先に動く前提)。何もしません", flush=True)
        return 1
    csv_input = load_csv_owners_input(path)
    deals = {str(d["id"]): d.get("properties") or {} for d in search_all_by_id(
        "0-3", [DM.PROP_CODE, "hrhacker_shop_ids", "kanri_mail_address", "dealstage"],
        [{"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE}])}
    shop_index, mail_index = DM.owner_indexes(deals)
    listings = list_all("0-420", ["id_hrhakkaa", DM.LISTING_OWNER, DM.LISTING_OWNER_BASIS])
    updates, stat = plan(listings, csv_input, shop_index, mail_index, DM.live_codes(deals))
    print(f"=== 求人の持ち主の判定 (CSV {len(csv_input):,}求人 / dry_run={a.dry_run}) ===", flush=True)
    for k, n in stat.most_common():
        print(f"   {n:7,}  {k}", flush=True)
    print(f"書き換える求人 {len(updates):,}件", flush=True)
    if a.dry_run:
        return 0
    ok, fail = apply(updates)
    print(f"=== 結果 === 書き込み {ok:,} / 失敗 {fail:,}", flush=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
