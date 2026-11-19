"""専用PCの HR の処理が動いているかを、非公開シートのタブ「PC稼働」で見る (2026-11-19 決定)。

なぜ:
  HR の応募の登録と求人の監視を GitHub Actions から専用PCへ移した。Actions を予備に
  残さない (同時に動くと二重登録になる) ので、PC が止まると誰も取り込まない。
  PC が止まっていると PC からは知らせられないので、PC は回ごとに結果を書き、
  Actions の軽い定期ジョブ (pc_heartbeat_watch.yml) が「最後に成功した時刻」を読んで、
  古ければ Slack に送る (失敗や飛ばしが続いても知らせる)。

書く場所: 集約シート (JAS_APPLICANT_QUEUE_SHEET_ID) のタブ「PC稼働」。1 処理 1 行。
  A=処理 / B=最後に成功した時刻 / C=最後の結果 / D=知らせた時刻 / E=最後に動いた時刻
  PC は B・C・E (成功でなければ C・E だけ) を、見張りは D だけを書く。
  タブと 2 行は切り替えの前に手で作る (手順書)。無ければ PC が作り、行は append で足す。
  見張りはタブも行も作らない。行が無い処理 (まだ一度も記録が無い) は知らせない。

使い方:
  PC (pc_runner.py から):  beat("HR応募", "成功", success=True)
  Actions:                 python scripts/job_application_sync/pc_heartbeat.py --check
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

JST = timezone(timedelta(hours=9))
TAB = "PC稼働"
HEADER = ["処理", "最後に成功した時刻", "最後の結果", "知らせた時刻", "最後に動いた時刻"]
NCOL = len(HEADER)
# 処理ごとの「最後の成功がこれより古ければ止まっている」(分)。
#   応募は 5 分おき → 30 分。求人は JST 1/12/20 時 (最長の間隔は 1→12 時の 11 時間) → 13 時間。
LIMIT_MIN = {"HR応募": 30, "HR求人": 13 * 60}
# 止まったままなら、この間隔でもう一度知らせる
RENOTIFY_MIN = 6 * 60


def _now() -> datetime:
    return datetime.now(JST)


def _open(create: bool):
    """(al, ws)。タブが無ければ create=True のときだけ作る (無くて作らないなら ws=None)。"""
    from scripts.job_application_sync.fetchers import account_loader as al
    sid = os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", "")
    if not sid:
        raise RuntimeError("JAS_APPLICANT_QUEUE_SHEET_ID が未設定")
    sh = al.sheet_retry(al.get_sheets_client().open_by_key, sid)
    titles = [w.title for w in al.sheet_retry(sh.worksheets)]
    if TAB in titles:
        return al, al.sheet_retry(sh.worksheet, TAB)
    if not create:
        return al, None
    try:
        ws = al.sheet_retry(sh.add_worksheet, title=TAB, rows=20, cols=NCOL, retries=1)
        al.sheet_retry(ws.append_row, HEADER)
    except Exception:  # noqa: BLE001 — 同時に別の回が作った
        ws = al.sheet_retry(sh.worksheet, TAB)
    return al, ws


def _rows(al, ws) -> dict:
    """{処理: (行番号, [A..E])}。同じ処理が 2 行あれば上の行を使う。"""
    vals = al.sheet_retry(ws.get, "A1:E50") or []
    out = {}
    for i, r in enumerate(vals, start=1):
        r = (list(r) + [""] * NCOL)[:NCOL]
        if i > 1 and r[0] and r[0] not in out:
            out[r[0]] = (i, r)
    return out


def beat(job: str, result: str, success: bool) -> bool:
    """PC が 1 回終わるたびに呼ぶ。失敗しても処理そのものは止めない (False を返すだけ)。"""
    now = _now().isoformat(timespec="seconds")
    try:
        al, ws = _open(create=True)
        rows = _rows(al, ws)
        if job not in rows:
            al.sheet_retry(ws.append_row,
                           [job, now if success else "", result[:100], "", now])
            return True
        n = rows[job][0]
        if success:
            al.sheet_retry(ws.update, values=[[now, result[:100]]], range_name=f"B{n}:C{n}")
        else:
            al.sheet_retry(ws.update, values=[[result[:100]]], range_name=f"C{n}")
        al.sheet_retry(ws.update, values=[[now]], range_name=f"E{n}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[pc_heartbeat] 稼働の記録を書けませんでした ({type(e).__name__})", flush=True)
        return False


def _parse(s: str):
    """読めなければ None。タイムゾーンが無ければ JST とみなす。"""
    try:
        d = datetime.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=JST)


def _slack(text: str) -> bool:
    import requests
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print("[pc_heartbeat] Slack 未設定", flush=True)
        return False
    try:
        return requests.post(url, json={"text": text}, timeout=15).status_code == 200
    except requests.RequestException as e:
        print(f"[pc_heartbeat] Slack 送信失敗 ({type(e).__name__})", flush=True)
        return False


def decide(job: str, last_ok: str, notified: str, now: datetime) -> str:
    """'alert' / 'recovered' / '' (何もしない)。最後に成功した時刻が読めなければ止まっている扱い。"""
    last = _parse(last_ok)
    stale = last is None or (now - last) > timedelta(minutes=LIMIT_MIN[job])
    noti = _parse(notified)
    if stale:
        if noti is None or (now - noti) >= timedelta(minutes=RENOTIFY_MIN):
            return "alert"
        return ""
    return "recovered" if noti is not None else ""


def _span(minutes: int) -> str:
    return f"{minutes // 60}時間" if minutes >= 60 else f"{minutes}分"


def check() -> int:
    al, ws = _open(create=False)
    if ws is None:
        print(f"[pc_heartbeat] タブ「{TAB}」がまだ無い → 何もしない", flush=True)
        return 0
    rows = _rows(al, ws)
    now = _now()
    for job, limit in LIMIT_MIN.items():
        if job not in rows:
            print(f"[pc_heartbeat] {job}: 行がまだ無い → 何もしない", flush=True)
            continue
        n, cur = rows[job]
        what = decide(job, cur[1], cur[3], now)
        print(f"[pc_heartbeat] {job}: 最後の成功={cur[1] or 'なし'} 最後の結果={cur[2] or '-'}"
              f" 最後に動いた={cur[4] or '-'} → {what or 'そのまま'}", flush=True)
        if what == "alert":
            msg = (f"⚠️ [Actions] 専用PCの {job} が {_span(limit)}以上成功していません"
                   f" (最後に成功: {cur[1] or '記録なし'} / 最後の結果: {cur[2] or '-'}"
                   f" / 最後に動いた: {cur[4] or '-'})。\n"
                   "→ PC の電源・ネット・タスクスケジューラ・ログを確かめてください"
                   " (手順書の「止まったとき」)。")
            if _slack(msg):
                al.sheet_retry(ws.update, values=[[now.isoformat(timespec="seconds")]],
                               range_name=f"D{n}")
        elif what == "recovered":
            if _slack(f"✅ [Actions] 専用PCの {job} が戻りました (最後に成功: {cur[1]})。"):
                al.sheet_retry(ws.update, values=[[""]], range_name=f"D{n}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="専用PCの稼働の記録と見張り")
    p.add_argument("--check", action="store_true", help="古ければ Slack に送る (Actions で動かす)")
    a = p.parse_args(argv)
    if a.check:
        return check()
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
