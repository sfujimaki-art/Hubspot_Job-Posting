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
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

try:  # パッケージ実行/スクリプト直実行の両対応 (CIは直実行)
    from scripts.job_application_sync import private_log as plog
except ImportError:  # pragma: no cover
    import private_log as plog  # type: ignore

# 標準出力を差し替えない (テストから import すると出力処理が壊れる)。他の処理と同じ作法。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass
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


def load_pipeline_deals() -> dict:
    """納品管理PLの取引 {id: props}。10,000件上限の無い取得で全件。"""
    try:
        from scripts.job_application_sync.hs_paging import search_all_by_id
    except ImportError:  # スクリプト直実行
        from hs_paging import search_all_by_id  # type: ignore
    return {str(d["id"]): d.get("properties") or {} for d in search_all_by_id(
        "0-3", ["kanri_mail_address", PROP, "code_of_customer", "dealstage",
                "dealname", "contract_start_date", "createdate"],
        [{"propertyName": "pipeline", "operator": "EQ", "value": PIPE}])}


def plan_additions(m2s: dict, deals: dict) -> tuple:
    """純関数: {取引ID: 足す店舗ID集合} と内訳。

    ★2026-10-05 是正 (ユーザー決定)。旧実装は管理用メールが一致した取引の
      **最初の1件**に店舗IDを足していた。同じ会社の別拠点が同じ別名を使うと
      別の拠点の取引や終わった取引に店舗IDが入り、同じ店舗IDを複数の会社
      (取引先コード) の取引が持つ状態を毎日作っていた (実測95店舗)。
      人が拠点ごとに振り分けても、次の取り込みで足し戻されていた。

    足すのは次の全部を満たすときだけ:
      1. メールが**1つの取引先コードだけ**を指す (同じ会社の別拠点が同じ別名なら足さない)
      2. 足す先はそのコードの**生きている今の契約** (終わった取引には足さない)
      3. その店舗IDが**別の取引先コードの取引に入っていない** (人の振り分けを崩さない)
    外すことはしない。
    """
    try:
        from scripts.job_application_sync import deal_master as DM
    except ImportError:  # スクリプト直実行
        import deal_master as DM  # type: ignore
    shop_index, mail_index = DM.owner_indexes(deals)
    by_code = DM.group_by_code(deals)
    add, stat = defaultdict(set), Counter()
    for mail, shops in m2s.items():
        codes = mail_index.get(mail)
        if not codes:
            stat["メールが取引に無い"] += 1
            continue
        if len(codes) > 1:
            stat["メールが複数の取引先コードを指す(自動で足さない)"] += 1
            continue
        code = next(iter(codes))
        target = DM.latest_live(by_code.get(code, []), deals)
        if not target or not DM.is_live(deals.get(target)):
            stat["そのコードに生きている取引が無い"] += 1
            continue
        for sid in shops:
            others = set(shop_index.get(sid, ())) - {code}
            if others:
                stat["店舗IDが別の取引先コードに入っている(足さない)"] += 1
                continue
            if sid not in split_ids(deals[target].get(PROP)):
                add[target].add(sid)
        stat["メールが1社を指す"] += 1
    return add, stat


def main(dry_run, limit, hr_csv):
    print(f"=== backfill_deal_shop_id_via_email (dry_run={dry_run}) ===")
    m2s = load_hr_mail_to_shops(Path(hr_csv))
    deals = load_pipeline_deals()
    print(f"HR 連絡先メール: {len(m2s)} / 納品管理Deal: {len(deals)}")
    deal_add, stat = plan_additions(m2s, deals)
    for k, n in stat.most_common():
        print(f"  {n:6}  {k}")

    plan, updated, errors = [], 0, 0
    items = sorted(deal_add.items())
    if limit:
        items = items[:limit]
    for did, add in items:
        existing = split_ids(deals[did].get(PROP))
        merged = existing | add
        plan.append({"deal_id": did, "added": sorted(add),
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

    print("\n--- 結果 ---")
    print(f"  {'書込予定' if dry_run else '書込実行'}(新規店舗ID追加): {len(plan)} 件")
    if not dry_run:
        print(f"  更新OK: {updated} / NG: {errors}")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    log = LOG_DIR / f"backfill_shop_via_email_{'actual' if not dry_run else 'dry'}_{ts}.json"
    log.write_text(json.dumps({"plan": plan[:800]}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"  log: {log}")
    # 取引ID・店舗IDは公開ログに出さない (2026-10-09)。全件は非公開ログへ
    for p in plan[:6]:
        print(f"    Deal {plog.mask_id(p['deal_id'])} += {len(p['added'])}件 "
              f"(既存{p['existing']}->{p['merged']})")
    for p in plan:
        plog.detail("deal_shop_id_added", deal_id=p["deal_id"], added=p["added"],
                    existing=p["existing"], merged=p["merged"],
                    error=p.get("error", ""), dry_run=dry_run)
    if errors:
        raise RuntimeError(f"店舗IDの書き込みに {errors} 件失敗")


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
        plog.public(f"[slack未設定] {len(message)}字 (本文は非公開ログ)")
        plog.detail("slack_unsent", reason="webhook未設定", message=message)
        return False
    try:
        return requests.post(url, json={"text": message},
                             timeout=15).status_code == 200
    except requests.RequestException as e:  # noqa: BLE001
        plog.public(f"[slack送信失敗] {type(e).__name__} (例外文はURLを含みうるため非公開ログ)")
        plog.detail("slack_unsent", reason=type(e).__name__, message=message)
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
    finally:
        plog.flush()
