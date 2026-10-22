# -*- coding: utf-8 -*-
"""Slack の通知に、対象一覧 (集約シートのタブ) の URL を付ける (2026-10-23)。

ユーザー指示: 通知にある対象案件 (特にエラー) をレポートにして Slack に出す。
Webhook ではファイルを添付できないので、シートに書いて URL を通知に載せる。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from scripts.job_application_sync import private_log as plog
from scripts.job_application_sync import report_sheet as rs
from scripts.job_application_sync.fetchers import aw_orchestrator as orch

SID = "SHEET123"


class _WS:
    def __init__(self, rows=None, gid=777):
        self.rows = [list(r) for r in (rows or [])]
        self.id = gid

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def clear(self):
        self.rows = []

    def resize(self, rows, cols):
        pass

    def update(self, values, rng, value_input_option=None):
        self.rows = [list(r) for r in values]


class _AL:
    @staticmethod
    def sheet_retry(fn, *a, **k):
        return fn(*a, **k)


@pytest.fixture
def tab(monkeypatch):
    ws = _WS()
    monkeypatch.setenv("JAS_APPLICANT_QUEUE_SHEET_ID", SID)
    monkeypatch.setattr(plog, "_open_book", lambda: (_AL, object()))
    monkeypatch.setattr(plog, "_worksheet", lambda al, sh, title, cols, create=True: (ws, False))
    return ws


NOW = datetime(2026, 10, 23, 9, 0, 0, tzinfo=plog.JST)


def test_新しい行を上に足し古い行と見出しの違う表は消す(tab):
    old_ok = (NOW - timedelta(days=3)).strftime(plog.TS_FMT)
    old_ng = (NOW - timedelta(days=15)).strftime(plog.TS_FMT)
    tab.rows = [rs.COLS,
                [old_ok, "求人の巡回(full)", "NG", "A社", "ログインID ab…", "前回", "x"],
                [old_ng, "求人の巡回(full)", "NG", "B社", "", "古い", "x"]]
    url = rs.publish(rs.TAB_AW_CRAWL, [rs.row("至急の取得", "NG", "C社", "ログインID cd…",
                                              "ID か PW が違います", "直す")], now=NOW)
    assert url == f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=777"
    assert tab.rows[0] == rs.COLS
    assert tab.rows[1] == ["2026-10-23 09:00:00", "至急の取得", "NG", "C社",
                           "ログインID cd…", "ID か PW が違います", "直す"]
    assert [r[3] for r in tab.rows[1:]] == ["C社", "A社"], "15日前の B社 は消える"

    tab.rows = [["別の見出し"], ["x"]]
    rs.publish(rs.TAB_AW_CRAWL, [rs.row("n", "NG", "D社")], now=NOW)
    assert [r[3] for r in tab.rows[1:]] == ["D社"], "見出しが違う古い表は引き継がない"


def test_行が無いときとシートが書けないときはURLを返さない(tab, monkeypatch):
    assert rs.publish(rs.TAB_AW_CRAWL, []) == ""
    assert tab.rows == [], "0行のときはシートに触らない"

    def boom():
        raise RuntimeError(f"open failed {SID}")
    monkeypatch.setattr(plog, "_open_book", boom)
    assert rs.publish(rs.TAB_AW_CRAWL, [rs.row("n", "NG", "D社")]) == ""


def test_通知の末尾の行はエラー件数とURL():
    rows = [rs.row("n", "NG", "A"), rs.row("n", "要確認", "B"), rs.row("n", "NG", "C")]
    assert rs.link_line("https://x", rows) == "\n▶ 対象一覧 3件 (うちエラー 2件): https://x"
    assert rs.link_line("", rows) == ""


def test_巡回の対象一覧はエラーを先にしIDは伏せパスワードは書かない():
    results = [
        {"login_id": "okuser01", "company": "画像の社", "status": "ok",
         "result": {"ends_skipped_cap": 4, "creates_ng": 0},
         "images": {"targets": 3, "page_failed": 2, "write_failed": 0,
                    "page_failed_reasons": {"gallery_layout_unrecognized": 2}}},
        {"login_id": "rpo_secret_id", "company": "規約の社", "status": "error",
         "error": "ログイン後 dashboards に遷移せず: 利用規約の再同意が必要です "
                  "(login_id=rpo_secret_id, last_url=https://ats.rct.airwork.net/usage_re_agreement)"},
        {"login_id": "zz_other", "company": "不明の社", "status": "error",
         "error": "boom for zz_other taro@example.co.jp"},
        {"login_id": "hs", "company": "書けない社", "status": "ok",
         "result": {"creates_ng": 1, "updates_ng": 2, "ends_ng": 0}},
        {"login_id": "fine", "company": "問題ない社", "status": "ok",
         "result": {"creates_ng": 0}, "images": {"targets": 1, "page_failed": 0}},
    ]
    rows = orch._report_rows(results, "求人の巡回(full)")
    assert [(r["結果"], r["会社名"]) for r in rows] == [
        ("NG", "規約の社"), ("NG", "不明の社"), ("NG", "書けない社"),
        ("要確認", "画像の社"), ("画像取れず", "画像の社")]
    assert "利用規約の再同意" in rows[0]["内容"] and "同意" in rows[0]["やること"]
    assert rows[0]["対象"] == "ログインID rp…"
    joined = repr(rows)
    assert "rpo_secret_id" not in joined and "zz_other" not in joined
    assert "taro@example.co.jp" not in joined
    assert "作成 1 / 更新 2" in rows[2]["内容"]
    assert "4件" in rows[3]["内容"] and "20%" in rows[3]["内容"]
    assert rows[4]["内容"] == "公開ページの画像の並び方を読み取れない: 2件"


def test_巡回の通知に一覧のURLが付き一覧が書けなくても通知は送る(monkeypatch):
    sent = []
    monkeypatch.setattr(orch, "_slack_notify", sent.append)
    monkeypatch.setattr(orch.report_sheet, "publish", lambda tab, rows: "https://sheet#gid=1")
    rows = [rs.row("n", "NG", "A")]
    orch._notify_with_report("🔄 AW巡回(full) 1社処理: login_fail=1", rows)
    assert sent[-1].endswith("▶ 対象一覧 1件 (うちエラー 1件): https://sheet#gid=1")

    monkeypatch.setattr(orch.report_sheet, "publish", lambda tab, rows: "")
    orch._notify_with_report("🔄 AW巡回(full) 1社処理: login_fail=1", rows)
    assert sent[-1] == "🔄 AW巡回(full) 1社処理: login_fail=1"

    called = []
    monkeypatch.setattr(orch.report_sheet, "publish", lambda tab, rows: called.append(1) or "u")
    orch._notify_with_report("m", rows, dry_run=True)
    assert called == [] and sent[-1] == "m", "dry-run ではシートに書かない"


def test_課金の対象一覧はエラーの社だけ():
    rows = orch._billing_report_rows(
        [{"login_id": "abcdef", "company": "A社", "status": "error",
          "error": "ログイン後 dashboards に遷移せず: ID か PW が違います "
                   "(login_id=abcdef, last_url=https://connect.airregi.jp/login?login_failed=true)"},
         {"login_id": "x1", "company": "B社", "status": "queued"}], "課金の取得(billing-create)")
    assert len(rows) == 1 and rows[0]["会社名"] == "A社"
    assert rows[0]["内容"].startswith("ID か PW が違います")
    assert "abcdef" not in repr(rows)


def test_応募の通知は対象一覧を書いてからURLを付けて送る(monkeypatch):
    from scripts.job_application_sync import applicant_sync as aps
    posted = []

    class _R:
        status_code = 200
    monkeypatch.setenv("SLACK_APPLICANT_ALERT_WEBHOOK", "https://hooks.example/x")
    monkeypatch.setattr(aps.requests, "post", lambda url, json, timeout: posted.append(json) or _R())
    tabs = []
    monkeypatch.setattr(aps.report_sheet, "publish",
                        lambda tab, rows: tabs.append((tab, len(rows))) or "https://s#gid=9")
    assert aps.slack_notify("⚠️ 応募連携: テスト", rows=[rs.row("n", "NG", "A社")]) is True
    assert tabs == [(rs.TAB_APPLICANTS, 1)]
    assert posted[-1]["text"].endswith("▶ 対象一覧 1件 (うちエラー 1件): https://s#gid=9")

    tabs.clear()
    aps.slack_notify("⚠️ 応募連携: テスト2")
    assert tabs == [] and "対象一覧" not in posted[-1]["text"], "一覧が無い通知は今までどおり"
