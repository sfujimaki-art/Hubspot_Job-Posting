# -*- coding: utf-8 -*-
"""公開ログと非公開ログを分ける (2026-10-09)。

★なぜ要るか
  本番リポジトリは public で、GitHub Actions のログと成果物は誰でも読める。
  それまでのログには顧客の会社名・担当者の姓・取引ID・取引先コード・取引名・
  サービスアカウントのメール・バケット名・シートIDの先頭が出ていた。
  ユーザー決定 (2026-10-09): リポジトリは public のまま、詳しいログは
  サービスアカウントが既に書き込んでいる非公開の集約シート (JAS_APPLICANT_QUEUE_SHEET_ID) の
  タブ「実行ログ」に置く。

使い方
  from scripts.job_application_sync import private_log as plog
  plog.public(f"書込 {n}件")                       # Actions ログ (件数・状態だけ)
  plog.detail("deal_written", deal_id=did, name=nm)  # 非公開シートへ (stdout には出さない)
  plog.flush()                                       # main の最後 (atexit でも呼ばれる)

シート「実行ログ」の列
  日時(JST) | ワークフロー | 実行ID | スクリプト | イベント | 内容(JSON)
  - 1回の実行で溜めた行を最後に1回の append でまとめて書く
  - 30日より古い行は消す。消すのは先頭行が31日より古いときだけなので、
    実際に消しに行くのは1日1回程度 (セル上限 1,000万に対して行数を抑える)
  - 応募連携は5分ごとに走るので、毎行ではなく「目立つ出来事」だけ detail() する

書けなかったとき (環境変数なし・認証エラー・Sheets 障害)
  ジョブは落とさない。公開ログに件数だけの1行を出す。
  - CI (GITHUB_ACTIONS=true) では行を捨てる。data/job_application_sync は
    Actions のキャッシュに入るため、そこに溜めると実行のたびに肥大し、
    キャッシュ経由で残り続ける。
  - ローカル実行ではリポジトリ外に出ない data/job_application_sync/
    private_log_unsent.jsonl に追記する (git 管理外・成果物にもしない)。

要対応の一覧
  replace_list(タブ名, 行) で同じスプレッドシートのタブを毎回全置換する
  (旧: Actions の成果物 要対応_*.csv / RPOアドレス*.csv。成果物は public)。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

JST = timezone(timedelta(hours=9))
LOG_TAB = "実行ログ"
HEADER = ["日時(JST)", "ワークフロー", "実行ID", "スクリプト", "イベント", "内容"]
TS_FMT = "%Y-%m-%d %H:%M:%S"
RETENTION_DAYS = 30
MAX_ROWS_PER_RUN = 500          # 1回の実行で溜める上限 (超えた分は件数だけ残す)
MAX_CELL_CHARS = 5000           # セル上限 50,000 字より十分小さく
LOCAL_FALLBACK = Path("data/job_application_sync/private_log_unsent.jsonl")

_buffer: list = []
_dropped = 0

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ---------------------------------------------------------------- 伏字
def mask_name(s: Any) -> str:
    """会社名・人名・取引名など: 先頭2文字 + ***。空は空のまま。"""
    t = "" if s is None else str(s).strip()
    if not t:
        return ""
    return t[:2] + "***"


def mask_id(s: Any) -> str:
    """ID・コード・メールなど: 先頭2 + *** + 末尾2。短い値は先頭だけ残す。"""
    t = "" if s is None else str(s).strip()
    if not t:
        return ""
    if len(t) <= 4:
        return t[:1] + "***"
    return t[:2] + "***" + t[-2:]


def redact(text: Any, values: Iterable[Any] = ()) -> str:
    """自由文 (例外文・エラー理由) からメール形式と指定の値を伏せる。"""
    t = "" if text is None else str(text)
    for v in sorted({str(x) for x in values if x and len(str(x)) >= 3},
                    key=len, reverse=True):
        t = t.replace(v, mask_id(v))
    return _EMAIL.sub(lambda m: mask_id(m.group(0)), t)


# ---------------------------------------------------------------- 出力
def public(msg: str) -> None:
    """Actions ログに出す。件数・状態・伏字済みの値だけを渡すこと。"""
    print(msg, flush=True)


def _script() -> str:
    try:
        return Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    except Exception:  # noqa: BLE001
        return ""


def detail(event: str, **fields: Any) -> None:
    """非公開シート用の行を溜める。stdout には何も出さない。"""
    global _dropped
    if len(_buffer) >= MAX_ROWS_PER_RUN:
        _dropped += 1
        return
    body = json.dumps(fields, ensure_ascii=False, default=str)
    if len(body) > MAX_CELL_CHARS:
        body = body[:MAX_CELL_CHARS] + "…(省略)"
    _buffer.append([datetime.now(JST).strftime(TS_FMT),
                    os.environ.get("GITHUB_WORKFLOW", ""),
                    os.environ.get("GITHUB_RUN_ID", ""),
                    _script(), str(event), body])


def pending() -> int:
    """まだ書いていない行数 (テスト・確認用)。"""
    return len(_buffer)


def _take() -> list:
    global _buffer, _dropped
    rows, _buffer = _buffer, []
    if _dropped:
        rows.append([datetime.now(JST).strftime(TS_FMT),
                     os.environ.get("GITHUB_WORKFLOW", ""),
                     os.environ.get("GITHUB_RUN_ID", ""), _script(),
                     "truncated", json.dumps({"省略した行": _dropped})])
        _dropped = 0
    return rows


def _al():
    try:
        from scripts.job_application_sync.fetchers import account_loader as al
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.job_application_sync.fetchers import account_loader as al
    return al


def _open_book():
    # 書き先は集約シート (応募キューのシート。SA が書き込める)。顧客管理シート
    # (JAS_SHEET_ID) は「アカウント情報」の ID/PW を持ち、SA は読むだけなので使わない
    # (2026-10-09 本番初回で WorksheetNotFound = タブを作れなかった)。
    # PRIVATE_LOG_SHEET_ID があればそちらを優先する。
    sid = (os.environ.get("PRIVATE_LOG_SHEET_ID", "")
           or os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", ""))
    if not sid:
        raise RuntimeError("非公開ログのシートID 未設定")
    al = _al()
    gc = al.get_sheets_client()
    return al, al.sheet_retry(gc.open_by_key, sid)


def _worksheet(al, sh, title: str, cols: int, create: bool = True):
    try:
        return al.sheet_retry(sh.worksheet, title), False
    except al.gspread.WorksheetNotFound:
        if not create:
            return None, False
    try:
        return al.sheet_retry(sh.add_worksheet, title=title, rows=1,
                              cols=max(cols, 1)), True
    except al.gspread.exceptions.APIError:
        # 別の実行が同時に作った場合
        return al.sheet_retry(sh.worksheet, title), False


def _prune(al, ws, now: Optional[datetime] = None) -> int:
    """30日より古い行を消す。先頭行が31日より古いときだけ動く (=1日1回程度)。"""
    now = now or datetime.now(JST)
    first = al.sheet_retry(ws.acell, "A2").value or ""
    try:
        t0 = datetime.strptime(first, TS_FMT).replace(tzinfo=JST)
    except ValueError:
        return 0
    if t0 >= now - timedelta(days=RETENTION_DAYS + 1):
        return 0
    cutoff = now - timedelta(days=RETENTION_DAYS)
    col = al.sheet_retry(ws.col_values, 1)[1:]
    n = 0
    for v in col:
        try:
            if datetime.strptime(v, TS_FMT).replace(tzinfo=JST) >= cutoff:
                break
        except ValueError:
            pass
        n += 1
    if n and n < len(col):      # 全行は消さない (直前に追記した行が必ず残る)
        al.sheet_retry(ws.delete_rows, 2, 1 + n)
        return n
    return 0


def _fallback(rows: list, why: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        public(f"[private_log] 非公開ログに書けませんでした ({why})。"
               f"{len(rows)}行は破棄します")
        return
    try:
        LOCAL_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        with LOCAL_FALLBACK.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(dict(zip(HEADER, r)), ensure_ascii=False) + "\n")
        public(f"[private_log] 非公開ログに書けませんでした ({why})。"
               f"{len(rows)}行をローカルの {LOCAL_FALLBACK.name} に残しました")
    except OSError as e:
        public(f"[private_log] 非公開ログに書けませんでした ({why})。"
               f"{len(rows)}行は破棄します ({type(e).__name__})")


def flush() -> int:
    """溜めた行を非公開シートへ1回の append で書く。書けた行数を返す。

    どんな失敗でも例外を上げない (ログのためにジョブを落とさない)。
    """
    rows = _take()
    if not rows:
        return 0
    try:
        al, sh = _open_book()
        ws, created = _worksheet(al, sh, LOG_TAB, len(HEADER))
        al.sheet_retry(ws.append_rows, ([HEADER] if created else []) + rows,
                       value_input_option="RAW")
    except Exception as e:  # noqa: BLE001  例外文はシートIDを含みうるので型名だけ
        _fallback(rows, type(e).__name__)
        return 0
    try:
        _prune(al, ws)
    except Exception as e:  # noqa: BLE001
        public(f"[private_log] 古い行の削除に失敗 ({type(e).__name__})。次回に再試行")
    return len(rows)


def replace_list(title: str, rows: list, create: bool = True) -> bool:
    """要対応の一覧を同じスプレッドシートのタブへ全置換で書く。

    rows が空でも既存のタブは「該当なし」で上書きする (前回分が残って
    「まだ要対応」に見えないように)。create=False ならタブが無いときは作らない。
    書けたら True。失敗しても例外は上げない (公開ログには件数だけ)。
    """
    title = (title or "要対応")[:90]
    cols: list = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    stamp = datetime.now(JST).strftime(TS_FMT)
    if rows:
        values = [["更新日時(JST)"] + cols] + [
            [stamp] + [str(r.get(c, "") if r.get(c) is not None else "") for c in cols]
            for r in rows]
    else:
        values = [["更新日時(JST)", "状態"], [stamp, "該当なし"]]
    try:
        al, sh = _open_book()
        ws, _ = _worksheet(al, sh, title, len(values[0]), create=create)
        if ws is None:
            return False
        al.sheet_retry(ws.clear)
        # 書く範囲がグリッドを超えないよう、行数・列数を合わせる (余った古い行も消える)
        al.sheet_retry(ws.resize, rows=len(values), cols=len(values[0]))
        al.sheet_retry(ws.update, values, "A1", value_input_option="RAW")
        return True
    except Exception as e:  # noqa: BLE001
        public(f"[private_log] 一覧タブに書けませんでした ({type(e).__name__})。"
               f"{len(rows)}行")
        return False


atexit.register(flush)
