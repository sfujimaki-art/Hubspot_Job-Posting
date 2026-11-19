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
"""
from __future__ import annotations

import argparse
import json
import os
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


def applicants(log, dry_run: bool, extra: list) -> tuple:
    rc = _run(log, [S + "applicant_sync.py", "--dry-run" if dry_run else "--actual",
                    "--source", "sheet1", "--media", "HR", *extra],
              TIMEOUT_MIN["applicants"])
    return rc, ("ok" if rc == 0 else f"rc={rc}")


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
    if rc != 0:
        return rc, f"hr_watcher rc={rc}"
    try:
        st = json.loads(status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if st.get("skip_reason") == "incomplete_csv":
        _say(log, "CSV欠け: 後続の店舗ID補完・持ち主判定も止めます (正常終了)")
        return 0, "CSV欠け"
    for script in ("backfill_deal_shop_id_via_email.py", "resolve_listing_owner.py"):
        left = budget - (time.time() - t0) / 60
        if left < 1:
            _say(log, f"時間が残っていないので {script} を飛ばします")
            continue
        r = _run(log, [S + script, flag], left)
        if r != 0:
            _say(log, f"⚠️ {script} が失敗 (本線は継続・前回の結果を使う)")
    # 控えが無い初回は、HubSpot に書かずに控えだけ作って止まる (hr_watcher の初回の安全装置)
    return 0, (st.get("skip_reason") or ("初回の控え作り" if st.get("aborted") else "ok"))


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
                return 0
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
            rc, result = fn(log, a.dry_run, extra)
        if not a.dry_run:
            from scripts.job_application_sync import pc_heartbeat
            if rc == 0 and pc_heartbeat.beat(NAMES[a.job], result):
                _say(log, f"稼働の記録: {NAMES[a.job]} {result}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
