"""HR CSVの連絡先メール → Deal.kanri_mail_address 経由で 店舗ID を補完.

WBS 1.11.9 派生 (2026-07-03)。
背景: Deal店舗IDは 2026-06-03 由来で古く、HR公開求人→Deal紐づけ87%。
      LISTING↔Deal Associationは納品管理外Dealを指すため補完に使えない (実測)。
      正しい橋渡し = HR求人の連絡先メール(col75 rpo.medica+xxx) → Deal.kanri_mail_address
      (今回459 Dealに投入したキー) で店舗IDをDealへ和集合マージする。

方式:
  1. HR CSV: 連絡先メール(col75) → 店舗id(col1) 集合 を作る
  2. 納品管理Deal: kanri_mail_address → (deal_id, 既存hrhacker_shop_ids) 索引
     (semicolon連結の複数メールは各々index)
  3. HRメールがDealのkanri_mailに一致 → 店舗IDを和集合マージ (既存保護)
  4. dry-run / actual

CLI: --dry-run(既定) / --actual / --limit N / --hr-csv <path>
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LOG_DIR = HERE / "logs"; LOG_DIR.mkdir(exist_ok=True)
load_dotenv(REPO / ".env")
H = {"Authorization": f"Bearer {os.environ.get('HUBSPOT_ACCESS_TOKEN','')}",
     "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
PIPE = "21596025"
PROP = "hrhacker_shop_ids"
_HR_DIR = "scratchpad/csv_fetched/hr"


def latest_hr_csv() -> str:
    """hr_watcher が落とした**最新の**求人CSVを選ぶ。

    ★固定パスにしていたため、日次で新しいCSVが落ちていても2026-07-03の
      スナップショットを見続けていた (2026-08-10 実測: 未紐付け求人の店舗ID
      31種のうち、7/27のCSVに載っていたのは8種だけ。残りはCSVの上限
      1521550より新しい店舗で、古いCSVでは永久に補完できない)。
    """
    import glob as _g
    c = sorted(_g.glob(f"{_HR_DIR}/hr_offers_all_*.csv"))
    return c[-1] if c else ""


DEFAULT_HR = latest_hr_csv()


def split_ids(raw):
    return {s.strip() for s in (raw or "").replace(",", ";").split(";") if s.strip()}


def load_hr_mail_to_shops(hr_csv: Path) -> dict[str, set]:
    rows = list(csv.reader(io.StringIO(
        hr_csv.read_bytes().decode("shift_jis", errors="replace"))))
    hdr = rows[0]
    ci_shop, ci_mail = hdr.index("店舗id"), hdr.index("連絡先メールアドレス")
    m2s = defaultdict(set)
    for r in rows[1:]:
        if len(r) <= max(ci_shop, ci_mail):
            continue
        mail = r[ci_mail].strip().lower()
        sid = r[ci_shop].strip()
        if mail and sid:
            m2s[mail].add(sid)
    return m2s


def load_deal_mail_index() -> dict[str, dict]:
    """kanri_mail(小文字) → {deal_id, shops(既存)} (納品管理Deal)."""
    idx = {}
    after = None
    while True:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "kanri_mail_address", "operator": "HAS_PROPERTY"},
            {"propertyName": "pipeline", "operator": "EQ", "value": PIPE}]}],
            "properties": ["kanri_mail_address", PROP], "limit": 100}
        if after:
            body["after"] = after
        j = requests.post(f"{BASE}/crm/v3/objects/0-3/search",
                          headers=H, json=body, timeout=40).json()
        for o in j.get("results", []):
            did = o["id"]
            shops = split_ids(o["properties"].get(PROP, ""))
            for m in (o["properties"].get("kanri_mail_address") or "").replace(",", ";").split(";"):
                mm = m.strip().lower()
                if mm and mm not in idx:
                    idx[mm] = {"deal_id": did, "shops": shops}
        after = j.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return idx


def main(dry_run, limit, hr_csv):
    print(f"=== backfill_deal_shop_id_via_email (dry_run={dry_run}) ===")
    m2s = load_hr_mail_to_shops(Path(hr_csv))
    didx = load_deal_mail_index()
    print(f"HR 連絡先メール: {len(m2s)} / 納品管理Deal(kanri_mail): {len(didx)}")

    # メール一致で Deal に店舗IDマージ (同一Dealに複数メールが来る場合は集約)
    deal_add = defaultdict(set)     # deal_id -> 追加候補店舗ID
    deal_existing = {}
    matched_mail = 0
    for mail, shops in m2s.items():
        d = didx.get(mail)
        if not d:
            continue
        matched_mail += 1
        deal_add[d["deal_id"]] |= shops
        deal_existing[d["deal_id"]] = d["shops"]
    print(f"HRメール->Deal一致: {matched_mail}")

    plan, skip_same, updated, errors = [], 0, 0, 0
    items = list(deal_add.items())
    if limit:
        items = items[:limit]
    for did, add in items:
        existing = deal_existing.get(did, set())
        merged = existing | add
        if merged == existing:
            skip_same += 1
            continue
        plan.append({"deal_id": did, "added": sorted(add - existing),
                     "existing": len(existing), "merged": len(merged)})
        if not dry_run:
            resp = requests.patch(f"{BASE}/crm/v3/objects/0-3/{did}", headers=H,
                                  json={"properties": {PROP: ";".join(sorted(merged))}},
                                  timeout=30)
            if resp.status_code in (200, 201):
                updated += 1
            else:
                errors += 1
                plan[-1]["error"] = resp.text[:120]
            time.sleep(0.06)

    print(f"\n--- 結果 ---")
    print(f"  {'書込予定' if dry_run else '書込実行'}(新規店舗ID追加): {len(plan)} 件")
    print(f"  既存に全て含む(skip): {skip_same} 件")
    if not dry_run:
        print(f"  更新OK: {updated} / NG: {errors}")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    log = LOG_DIR / f"backfill_shop_via_email_{'actual' if not dry_run else 'dry'}_{ts}.json"
    log.write_text(json.dumps({"plan": plan[:800]}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"  log: {log}")
    for p in plan[:6]:
        print(f"    Deal {p['deal_id']} += {p['added'][:5]} (既存{p['existing']}->{p['merged']})")


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--hr-csv", default=DEFAULT_HR)
    return ap.parse_args(argv)


def _slack(message: str) -> bool:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:300]}", flush=True)
        return False
    try:
        return requests.post(url, json={"text": message},
                             timeout=15).status_code == 200
    except requests.RequestException as e:  # noqa: BLE001
        print(f"[slack送信失敗] {e}", flush=True)
        return False


if __name__ == "__main__":
    a = parse_args()
    # ★失敗を握り潰さない (2026-08-12 是正)。
    #   CI側は `|| echo "::warning::..."` で継続する作りにしてあるが、
    #   ::warning:: はSlackに飛ばないので**誰も気づかない**。
    #   この補完が止まると取引に店舗IDが入らず、求人が取引に紐付かなくなり、
    #   応募の一次対応の要否・担当者が空のまま積み上がる。必ず人へ届ける。
    try:
        if not a.hr_csv:
            raise RuntimeError(
                "HR求人CSVが見つかりません "
                "(scratchpad/csv_fetched/hr/hr_offers_all_*.csv)。"
                "hr_watcher が先に走っている必要があります")
        main(dry_run=not a.actual, limit=a.limit, hr_csv=a.hr_csv)
    except Exception as e:  # noqa: BLE001
        _slack(
            "⚠️ 取引の店舗ID補完が失敗しました\n"
            f"　理由: {type(e).__name__}: {str(e)[:200]}\n"
            "　▶ 放置すると: 新しい店舗の取引に鍵が入らず、求人が取引に紐付かない。"
            "その求人への応募は一次対応の要否・担当者が空のまま入る\n"
            "　▶ 確認: Job Daily の hr フェーズのログ "
            "(backfill_deal_shop_id_via_email)")
        raise
