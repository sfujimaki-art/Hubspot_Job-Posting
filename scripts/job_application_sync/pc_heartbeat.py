"""専用PCの HR の処理が動いているかを、非公開シートのタブ「PC稼働」で見る (2026-11-19 決定)。

なぜ:
  HR の応募の登録と求人の監視を GitHub Actions から専用PCへ移した。Actions を予備に
  残さない (同時に動くと二重登録になる) ので、PC が止まると誰も取り込まない。
  PC が止まっていると PC からは知らせられないので、PC は終わるたびに時刻を書き、
  Actions の軽い定期ジョブ (pc_heartbeat_watch.yml) がそれを読んで古ければ Slack に送る。

書く場所: 集約シート (JAS_APPLICANT_QUEUE_SHEET_ID) のタブ「PC稼働」。1 処理 1 行を上書き。
  A=処理 / B=最後に終わった時刻 / C=結果 / D=知らせた時刻 (見張りが書く)

使い方:
  PC (pc_runner.py から):  beat("HR応募", "ok")
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
HEADER = ["処理", "最後に終わった時刻", "結果", "知らせた時刻"]
# 処理ごとの「これより古ければ止まっている」(分)。
#   応募は 5 分おき → 30 分。求人は JST 1/12/20 時 (最長の間隔は 1→12 時の 11 時間) → 13 時間。
LIMIT_MIN = {"HR応募": 30, "HR求人": 13 * 60}
# 止まったままなら、この間隔でもう一度知らせる
RENOTIFY_MIN = 6 * 60


def _now() -> datetime:
    return datetime.now(JST)


def _sheet():
    from scripts.job_application_sync.fetchers import account_loader as al
    sid = os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", "")
    if not sid:
        raise RuntimeError("JAS_APPLICANT_QUEUE_SHEET_ID が未設定")
    gc = al.get_sheets_client()
    sh = al.sheet_retry(gc.open_by_key, sid)
    try:
        ws = al.sheet_retry(sh.worksheet, TAB)
    except Exception:  # noqa: BLE001 — タブが無ければ作る
        ws = al.sheet_retry(sh.add_worksheet, title=TAB, rows=20, cols=len(HEADER))
        al.sheet_retry(ws.update, values=[HEADER], range_name="A1:D1")
    return al, ws


def _rows(al, ws) -> dict:
    """{処理: (行番号, [A..D])}"""
    vals = al.sheet_retry(ws.get, "A1:D50") or []
    out = {}
    for i, r in enumerate(vals, start=1):
        r = list(r) + [""] * (4 - len(r))
        if i > 1 and r[0]:
            out[r[0]] = (i, r[:4])
    return out


def _write(al, ws, rows: dict, job: str, values: dict) -> None:
    """job の行の B/C/D のうち values にある列だけ書く。行が無ければ足す。"""
    if job in rows:
        n, cur = rows[job]
    else:
        n = max([1] + [x[0] for x in rows.values()]) + 1
        cur = [job, "", "", ""]
        rows[job] = (n, cur)
    new = list(cur)
    for col, v in values.items():
        new[col] = v
    al.sheet_retry(ws.update, values=[new], range_name=f"A{n}:D{n}")
    rows[job] = (n, new)


def beat(job: str, result: str = "ok") -> bool:
    """PC が 1 回終わるたびに呼ぶ。失敗しても処理そのものは止めない (False を返すだけ)。"""
    try:
        al, ws = _sheet()
        _write(al, ws, _rows(al, ws), job,
               {1: _now().isoformat(timespec="seconds"), 2: result[:100]})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[pc_heartbeat] 稼働の記録を書けませんでした ({type(e).__name__})", flush=True)
        return False


def _parse(s: str):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


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
    """'alert' / 'recovered' / '' (何もしない)。"""
    last = _parse(last_ok)
    stale = last is None or (now - last) > timedelta(minutes=LIMIT_MIN[job])
    noti = _parse(notified)
    if stale:
        if noti is None or (now - noti) >= timedelta(minutes=RENOTIFY_MIN):
            return "alert"
        return ""
    return "recovered" if noti is not None else ""


def check() -> int:
    al, ws = _sheet()
    rows = _rows(al, ws)
    now = _now()
    for job, limit in LIMIT_MIN.items():
        _, cur = rows.get(job, (0, [job, "", "", ""]))
        what = decide(job, cur[1], cur[3], now)
        print(f"[pc_heartbeat] {job}: 最後={cur[1] or 'なし'} 結果={cur[2] or '-'} → {what or 'そのまま'}",
              flush=True)
        if what == "alert":
            last = cur[1] or "記録なし"
            msg = (f"⚠️ [Actions] 専用PCの {job} が {limit // 60 if limit >= 60 else limit}"
                   f"{'時間' if limit >= 60 else '分'}以上終わっていません (最後に終わった時刻: {last})。\n"
                   "→ PC の電源・ネット・タスクスケジューラの状態を確かめてください"
                   " (手順書の「止まったとき」)。")
            if _slack(msg):
                _write(al, ws, rows, job, {3: now.isoformat(timespec="seconds")})
        elif what == "recovered":
            if _slack(f"✅ [Actions] 専用PCの {job} が戻りました (最後に終わった時刻: {cur[1]})。"):
                _write(al, ws, rows, job, {3: ""})
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
