# -*- coding: utf-8 -*-
"""Slack の通知に添える「対象一覧」を、非公開の集約シートのタブに書く (2026-10-23)。

★なぜ要るか
  Slack の通知は件数だけ (「AW巡回 464社処理: login_fail=37」) か、上位数件だけで、
  どの社が何で失敗したかを見るには PC のログを開くしかなかった。
  ユーザー指示 (2026-10-23): 通知にある対象案件 (特にエラー) をレポートにして Slack に出す。
  Webhook ではファイルを添付できないので、スプレッドシートに書いて URL を通知に付ける
  (ユーザー決定)。書き先は非公開ログと同じ集約シート (private_log._open_book)。

書き方
  - タブは通知の種類ごとに 1 つ。列は固定 (COLS)。新しい行を上に足し、
    KEEP_DAYS 日より古い行と MAX_ROWS を超えた行は消す。
  - 会社名は出す。ログイン ID は伏せる (呼び出し側で mask してから渡す)。パスワードは書かない。
  - 書けなかったときは "" を返し、通知はそのまま送る (通知を止めない)。
  - URL は Slack にだけ載せる。公開ログ (Actions) には出さない。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from scripts.job_application_sync import private_log as plog

COLS = ["日時(JST)", "通知", "結果", "会社名", "対象", "内容", "やること"]
KEEP_DAYS = 14
MAX_ROWS = 5000

TAB_AW_CRAWL = "報告_エアワーク巡回"
TAB_APPLICANTS = "報告_応募取り込み"


def _sheet_id() -> str:
    return (os.environ.get("PRIVATE_LOG_SHEET_ID", "")
            or os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", ""))


def tab_url(sid: str, gid) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gid}"


def row(notice: str, result: str, company: str = "", target: str = "",
        detail: str = "", action: str = "") -> dict:
    """1 行。列名は COLS と同じ (日時は publish が入れる)。"""
    return {"通知": notice, "結果": result, "会社名": company or "",
            "対象": target or "", "内容": detail or "", "やること": action or ""}


def _merge(new: list, old: list, now: datetime, keep_days: int = KEEP_DAYS,
           max_rows: int = MAX_ROWS) -> list:
    """新しい行を上に、古い行 (keep_days 日より前・見出しが違う表) を消して返す (見出し込み)。"""
    stamp = now.strftime(plog.TS_FMT)
    values = [COLS] + [[stamp] + [str(r.get(c, "") or "") for c in COLS[1:]] for r in new]
    if old and old[0][:len(COLS)] == COLS:
        cutoff = (now - timedelta(days=keep_days)).strftime(plog.TS_FMT)
        for v in old[1:]:
            v = (list(v) + [""] * len(COLS))[:len(COLS)]
            if v[0] and v[0] >= cutoff:
                values.append(v)
    return values[:max_rows + 1]


def publish(tab: str, rows: list, now: Optional[datetime] = None) -> str:
    """rows を tab の上に足し、そのタブの URL を返す。rows が空・書けないときは ""。"""
    if not rows:
        return ""
    sid = _sheet_id()
    if not sid:
        return ""
    now = now or datetime.now(plog.JST)
    try:
        al, sh = plog._open_book()
        ws, _ = plog._worksheet(al, sh, tab, len(COLS))
        if ws is None:
            return ""
        old = al.sheet_retry(ws.get_all_values)
        values = _merge(rows, old, now)
        al.sheet_retry(ws.clear)
        al.sheet_retry(ws.resize, rows=len(values), cols=len(COLS))
        al.sheet_retry(ws.update, values, "A1", value_input_option="RAW")
        plog.public(f"[report] シートのタブ「{tab}」に {len(rows)}行を書きました")
        return tab_url(sid, ws.id)
    except Exception as e:  # noqa: BLE001  例外文はシートIDを含みうるので型名だけ
        plog.public(f"[report] 対象一覧を書けませんでした ({type(e).__name__})。{len(rows)}行")
        return ""


def link_line(url: str, rows: list) -> str:
    """通知の末尾に付ける 1 行。URL が無ければ空。"""
    if not url:
        return ""
    ng = sum(1 for r in rows if r.get("結果") == "NG")
    return (f"\n▶ 対象一覧 {len(rows):,}件"
            + (f" (うちエラー {ng:,}件)" if ng else "") + f": {url}")
