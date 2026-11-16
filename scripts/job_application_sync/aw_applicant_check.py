# -*- coding: utf-8 -*-
"""AirWork の応募一覧と HubSpot を突き合わせ、HubSpot に無い応募を知らせる (2026-11-05)。

## なぜ要るか

応募の取り込み (applicant_sync) は**シート1 の行**を起点に動く。応募通知メールが
シート1 に入らなかった応募は、取り込みにも通知にも一切現れない (2026-11-04 実測:
ある顧客の拠点への応募が HubSpot に無いと現場から連絡があり、シート1 に行が無かった)。

夜の巡回は毎晩、全社の AirWork にログインしている。その社の応募一覧 CSV も
読み、直近の応募が HubSpot にあるかを確かめる。無いものだけを Slack と
集約シートの「報告_応募取り込み」に出す。

  - 見るのは**昨日まで**。最初の日は直近 N 日 (既定 3 日) か、前回その社を確かめた日の続きの
    早い方 (止めていた期間の分も見る、2026-11-16)。今日の分は通常の取り込みの途中でありうる
  - 求人の取得を待っている (取り込みの途中の) 応募は知らせず、次の夜に確かめ直す
  - 照合は取り込みの重複判定と同じ (媒体・求人ID・応募日・メール or 電話)。
    人が手入力した応募もメールか電話が合えば「ある」と見なす
  - HubSpot へは書かない。取り込むかどうかは人が決める
    (手入力との二重登録を避けるため。2026-10-26「手作業の分はスルー、ダブるから」)
  - 同じ応募は 1 回だけ知らせる

使い方:
  夜の巡回の各社の処理の中で check_company() (環境変数 JAS_AW_APPLICANT_CHECK=1 のときだけ)
  巡回の最後に python scripts/job_application_sync/aw_applicant_check.py --report
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync import private_log as plog  # noqa: E402

STATE_PATH = _REPO / "data" / "job_application_sync" / "aw_applicant_missing.json"
DAYS = int(os.environ.get("JAS_AW_APPLICANT_CHECK_DAYS", "3") or 3)
KEEP_DAYS = 30
_LOCK = threading.Lock()


CHECKED_PATH = _REPO / "data" / "job_application_sync" / "aw_applicant_checked.json"
MAX_BACK_DAYS = 14


def window(today: Optional[date] = None, days: int = DAYS,
           prev_to: Optional[str] = None) -> tuple:
    """(最初の日, 最後の日) の YYYY-MM-DD。昨日まで。

    最初の日は「今日の days 日前」と「前回その社を確かめた最後の日の days 日前」の早い方
    (ただし MAX_BACK_DAYS 日前より前には戻らない)。

    ★2026-11-16: 以前は常に直近 days 日だけを見ていた。PC や巡回を止めた日・その社の
      巡回が失敗した日があると、その間の応募が一度も突き合わされないまま窓から外れる。
      前回の続きから見るので、止めていた期間の分も確かめる。前回の窓の分も見直すのは、
      前回「取り込みの途中」として見送った応募をもう一度確かめるため。
    """
    today = today or date.today()
    frm = today - timedelta(days=days)
    if prev_to:
        try:
            frm = min(frm, date.fromisoformat(prev_to) - timedelta(days=days))
        except ValueError:
            pass
    frm = max(frm, today - timedelta(days=MAX_BACK_DAYS))
    return frm.isoformat(), (today - timedelta(days=1)).isoformat()


def _login_key(login_id: str) -> str:
    return hashlib.sha256(login_id.lower().encode("utf-8")).hexdigest()[:16]


def last_checked_to(login_id: str) -> Optional[str]:
    try:
        d = json.loads(CHECKED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # "__default__": その社の記録がまだ無いときに使う日 (この仕組みを入れた時点で、
    # 全社を最後に確かめた日。2026-11-16 は 11/12 の夜に確かめた 11/11)
    return d.get(_login_key(login_id)) or d.get("__default__")


def set_checked_to(login_id: str, to: str) -> None:
    with _LOCK:
        try:
            d = json.loads(CHECKED_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[_login_key(login_id)] = to
        CHECKED_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHECKED_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(CHECKED_PATH)


def in_progress_job_ids() -> set:
    """取り込みの途中の求人ID (求人の取得を待っている)。これらの応募は、取り込みが求人を
    待って止めているだけなので「HubSpot に無い」と知らせない (知らせると手で入れられて
    二重になる)。次の夜にもう一度確かめる。"""
    try:
        from scripts.job_application_sync.fetchers import aw_urgent_queue as uq
        return uq.pending_job_ids()
    except Exception:  # noqa: BLE001
        return set()


def applicant_key(login_id: str, row) -> str:
    raw = "|".join([login_id.lower(), row.apply_date, row.media_job_id or "",
                    (row.email or "").strip().lower(), (row.phone or "").strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def find_missing(rows: list, client, frm: str, to: str) -> tuple:
    """(確かめた件数, HubSpot に無い行)。照合は取り込みの重複判定と同じ。

    メールも電話も無い行は照合できないので数えない (無いと断定しない)。
    """
    checked, missing = 0, []
    for r in rows:
        if not (frm <= (r.apply_date or "") <= to):
            continue
        if not (r.email or r.phone):
            continue
        checked += 1
        if not client.find_existing_appointment(r.media, r.media_job_id, r.phone,
                                                r.email, r.apply_date):
            missing.append(r)
    return checked, missing


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def record(login_id: str, company: str, missing: list, now: Optional[datetime] = None) -> int:
    """HubSpot に無い応募を状態ファイルに積む。初めて見たものの件数を返す。"""
    now = now or datetime.now()
    new = 0
    with _LOCK:
        st = _load()
        for r in missing:
            k = applicant_key(login_id, r)
            if k in st:
                st[k]["last_seen"] = now.isoformat()
                continue
            st[k] = {"company": company, "apply_date": r.apply_date,
                     "media_job_id": r.media_job_id or "", "name": r.name or "",
                     "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                     "notified": ""}
            new += 1
        _save(st)
    return new


def check_company(login_id: str, password: str, company: str,
                  out_dir: Path, token: str, headless: bool = True) -> dict:
    """1 社の応募一覧 CSV を読み、直近の応募で HubSpot に無いものを積む。"""
    from scripts.job_application_sync import applicant_import as ai
    from scripts.job_application_sync import applicant_sync as asy
    from scripts.job_application_sync.fetchers import aw_applicant_fetcher as af
    try:
        csv_path = asyncio.run(af.fetch_aw_applicants(
            login_id, password, out_dir, headless=headless,
            storage_state_path=asy._session_path(login_id)))
    except af.NoApplicantsError:
        set_checked_to(login_id, window()[1])
        return {"checked": 0, "missing": 0, "new": 0, "empty": True}
    rows = ai.load_applicants_csv(str(csv_path))
    frm, to = window(prev_to=last_checked_to(login_id))
    checked, missing = find_missing(rows, ai.RealHubSpotClient(token), frm, to)
    waiting = in_progress_job_ids()
    deferred = [r for r in missing if (r.media_job_id or "") in waiting]
    missing = [r for r in missing if (r.media_job_id or "") not in waiting]
    new = record(login_id, company, missing) if missing else 0
    set_checked_to(login_id, to)
    return {"checked": checked, "missing": len(missing), "new": new,
            "deferred": len(deferred), "from": frm}


def report(dry_run: bool = False, now: Optional[datetime] = None) -> int:
    """まだ知らせていない「HubSpot に無い応募」を Slack と集約シートに出す。件数を返す。"""
    from scripts.job_application_sync import applicant_sync as asy
    from scripts.job_application_sync import report_sheet
    now = now or datetime.now()
    with _LOCK:
        st = _load()
        todo = {k: v for k, v in st.items() if not v.get("notified")}
        if todo:
            by_co: dict = {}
            for v in todo.values():
                by_co.setdefault(v["company"], []).append(v)
            rows = [report_sheet.row(
                "AirWorkにあるがHubSpotに無い応募", "NG", v["company"],
                f"応募日 {v['apply_date']} / 求人ID {v['media_job_id']} / {v['name']}",
                "シート1に応募通知の行が無い・取り込みが止まっている、のどちらか",
                "シート1と取り込みの状況を確かめ、必要なら手で入れる (二重登録に注意)")
                for vs in by_co.values() for v in vs]
            lines = [f"・{c[:24]}: {len(vs)}件 (応募日 "
                     f"{min(v['apply_date'] for v in vs)}〜{max(v['apply_date'] for v in vs)})"
                     for c, vs in sorted(by_co.items())]
            sent = asy.slack_notify(dry_run=dry_run, message=(
                f"⚠️ 応募の突き合わせ: AirWork にあるのに HubSpot に無い応募 "
                f"{len(todo)}件 ({len(by_co)}社)\n" + "\n".join(lines[:15])
                + (f"\n…ほか{len(lines) - 15}社" if len(lines) > 15 else "")
                + "\n→ 夜の巡回で各社の応募一覧と HubSpot を突き合わせた結果です。"
                  "シート1 に通知が入っていない応募は、通常の取り込みでは拾えません。"
                  "同じ応募のこの通知は1回だけです"), rows=rows)
            if sent and not dry_run:
                for k in todo:
                    st[k]["notified"] = now.isoformat()
        # 古いものは捨てる (応募日から KEEP_DAYS 日)
        cut = (now.date() - timedelta(days=KEEP_DAYS)).isoformat()
        st = {k: v for k, v in st.items() if v.get("apply_date", "") >= cut}
        if not dry_run:
            _save(st)
    print(f"[applicant_check] 未通知 {len(todo)}件"
          + (" (dry-run・未送信)" if dry_run and todo else ""), flush=True)
    plog.detail("aw_applicant_missing", n=len(todo))
    return len(todo)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="未通知の分を Slack に出す")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.report:
        from dotenv import load_dotenv
        load_dotenv(_REPO / ".env")
        report(dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
