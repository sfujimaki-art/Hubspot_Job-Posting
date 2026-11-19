"""専用PC で HR の処理を動かす入口 (2026-11-19 決定。GitHub Actions から移した)。

なぜ:
  GitHub の 5 分おきの起動 (GAS からの dispatch) と cron は、着く時刻・かかる時間がばらつく
  (ブラウザの導入だけで 4〜11 分、待っている起動が後の起動に置き換えられて取り消し)。
  AirWork を動かしている専用PC でタスクスケジューラから動かす。

使い方 (タスクスケジューラから。作業フォルダはリポのルート):
  python scripts/job_application_sync/pc_runner.py applicants   # 5 分おき (応募の登録)
  python scripts/job_application_sync/pc_runner.py listings     # JAS 1/12/20 時 (求人の監視)
  --dry-run で HubSpot・シートに書かずに試す。`--` の後ろは applicant_sync.py にそのまま渡す
  (例: 14 日より前の通知の回収 `applicants -- --hr-cutoff 2026-11-01`)。

決まり:
  - 同じ処理の前の回が終わっていなければ、その回は飛ばす (ファイルの鍵。PC が落ちても OS が外す)。
  - 重なったら応募を先に: 求人の監視は、応募の回が動いている間は始めるのを待つ (最大 15 分)。
    応募の回は求人の監視を待たない (Actions でも 2 つは別々に同時に動いていた)。
  - HR は AirWork に触らないので、AW の順番の鍵 (fetchers/aw_pace.py) には入らない。
  - 終わったら非公開シートのタブ「PC稼働」に時刻を書く (pc_heartbeat.py)。
    Actions の見張り (pc_heartbeat_watch.yml) がそれを読み、古ければ Slack に送る。
  - 出力は scratchpad/pc_logs/<処理>-<日付>.log に足していく (14 日より古いものは消す)。
  - 回ごとに結果を「成功 / 飛ばした / 失敗 / 終了 (読めない)」に分けて書く。見張りは成功の時刻だけを見る。
  - 溜まるファイルを消す: 応募者の CSV (個人情報) は 3 日、求人 CSV・店舗一覧・求人の控えは 14 日
    (どれも最新の 1 つは残す。最新の求人 CSV は店舗ID補完・持ち主判定が読む)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:  # pragma: no cover
    pass

_DATA = _REPO / "data" / "job_application_sync"
LOG_DIR = _REPO / "scratchpad" / "pc_logs"
LOG_KEEP_DAYS = 14
APPLICANT_KEEP_DAYS = 3   # 応募者の CSV (個人情報入り)
LOCKS = {"applicants": _DATA / "pc_hr_applicants.lock",
         "listings": _DATA / "pc_hr_listings.lock"}
NAMES = {"applicants": "HR応募", "listings": "HR求人"}
# 子の処理の打ち切り (分)。タスクスケジューラの「停止するまでの時間」より短くする
TIMEOUT_MIN = {"applicants": 25, "listings": 100}
WAIT_FOR_APPLICANTS_MIN = 15
PY = sys.executable
S = "scripts/job_application_sync/"


@contextmanager
def try_lock(path: Path):
    """取れたら True、ほかが持っていたら False を渡す。待たない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    got = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _log_path(job: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cut = time.time() - LOG_KEEP_DAYS * 86400
    for p in LOG_DIR.glob("*.log"):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
        except OSError:
            pass
    return LOG_DIR / f"{job}-{datetime.now():%Y%m%d}.log"


def _say(log, text: str) -> None:
    line = f"[pc_runner {datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()


def _run(log, args: list, timeout_min: float) -> int:
    """子の処理を動かし、出力をログに足す。時間切れならブラウザごと止めて 124 を返す。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    _say(log, "開始: " + " ".join(a for a in args[1:]))
    p = subprocess.Popen([PY, *args], cwd=str(_REPO), env=env,
                         stdout=log, stderr=subprocess.STDOUT)
    try:
        rc = p.wait(timeout=timeout_min * 60)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            p.kill()
        p.wait()
        rc = 124
        _say(log, f"時間切れ ({timeout_min:.0f} 分) で止めました")
    _say(log, f"終了: rc={rc}")
    return rc


def _new_text(log, pos: int) -> str:
    """この回で子の処理がログに足した分。"""
    log.flush()
    try:
        with open(log.name, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            return f.read()
    except OSError:
        return ""


def classify_applicants(rc: int, text: str) -> tuple:
    """(結果, 成功か)。applicant_sync.py の出力から分かる範囲で分ける。"""
    if rc != 0:
        return f"失敗 (rc={rc})", False
    if "別インスタンス実行中 -> skip" in text:
        return "飛ばした (別の回が実行中)", False
    if "❌ HR:" in text or "⏳ HR:" in text:
        return "失敗 (HRの取得・ログイン)", False
    m = re.search(r"\[applicant_sync-done\].*'failed': (\d+)", text)
    if m and int(m.group(1)) > 0:
        return f"失敗 (failed={m.group(1)})", False
    if "✅ HR:" in text:
        return "成功", True
    if "[applicant_sync-done]" in text:
        return "成功 (新しい通知なし)", True
    return "終了 (結果が読めない)", False


def classify_listings(rc: int, st: dict) -> tuple:
    """(結果, 成功か)。hr_watcher.py の結果 (--status-file) から分ける。"""
    if rc != 0:
        return f"失敗 (hr_watcher rc={rc})", False
    if not st:
        return "終了 (結果が読めない)", False
    if st.get("skip_reason") == "incomplete_csv":
        return "止めた (CSV欠け)", False
    if st.get("hrhacker_error") or st.get("snapshot_kept_previous"):
        return "失敗 (取り込み。控えを残した)", False
    if st.get("aborted"):
        # 控えが無い初回は HubSpot に書かずに控えだけ作る (hr_watcher の初回の安全装置)
        return "成功 (初回の控え作り)", True
    reason = st.get("skip_reason")
    if reason == "no_diff":
        return "成功 (差分なし)", True
    if reason == "skip_aw_flag":
        return "成功 (取り込み済み)", True
    return f"終了 ({reason or '理由なし'})", False


def applicants(log, dry_run: bool, extra: list) -> tuple:
    pos = log.tell()
    rc = _run(log, [S + "applicant_sync.py", "--dry-run" if dry_run else "--actual",
                    "--source", "sheet1", "--media", "HR", *extra],
              TIMEOUT_MIN["applicants"])
    return (rc, *classify_applicants(rc, _new_text(log, pos)))


def listings(log, dry_run: bool, extra: list) -> tuple:
    # job_daily.yml の phase=hr と同じ順番 (2026-11-19 時点)
    flag = "--dry-run" if dry_run else "--actual"
    status = _REPO / "scratchpad" / "hr_watch_status.json"
    try:
        status.unlink()
    except OSError:
        pass
    budget = TIMEOUT_MIN["listings"]
    t0 = time.time()
    rc = _run(log, [S + "hr_watcher.py", flag, "--skip-aw", "--slack",
                    "--status-file", str(status)], budget)
    try:
        st = json.loads(status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    result, ok = classify_listings(rc, st)
    if rc != 0:
        return rc, result, ok
    if st.get("skip_reason") == "incomplete_csv":
        _say(log, "CSV欠け: 後続の店舗ID補完・持ち主判定も止めます")
        return 0, result, ok
    for script in ("backfill_deal_shop_id_via_email.py", "resolve_listing_owner.py"):
        left = budget - (time.time() - t0) / 60
        if left < 1:
            _say(log, f"時間が残っていないので {script} を飛ばします")
            continue
        r = _run(log, [S + script, flag], left)
        if r != 0:
            _say(log, f"⚠️ {script} が失敗 (本線は継続・前回の結果を使う)")
    return 0, result, ok


def _old(p: Path, days: float, now: float) -> bool:
    try:
        return p.stat().st_mtime < now - days * 86400
    except OSError:
        return False


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def cleanup(now: float = None) -> None:
    """PC に溜まるファイルを消す。最新の求人 CSV・店舗一覧・控えは必ず残す。
    応募者の CSV (個人情報) は 3 日、ほかは 14 日。"""
    now = time.time() if now is None else now
    sp = _REPO / "scratchpad"
    for d in (sp / "applicant_sync_csv", sp / "csv_fetched" / "hr_applicants"):
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file() and _old(p, APPLICANT_KEEP_DAYS, now):
                    _unlink(p)

    def keep_newest(files: list, newest_of: list) -> None:
        latest = max(newest_of, key=lambda p: p.name, default=None)
        for p in files:
            if p != latest and _old(p, LOG_KEEP_DAYS, now):
                _unlink(p)

    hr = sp / "csv_fetched" / "hr"
    if hr.exists():
        # backfill_deal_shop_id_via_email.py / resolve_listing_owner.py は最新の hr_offers_all_* を読む
        keep_newest(list(hr.glob("hr_offers_*.csv")), list(hr.glob("hr_offers_all_*.csv")))
    br = sp / "csv_fetched" / "hr_branches"
    if br.exists():
        files = list(br.glob("*.csv"))
        keep_newest(files, files)
    snaps = _DATA / "hr_snapshots"
    if snaps.exists():
        files = list(snaps.glob("*.json.gz"))
        keep_newest(files, files)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(description="専用PC で HR の処理を動かす")
    p.add_argument("job", choices=["applicants", "listings"])
    p.add_argument("--dry-run", action="store_true", help="HubSpot・シートに書かない")
    a = p.parse_args(argv)
    with open(_log_path(a.job), "a", encoding="utf-8") as log:
        with try_lock(LOCKS[a.job]) as got:
            if not got:
                _say(log, "前の回がまだ動いているので、この回は飛ばします")
                rc, result, ok = 0, "飛ばした (前の回が実行中)", False
            else:
                cleanup()
                if a.job == "listings":
                    waited = 0.0
                    while waited < WAIT_FOR_APPLICANTS_MIN * 60:
                        with try_lock(LOCKS["applicants"]) as free:
                            if free:
                                break
                        if waited == 0:
                            _say(log, "応募の回が動いているので、終わるまで待ちます")
                        time.sleep(10)
                        waited += 10
                fn = applicants if a.job == "applicants" else listings
                rc, result, ok = fn(log, a.dry_run, extra)
        _say(log, f"結果: {NAMES[a.job]} {result}")
        if not a.dry_run:
            from scripts.job_application_sync import pc_heartbeat
            if pc_heartbeat.beat(NAMES[a.job], result, success=ok):
                _say(log, f"稼働の記録: {NAMES[a.job]} {result}"
                          f"{' (最後に成功した時刻も更新)' if ok else ''}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
