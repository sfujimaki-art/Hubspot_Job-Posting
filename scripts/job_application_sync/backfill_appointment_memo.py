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

## 2026-09-17 の是正 (本番が2晩タイムアウトした)

当初は FLOOR を固定日にし、応募1件ごと・求人1件ごとにAPIを叩いていた。
そのため **対象が毎日増え続け、所要時間も一緒に伸びた**:

    2026-09-10  応募 1,209件  … 完走 (deal_hygiene 全体で28分)
    2026-09-15  応募 1,940件  … ★60分のtimeoutで打ち切り
    2026-09-16  応募 2,017件  … ★同上

打ち切られると後続の「シートURL乖離検知」「健全性チェック」も走らないので、
**2晩ぶんのSlack通知が出なかった**。実装時(112件)には見えなかった。

2つ直す:
  1. 判定をバッチ読みにする。応募→ノートの関連と本文をまとめて取り、
     マーカーの有無はメモリ上で見る (1件1GET → 100件1POST)
  2. 窓を「FLOOR以降 かつ 直近 WINDOW_DAYS 日」にする。固定窓だと
     対象が無限に伸びる。窓の外は件数をログに出して黙って捨てない
"""
from __future__ import annotations

import argparse
import sys
import time
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
# ★固定窓だと対象が毎日増え、2026-09-15/16 に60分のtimeoutで打ち切られた。
#   メモは遅くとも翌晩には求人へ届くので、30日あれば取りこぼさない。
WINDOW_DAYS = 30


def window_start(now=None) -> str:
    """実際に見る下限。FLOOR と「直近 WINDOW_DAYS 日」の**新しい方**。"""
    now = now or datetime.now(timezone.utc)
    rolling = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return max(FLOOR, rolling)


def recent_appointments(since: str = "") -> list:
    """since (既定は window_start) 以降に作られた応募のID一覧。"""
    since = since or window_start()
    return [str(r["id"]) for r in search_all_by_id(
        APPOINTMENT, ["hs_createdate"],
        [{"propertyName": "hs_createdate", "operator": "GTE", "value": since}])]


def _batch_assoc(obj_type: str, ids: list) -> dict:
    """{レコードID: [ノートID]}。1件1GETではなく100件1POSTで取る。"""
    out: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v4/associations/{obj_type}/notes/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in r.get("results", []):
            out[str(res["from"]["id"])] = [str(t["toObjectId"])
                                           for t in res.get("to") or []]
        time.sleep(0.2)
    return out


def _note_bodies(note_ids: list) -> dict:
    """{ノートID: 本文}。まとめて読む。"""
    out: dict = {}
    ids = sorted(set(note_ids))
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v3/objects/notes/batch/read",
                       {"inputs": [{"id": n} for n in ids[i:i + 100]],
                        "properties": ["hs_note_body"]})
        for x in r.get("results", []):
            out[str(x["id"])] = (x["properties"].get("hs_note_body") or "")
        time.sleep(0.2)
    return out


def copied_appointments(appt_ids: list) -> set:
    """②コピーNote (マーカー付き) を既に持つ応募の集合。

    ★notes.has_copied_note と同じ判定をバッチで行う。あちらは応募1件につき
      GET 1回 + ノート本文ぶんのGETで、2,000件だと1万回近くになる。
    """
    n_of = _batch_assoc(APPOINTMENT, appt_ids)
    bodies = _note_bodies([n for v in n_of.values() for n in v])
    return {a for a, ns in n_of.items()
            if any(N.COPIED_NOTE_MARKER in bodies.get(n, "") for n in ns)}


def listings_with_memo(listing_ids: list) -> set:
    """署名付きメモ (転記 or 旧テンプレ) を持つ求人の集合。

    実際にどちらを貼るかは copy_listing_note_to_appointment が決める。
    ここは「貼れる材料があるか」だけを見る。
    """
    n_of = _batch_assoc(LISTING, listing_ids)
    bodies = _note_bodies([n for v in n_of.values() for n in v])
    return {l for l, ns in n_of.items()
            if any(N.TRANSFER_SIGNATURE in bodies.get(n, "")
                   or N.TEMPLATE_SIGNATURE in bodies.get(n, "") for n in ns)}


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

    since = window_start()
    appts = recent_appointments(since)
    print(f"=== 応募メモの穴埋め ({since[:10]} 以降 / "
          f"{'actual' if a.actual else 'dry-run'}) ===", flush=True)
    print(f"対象期間の応募 {len(appts):,}件 "
          f"(窓: FLOOR {FLOOR[:10]} と直近{WINDOW_DAYS}日の新しい方。"
          f"これより古い応募は見ない)", flush=True)
    l_of = listings_of(appts)

    # ★判定はバッチで取る。1件ずつ叩くと2,000件で60分のtimeoutに当たる
    #   (2026-09-15/16 に本番が2晩とも打ち切られた)。
    done_set = copied_appointments(appts)
    memo_set = listings_with_memo(
        sorted({l for v in l_of.values() for l in v}))
    plan = plan_backfill(appts, l_of,
                         lambda x: x in done_set, lambda x: x in memo_set)
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
