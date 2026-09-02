# -*- coding: utf-8 -*-
"""作成時に求人メモが無かった応募へ、後からメモを届ける (2026-09-02)。

## なぜ要るか

応募カードへの暗黙知メモのコピー (notes.copy_listing_note_to_appointment) は
**応募の作成時に1回だけ**走る。求人側の転記メモは夜間 (deal_hygiene) にしか
更新されないので、次の順番で応募のメモが**永久に空のまま**になる:

    応募が来る (5分毎の同期で作成、この時点で求人にメモが無い)
      → その晩、取引のメモが求人へ転記される
      → しかし応募は二度と見に来ない

実測 (2026-08-30): 応募のメモ到達は61%。空の39%の相当数がこの取りこぼし。

## 何をするか

hs_createdate が FLOOR (2026-09-01) 以降の応募を毎晩見直し、
  - まだ②コピーNote (COPIED_NOTE_MARKER) を持たず、
  - 紐づく求人に署名付きメモ (転記 or 旧テンプレ) があるもの
へ copy_listing_note_to_appointment で複製する。

- **FLOOR より前の応募は対象にしない** (ユーザー決定 2026-09-02
  「応募はこれから完全になればよい」。過去の空きは救済しない)。
- 応募のメモは**コピー時点のスナップショット**。後から取引メモが変わっても
  追随しない (スナップショットか最新追随かは未決定のため、現行仕様を維持)。
- 冪等: コピー済みはスキップ。二度流しても増えない。
- 既定はドライラン。--actual で書き込む。
"""
from __future__ import annotations

import argparse
import sys
import time
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

try:
    from scripts.job_application_sync.hs_paging import (  # noqa: E402
        search_all_by_id, post_retry)
    from scripts.job_application_sync import notes as N  # noqa: E402
except ImportError:  # script直実行
    from hs_paging import search_all_by_id, post_retry  # type: ignore
    import notes as N  # type: ignore

BASE = "https://api.hubapi.com"
APPOINTMENT = "0-421"
LISTING = "0-420"
# これより前の応募は見ない (2026-09-02 ユーザー決定「これから完全になればよい」)。
# 窓を過去へ広げれば旧来の空き応募も埋まるが、意図して広げないこと。
FLOOR = "2026-09-01T00:00:00Z"


def recent_appointments() -> list:
    """FLOOR 以降に作られた応募のID一覧。"""
    return [str(r["id"]) for r in search_all_by_id(
        APPOINTMENT, ["hs_createdate"],
        [{"propertyName": "hs_createdate", "operator": "GTE", "value": FLOOR}])]


def listings_of(appt_ids: list) -> dict:
    """応募ID → 紐づく求人ID一覧。"""
    out: dict = {}
    for i in range(0, len(appt_ids), 100):
        r = post_retry(
            f"{BASE}/crm/v4/associations/{APPOINTMENT}/{LISTING}/batch/read",
            {"inputs": [{"id": a} for a in appt_ids[i:i + 100]]})
        for res in r.get("results", []):
            out[str(res["from"]["id"])] = [
                str(t["toObjectId"]) for t in res.get("to") or []]
        time.sleep(0.1)
    return out


def plan_backfill(appt_ids: list, l_of: dict, has_copy, listing_memo) -> dict:
    """純関数: 何をコピーすべきかを決める。

    has_copy(appt_id) -> bool     … 応募が②コピーNoteを既に持つか
    listing_memo(listing_id) -> bool … 求人に署名付きメモがあるか (キャッシュ推奨)
    戻り: {"copy": [(appt, listing)], "done": n, "no_listing": n, "no_memo": n}
    """
    copy, done, no_listing, no_memo = [], 0, 0, 0
    for a in appt_ids:
        lids = l_of.get(a) or []
        if not lids:
            no_listing += 1
            continue
        if has_copy(a):
            done += 1
            continue
        lid = next((l for l in lids if listing_memo(l)), None)
        if lid is None:
            # 求人にまだメモが無い = 取引側にもまだ無い。翌晩また見る
            no_memo += 1
            continue
        copy.append((a, lid))
    return {"copy": copy, "done": done,
            "no_listing": no_listing, "no_memo": no_memo}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="既定 (書き込まない)")
    a = ap.parse_args(argv)

    appts = recent_appointments()
    print(f"=== 応募メモの穴埋め (基準 {FLOOR[:10]} 以降 / "
          f"{'actual' if a.actual else 'dry-run'}) ===", flush=True)
    print(f"対象期間の応募 {len(appts):,}件", flush=True)
    l_of = listings_of(appts)

    memo_cache: dict = {}

    def listing_memo(lid: str) -> bool:
        if lid not in memo_cache:
            memo_cache[lid] = bool(N.get_listing_template_note_body(lid))
        return memo_cache[lid]

    plan = plan_backfill(appts, l_of, N.has_copied_note, listing_memo)
    print(f"  コピー済み {plan['done']} / 求人未紐付け {plan['no_listing']} / "
          f"求人にメモ無し(翌晩再訪) {plan['no_memo']} / "
          f"コピーする {len(plan['copy'])}", flush=True)

    copied, failed = 0, 0
    for appt, lid in plan["copy"]:
        if not a.actual:
            print(f"  [dry] 応募 {appt} ← 求人 {lid}", flush=True)
            continue
        # copy_listing_note_to_appointment 内でも has_copied_note を再確認する
        nid = N.copy_listing_note_to_appointment(lid, appt, dry_run=False)
        if nid:
            copied += 1
        else:
            failed += 1
            print(f"  [warn] コピーできず 応募={appt} 求人={lid}", flush=True)
        time.sleep(0.2)
    if a.actual:
        print(f"=== 結果 === コピー {copied} / 失敗 {failed}", flush=True)
        # 取りこぼしを黙って成功にしない: 失敗があれば非0で返し CI の rc に出す
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
