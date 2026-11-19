"""HRハッカーの応募一覧と HubSpot の応募 (0-421) を突き合わせ、取りこぼしを登録する (2026-10-26).

なぜ要るか:
  通常の応募同期は、通知シート (シート1) の行をきっかけに HR の応募を取り込む。
  2026-10-12〜10-24 の HR 応募のうち 4 件 (どれも 22:30〜23:40) が HubSpot に入っていなかった。
  通知の行が来なかった回と、行は来たのに最初から台帳で処理済み扱いになった回があった。
  通知に頼らず、HR の応募一覧そのものを正として毎時突き合わせる。
  (同じ期間に HubSpot に応募者idが無かった残り 26 件は、同じ人・同じ求人・同じ日の
   正しい重複。HRハッカーは 2 回目の応募に別の応募者idを振る。)

流れ:
  1. HR の応募一覧 CSV を直近 N 日 (既定 3 日) 取る (hr_applicant_fetcher)。
  2. 応募者id (hr_oubosha_id) が HubSpot にあれば取り込み済み。
  3. 無いものは、取り込みと同じ重複判定 (find_existing_appointment:
     媒体 + 媒体求人ID + 応募日 + メールか電話) で既存があれば正しい重複として除く。
  4. 応募から MIN_AGE_MIN 分たっていないものは、通常の同期の途中かもしれないので数えない。
  5. 残りが本当の取りこぼし。--actual --import-missing のとき、同じログインのまま詳細ページも取り、
     通常の取り込みと同じ run_import で登録する (通知シートは経由しない。
     run_import は作る前にもう一度重複を確かめる)。詳細ページの項目も同じように書く。
  6. 登録した件数を Slack に 1 行。登録したあとも残るものは状態ファイルに出す
     (ワークフローが Issue を立て、立てたときだけ Slack に知らせる)。

公開リポなので、ログ・Slack・状態ファイルに出すのは 件数・日付・応募経路の番号・店舗ID と、
応募者idの hash の先頭 8 文字まで。氏名・電話・メール・応募者id そのものは出さない。
CSV は一時フォルダに置き、終わったら消す。

CLI:
    python scripts/job_application_sync/hr_reconcile.py [--days 3] [--dry-run | --actual]
        [--import-missing] [--slack] [--status-file PATH] [--csv PATH]
"""
from __future__ import annotations

import sys as _sys
import pathlib as _pathlib
_REPO_ROOT = str(_pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

import argparse
import asyncio
import csv
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

JST = timezone(timedelta(hours=9))
MIN_AGE_MIN = 60          # これより新しい応募は通常の同期に任せる
IN_CHUNK = 100            # HubSpot Search の IN は 100 値まで
HS = "https://api.hubapi.com"


def short_key(applicant_id: str) -> str:
    return hashlib.sha256(applicant_id.encode("utf-8")).hexdigest()[:8]


def read_raw_by_id(path: Path) -> dict:
    """応募者id -> CSVの生の行 (応募日時・応募経路・店舗ID を読むため)。HR の CSV は Shift-JIS。"""
    data = Path(path).read_bytes()
    for enc in ("cp932", "utf-8-sig"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("応募CSVの文字コードを判定できません")
    import io
    return {str(r.get("応募者id") or "").strip(): r
            for r in csv.DictReader(io.StringIO(text)) if r.get("応募者id")}


def applied_at(raw: dict) -> Optional[datetime]:
    s = str(raw.get("応募日時") or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19] if len(s) >= 19 else s, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def classify(rows: Iterable, raw_by_id: dict, existing_ids: set,
             find_existing: Callable, now: datetime,
             min_age_min: int = MIN_AGE_MIN) -> dict:
    """rows (ApplicantRow) を 取り込み済み / 重複 / 新しすぎ / 取りこぼし に分ける。

    find_existing(row) -> 既存の応募ID or None (取り込みと同じ重複判定)。
    """
    out = {"checked": 0, "in_hubspot": 0, "duplicate": 0, "too_new": 0,
           "no_id": 0, "missing": []}
    for row in rows:
        out["checked"] += 1
        hid = (getattr(row, "hr_applicant_id", "") or "").strip()
        if not hid:
            out["no_id"] += 1
            continue
        if hid in existing_ids:
            out["in_hubspot"] += 1
            continue
        t = applied_at(raw_by_id.get(hid, {}))
        if t is not None and now - t < timedelta(minutes=min_age_min):
            out["too_new"] += 1
            continue
        if find_existing(row):
            out["duplicate"] += 1
            continue
        out["missing"].append(row)
    return out


def describe(rows: Iterable, raw_by_id: dict) -> list[dict]:
    """公開してよい項目だけの一覧 (日時・応募経路の番号・店舗ID・hash)。"""
    items = []
    for row in rows:
        raw = raw_by_id.get(row.hr_applicant_id, {})
        t = applied_at(raw)
        items.append({
            "applied": t.strftime("%Y-%m-%d %H:%M") if t else (row.apply_date or ""),
            "route": str(raw.get("応募経路") or "").strip(),
            "shop_id": str(raw.get("店舗ID") or "").strip(),
            "key": short_key(row.hr_applicant_id),
        })
    return sorted(items, key=lambda x: x["applied"])


def fetch_existing_ids(token: str, ids: list[str],
                       post: Optional[Callable] = None) -> set:
    """hr_oubosha_id が ids のどれかである 0-421 の、実在する応募者idの集合。"""
    post = post or (lambda body: requests.post(
        f"{HS}/crm/v3/objects/0-421/search",
        headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30))
    found: set = set()
    for i in range(0, len(ids), IN_CHUNK):
        chunk = ids[i:i + IN_CHUNK]
        after = None
        while True:
            body = {"filterGroups": [{"filters": [
                {"propertyName": "hr_oubosha_id", "operator": "IN", "values": chunk}]}],
                "properties": ["hr_oubosha_id"], "limit": 200}
            if after:
                body["after"] = after
            for attempt in range(5):
                r = post(body)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 + attempt * 2)
                    continue
                break
            if r.status_code >= 300:
                raise RuntimeError(f"HubSpot の検索に失敗しました (HTTP {r.status_code})")
            js = r.json()
            found |= {str((x.get("properties") or {}).get("hr_oubosha_id") or "")
                      for x in js.get("results", [])}
            after = (js.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
        time.sleep(0.3)   # Search は 1 秒 5 回まで。外部バッチと分け合う
    found.discard("")
    return found


def slack(message: str) -> None:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=15)
    except requests.RequestException:
        pass


def _fmt_items(items: list[dict], limit: int = 20) -> str:
    lines = [f"  {x['applied']} 経路{x['route'] or '-'} 店舗{x['shop_id'] or '-'} ({x['key']})"
             for x in items[:limit]]
    if len(items) > limit:
        lines.append(f"  ほか {len(items) - limit} 件")
    return "\n".join(lines)


def import_missing(missing: list, cli, detail=None) -> dict:
    """取りこぼしを通常の取り込み (run_import) で登録し、詳細ページの項目も同じように書く。"""
    from scripts.job_application_sync import applicant_import as ai
    from scripts.job_application_sync import hr_applicant_fields as hrf
    results = ai.run_import(missing, cli)
    out = {"status": dict(Counter(r.status for r in results))}
    if detail is not None and getattr(detail, "parsed", None):
        page_fields = hrf.active_page_fields(cli.known_appointment_props())
        appt_of = {r.hr_applicant_id: r.appointment_id for r in results
                   if getattr(r, "hr_applicant_id", "") and getattr(r, "appointment_id", None)}
        page = Counter()
        for hr_id, parsed in detail.parsed.items():
            appt = appt_of.get(hr_id)
            page[hrf.apply_page_fields(cli, appt, parsed, page_fields) if appt and page_fields
                 else "skipped"] += 1
        out["detail"] = dict(page)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="HR応募の取りこぼし照合")
    ap.add_argument("--days", type=int, default=3,
                    help="HR の応募一覧を何日さかのぼるか (既定 3。毎時動かす前提)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    ap.add_argument("--import-missing", action="store_true",
                    help="取りこぼしを通常の取り込み (run_import) で登録する。--actual のときだけ書く")
    ap.add_argument("--slack", action="store_true")
    ap.add_argument("--status-file", default="")
    ap.add_argument("--csv", default="", help="取得済みの応募CSV (手元の確認用。詳細ページは取らない)")
    a = ap.parse_args(argv)

    from scripts.job_application_sync import applicant_import as ai

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        print("::error::HUBSPOT_ACCESS_TOKEN がありません")
        return 2
    now = datetime.now(JST)
    date_to = now.date().isoformat()
    date_from = (now.date() - timedelta(days=a.days)).isoformat()
    write = a.import_missing and not a.dry_run
    cli = ai.RealHubSpotClient(token)

    def find_existing(row):
        return cli.find_existing_appointment(
            row.media, row.media_job_id, row.phone, row.email, apply_date=row.apply_date)

    state: dict = {}

    def check(csv_path) -> list:
        """CSV を突き合わせる。取りこぼしの応募者id (詳細ページを取る対象) を返す。"""
        rows = [r for r in ai.load_applicants_csv(csv_path) if r.hr_applicant_id]
        raw_by_id = read_raw_by_id(Path(csv_path))
        existing = fetch_existing_ids(token, sorted({r.hr_applicant_id for r in rows}))
        state.update(raw_by_id=raw_by_id,
                     res=classify(rows, raw_by_id, existing, find_existing, now))
        return [r.hr_applicant_id for r in state["res"]["missing"]] if write else []

    tmp = None
    detail = None
    try:
        if a.csv:
            check(a.csv)
        else:
            from scripts.job_application_sync.fetchers import hr_applicant_fetcher as hf
            tmp = Path(tempfile.mkdtemp(prefix="hr_reconcile_"))
            # CSV を取った同じログインのまま、取りこぼしの人だけ詳細ページを取る
            _, detail = asyncio.run(hf.fetch_hr_applicants_with_details(
                tmp, date_from, date_to, check, headless=True))
        res, raw_by_id = state["res"], state["raw_by_id"]
        missing = res["missing"]
        status = {
            "checked_at": now.isoformat(timespec="seconds"),
            "range": [date_from, date_to],
            "checked": res["checked"], "in_hubspot": res["in_hubspot"],
            "duplicate": res["duplicate"], "too_new": res["too_new"], "no_id": res["no_id"],
            "missing": len(missing), "missing_items": describe(missing, raw_by_id),
            "imported": {}, "added": 0, "remaining": len(missing), "remaining_items": [],
            "dry_run": a.dry_run,
        }
        print(f"[hr_reconcile] 範囲={date_from}〜{date_to} HR={res['checked']} "
              f"取り込み済み={res['in_hubspot']} 重複={res['duplicate']} "
              f"応募から{MIN_AGE_MIN}分未満={res['too_new']} 取りこぼし={len(missing)}")
        if missing:
            print(_fmt_items(status["missing_items"]))

        remaining = missing
        if missing and write:
            out = import_missing(missing, cli, detail)
            status["imported"] = out
            print(f"[hr_reconcile] 追加登録: {out}")
            # 登録したあとにもう一度確かめる
            again = fetch_existing_ids(token, [r.hr_applicant_id for r in missing])
            remaining = [r for r in missing
                         if r.hr_applicant_id not in again and not find_existing(r)]
            status["added"] = len(missing) - len(remaining)
        status["remaining"] = len(remaining)
        status["remaining_items"] = describe(remaining, raw_by_id)

        # 残ったものの知らせは、毎時くり返さないようワークフローが Issue を立てるときに 1 回だけ送る
        if a.slack and write and status["added"]:
            slack(f"照合で {status['added']} 件を追加登録")
        if a.status_file:
            Path(a.status_file).parent.mkdir(parents=True, exist_ok=True)
            Path(a.status_file).write_text(json.dumps(status, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _sys.exit(main())
