# -*- coding: utf-8 -*-
"""媒体の一覧から消えた「掲載中」求人を公開終了にする共通ロジックと、一回限りの是正 CLI (2026-10-15).

背景:
  HubSpot の求人 (0-420) が、媒体で終わったあとも「掲載中」のまま残っていた。
  - HRハッカー: 取込は CSV にある求人しか更新しないので、CSV から消えた求人は「公開」のまま。
  - AirWork: 「エクスポートから消えたら公開終了」は口座を取得した回にしか動かず、
    取得は不定期なので、取得されない口座の消えた求人が残る。

ルール (定数はこのファイルの先頭):
  - HRハッカー: CSV が完全に取れた取込のあと、店舗ごとに「公開中」で、最終CSV検出日が
    HRH_VANISH_DAYS(=2)日以上前、つまり中1日あけて2日以上見つからない求人を公開終了にする (kyuujin_status=公開終了 /
    baitai_genjoukyou_hrhakkaa=非公開)。再び CSV に載れば通常の更新で 公開中/公開 に戻る。
  - AirWork: 取得に成功した口座 (airwork_import が完全なエクスポートを処理した口座) の
    「公開中」で、そのエクスポートに無い求人だけを公開終了にする (airwork_import.run)。
    取得していない口座は触らない。
  - 安全弁: 1 つの店舗/口座で「公開中」の MAX_END_RATIO (20%) を超えて終了にする計画になったら、
    その店舗/口座は見送って件数だけ報告する (ダウンロードが壊れた疑い)。

CLI (既定は dry-run。件数だけ出す。名前・IDは出さない):
  python scripts/job_application_sync/end_vanished_listings.py            # dry-run
  python scripts/job_application_sync/end_vanished_listings.py --apply    # 書き込み
  --only hrh|aw   片方だけ
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:  # パッケージ実行 / スクリプト直実行の両対応
    from . import listing_stage as _stage
    from . import private_log as plog
except ImportError:  # pragma: no cover
    import listing_stage as _stage  # type: ignore
    import private_log as plog  # type: ignore

BASE = "https://api.hubapi.com"

# ---- 定数 -------------------------------------------------------------------
HRH_VANISH_DAYS = 2          # 中1日あける: 最終CSV検出日が取込日の2日以上前(=完全な取込で2日続けて載らなかった)なら終了。取込が1日抜けた/検出日が更新されなかった場合の誤終了を避ける余裕
MAX_END_RATIO = 0.20         # 1 店舗/口座の「公開中」のうち、1 回に終了にしてよい上限の割合
MAX_END_FLOOR = 2            # 終了予定がこの件数以下なら割合の上限を適用しない (小さな店舗が永久に止まらないように)
AW_RECENT_FETCH_DAYS = 7     # 是正 CLI: 口座を「最近取得した」とみなす日数
BATCH_SIZE = 100

PROP_KYUUJIN_STATUS = "kyuujin_status"
PROP_HRH_MEDIA_STATUS = "baitai_genjoukyou_hrhakkaa"
PROP_AW_MEDIA_STATUS = "baitai_genjoukyou_airwork"
PROP_SEEN = "saishuu_csv_kenshutsu_bi"
PROP_FLAG = "konkai_csv_kenshutsu_flag"
PROP_FILENAME = "douki_moto_filename"
PROP_SYNCED = "doukisaishuujikoku"
PROP_HRH_ID = "id_hrhakkaa"
PROP_HRH_SHOP = "id_shop_hrhakkaa"
PROP_AW_ID = "id_airwork"
PROP_AW_LOGIN = "airwork_account_login_id"
CLEANUP_MARK = "end_vanished_listings"   # 同期元ファイル名の欄に書く目印 (新しいプロパティは作らない)

AW_LEDGER_PATH = REPO / "data" / "job_application_sync" / "aw_fetch_ledger.json"


# ---- 純粋関数 ---------------------------------------------------------------
def parse_date(v) -> Optional[date]:
    """HubSpot の date 値 (YYYY-MM-DD / ISO 日時 / ミリ秒) を date にする。読めなければ None。"""
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        if s.isdigit() and len(s) >= 11:
            return datetime.utcfromtimestamp(int(s) / 1000).date()
        return date.fromisoformat(s[:10])
    except (ValueError, OverflowError, OSError):
        return None


def days_unseen(props: dict, today: date) -> Optional[int]:
    d = parse_date((props or {}).get(PROP_SEEN))
    return None if d is None else (today - d).days


def over_cap(n_end: int, n_published: int) -> bool:
    """終了予定が公開中の MAX_END_RATIO を超えるか (MAX_END_FLOOR 件以下は対象外)。"""
    if n_end <= MAX_END_FLOOR:
        return False
    return n_end > n_published * MAX_END_RATIO


def apply_cap(groups: dict) -> tuple[list, dict]:
    """groups: {キー: {"published": 公開中の件数, "ends": [候補,...]}}。

    Returns: (終了にする候補の一覧, 見送った {キー: (終了予定, 公開中)})
    """
    ends: list = []
    skipped: dict = {}
    for key, g in groups.items():
        e = g["ends"]
        if not e:
            continue
        if over_cap(len(e), g["published"]):
            skipped[key] = (len(e), g["published"])
        else:
            ends.extend(e)
    return ends, skipped


def build_hrh_end_props(existing_props: dict, now_iso: str, source: str) -> dict:
    p = {
        PROP_KYUUJIN_STATUS: "公開終了",
        PROP_HRH_MEDIA_STATUS: "非公開",
        PROP_FLAG: "false",
        PROP_SYNCED: now_iso,
        PROP_FILENAME: source,
    }
    p.update(_stage.stage_props("公開終了", (existing_props or {}).get("hs_pipeline_stage")))
    return p


def build_aw_end_props(existing_props: dict, now_iso: str, source: str) -> dict:
    """airwork_import.build_end_missing_props と同じ中身 (媒体原ステータスは観測していないので書かない)。"""
    p = {
        PROP_KYUUJIN_STATUS: "公開終了",
        PROP_FLAG: "false",
        PROP_SYNCED: now_iso,
        PROP_FILENAME: source,
    }
    p.update(_stage.stage_props("公開終了", (existing_props or {}).get("hs_pipeline_stage")))
    return p


def plan_hrh_ends(listings: Iterable[dict], today: date, csv_ids: Iterable = (),
                  vanish_days: int = HRH_VANISH_DAYS) -> tuple[list, dict, int]:
    """HRH の公開中 listings ([{id, properties}]) から終了にする候補を作る。

    csv_ids: 今回の CSV に載っていた求人ID (載っていれば消えていない)。
    Returns: (終了にする [{id, properties(既存)}], 見送った店舗 {店舗番号: (予定, 公開中)}, 日付なしの件数)
    店舗ごとの一括キーは平文の ID を持たないよう、呼び出し側で件数だけ出す。
    """
    in_csv = {str(x).strip() for x in csv_ids if str(x).strip()}
    groups: dict = defaultdict(lambda: {"published": 0, "ends": []})
    no_date = 0
    for l in listings:
        p = l.get("properties") or {}
        shop = (p.get(PROP_HRH_SHOP) or "").strip() or "(none)"
        g = groups[shop]
        g["published"] += 1
        if str(p.get(PROP_HRH_ID) or "").strip() in in_csv:
            continue
        d = days_unseen(p, today)
        if d is None:
            no_date += 1
            continue
        if d >= vanish_days:
            g["ends"].append(l)
    ends, skipped = apply_cap(groups)
    return ends, skipped, no_date


def csv_is_complete(total_rows: int, valid_rows: int, skipped_no_id: int,
                    limit: Optional[int]) -> bool:
    """HRH の CSV が最後まで取れたと言えるか。--limit で切った/0件/ID欠けがあれば False。"""
    if os.environ.get("JAS_HRH_END_VANISHED", "1") == "0":
        return False
    return not limit and total_rows > 0 and valid_rows > 0 and skipped_no_id == 0


# ---- HubSpot 入出力 ---------------------------------------------------------
def fetch_hrh_published() -> list:
    from scripts.job_application_sync import hs_paging
    return hs_paging.search_all_by_id(
        "0-420",
        [PROP_HRH_ID, PROP_HRH_SHOP, PROP_SEEN, PROP_KYUUJIN_STATUS, "hs_pipeline_stage"],
        [{"propertyName": PROP_KYUUJIN_STATUS, "operator": "EQ", "value": "公開中"},
         {"propertyName": PROP_HRH_ID, "operator": "HAS_PROPERTY"}])


def fetch_aw_published() -> list:
    from scripts.job_application_sync import hs_paging
    return hs_paging.search_all_by_id(
        "0-420",
        [PROP_AW_ID, PROP_AW_LOGIN, PROP_SEEN, PROP_KYUUJIN_STATUS, "hs_pipeline_stage"],
        [{"propertyName": PROP_KYUUJIN_STATUS, "operator": "EQ", "value": "公開中"},
         {"propertyName": PROP_AW_ID, "operator": "HAS_PROPERTY"}])


def write_updates(updates: list) -> tuple[int, int]:
    """batch/update を 100 件ずつ。429 などは post_retry が待って再試行する。(ok, ng)"""
    from scripts.job_application_sync import hs_paging
    ok = ng = 0
    for i in range(0, len(updates), BATCH_SIZE):
        chunk = updates[i:i + BATCH_SIZE]
        try:
            j = hs_paging.post_retry(f"{BASE}/crm/v3/objects/0-420/batch/update",
                                     {"inputs": chunk})
        except Exception as e:  # noqa: BLE001
            ng += len(chunk)
            print(f"  WARN: 書き込み失敗 ({type(e).__name__}) {len(chunk)} 件")
            continue
        ne = j.get("numErrors", 0) or 0
        ok += len(j.get("results", []))
        ng += ne
        time.sleep(0.15)
    return ok, ng


def run_hrh_end_vanished(csv_ids: Iterable, dry_run: bool, now_iso: str, source: str,
                         today: Optional[date] = None,
                         fetch: Optional[Callable[[], list]] = None,
                         write: Optional[Callable[[list], tuple]] = None) -> dict:
    """CSV 取込のあとに呼ぶ。件数のサマリを返す (名前・IDは返さない)。"""
    today = today or date.today()
    listings = (fetch or fetch_hrh_published)()
    ends, skipped, no_date = plan_hrh_ends(listings, today, csv_ids)
    summary = {"hrh_published": len(listings), "hrh_ends_planned": len(ends),
               "hrh_skipped_shops": len(skipped),
               "hrh_skipped_planned": sum(s[0] for s in skipped.values()),
               "hrh_no_date": no_date}
    if not dry_run and ends:
        updates = [{"id": l["id"], "properties": build_hrh_end_props(l["properties"], now_iso, source)}
                   for l in ends]
        ok, ng = (write or write_updates)(updates)
        summary.update({"hrh_ends_ok": ok, "hrh_ends_ng": ng})
    return summary


# ---- AirWork: 取得した口座の記録 --------------------------------------------
def record_aw_fetch(login_id: str, today: Optional[date] = None,
                    path: Optional[Path] = None) -> None:
    """完全なエクスポートを処理した口座を記録する (キーはログインIDのハッシュ先頭8桁)。"""
    if not login_id:
        return
    path = path or AW_LEDGER_PATH
    today = today or date.today()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[plog.file_tag(login_id)] = today.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_aw_ledger(path: Optional[Path] = None) -> dict:
    path = path or AW_LEDGER_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: d for k, v in raw.items() if (d := parse_date(v))}


def plan_aw_cleanup(listings: Iterable[dict], today: date, ledger: Optional[dict] = None,
                    recent_days: int = AW_RECENT_FETCH_DAYS) -> dict:
    """AW の公開中 listings から是正計画を作る。

    口座ごとの「最後に取得した日」= 台帳 (record_aw_fetch) があればそれ、なければ口座内の
    最終CSV検出日の最大値 (取得すれば載っていた求人はその日付になるため)。
    最後に取得した日が recent_days 以内の口座だけ対象。その日付より前に検出された求人 = その
    取得に載っていなかった求人 → 終了候補。口座が最近取得されていない / 日付が無い求人は「要確認」。
    Returns: {"ends": [...], "skipped": {口座タグ: (予定, 公開中)}, "review": 件数, "accounts": 口座数}
    """
    ledger = ledger or {}
    by_acct: dict = defaultdict(list)
    review = 0
    for l in listings:
        login = ((l.get("properties") or {}).get(PROP_AW_LOGIN) or "").strip()
        if not login:
            review += 1      # 口座が分からない求人は触らない
            continue
        by_acct[login].append(l)
    groups: dict = {}
    for login, ls in by_acct.items():
        seen = [d for d in (parse_date(x["properties"].get(PROP_SEEN)) for x in ls) if d]
        last = ledger.get(plog.file_tag(login)) or (max(seen) if seen else None)
        g = {"published": len(ls), "ends": []}
        groups[plog.file_tag(login)] = g
        if last is None or (today - last).days > recent_days:
            review += len(ls)
            continue
        for x in ls:
            d = parse_date(x["properties"].get(PROP_SEEN))
            if d is None:
                review += 1
            elif d < last:
                g["ends"].append(x)
    ends, skipped = apply_cap(groups)
    return {"ends": ends, "skipped": skipped, "review": review, "accounts": len(by_acct)}


# ---- CLI --------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="消えた掲載中求人を公開終了にする (一回限りの是正)")
    ap.add_argument("--apply", action="store_true", help="書き込む (既定は dry-run)")
    ap.add_argument("--only", choices=["hrh", "aw"], default=None)
    args = ap.parse_args(argv)
    if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        try:
            from dotenv import load_dotenv
            load_dotenv(REPO / ".env")
        except ImportError:
            pass
    if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        print("HUBSPOT_ACCESS_TOKEN が未設定です")
        return 2
    today = date.today()
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"=== end_vanished_listings ({'APPLY' if args.apply else 'dry-run'}) ===")
    updates: list = []

    if args.only in (None, "hrh"):
        hl = fetch_hrh_published()
        # 今日の取込が済んでいないと検出日が1日ずれて誤判定しうるため、今日検出された求人が
        # 1件も無ければ HRH は見送る (取込のあとに実行する)
        if not any(parse_date(l["properties"].get(PROP_SEEN)) == today for l in hl):
            print("[HRH] 今日検出された求人が無い(今日の取込が未実行)ため見送り")
            hl = []
        ends, skipped, no_date = plan_hrh_ends(hl, today)
        print(f"[HRH] 公開中 {len(hl)} 件 / 終了予定 {len(ends)} 件 / "
              f"上限超えで見送る店舗 {len(skipped)} (予定 {sum(s[0] for s in skipped.values())} 件) / "
              f"日付なし {no_date} 件 (HRH_VANISH_DAYS={HRH_VANISH_DAYS})")
        updates += [{"id": l["id"], "properties": build_hrh_end_props(l["properties"], now_iso, CLEANUP_MARK)}
                    for l in ends]

    if args.only in (None, "aw"):
        al = fetch_aw_published()
        plan = plan_aw_cleanup(al, today, load_aw_ledger())
        print(f"[AW] 公開中 {len(al)} 件 / 口座 {plan['accounts']} / 終了予定 {len(plan['ends'])} 件 / "
              f"上限超えで見送る口座 {len(plan['skipped'])} (予定 {sum(s[0] for s in plan['skipped'].values())} 件) / "
              f"要確認 {plan['review']} 件 (変更しない)")
        updates += [{"id": l["id"], "properties": build_aw_end_props(l["properties"], now_iso, CLEANUP_MARK)}
                    for l in plan["ends"]]

    print(f"合計 書き込み予定: {len(updates)} 件")
    if args.apply and updates:
        ok, ng = write_updates(updates)
        print(f"書き込み: OK={ok} NG={ng}")
        return 1 if ng else 0
    if not args.apply:
        print("dry-run のため書き込みなし。--apply で実行")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
