# -*- coding: utf-8 -*-
"""公開ログに顧客情報を出さず、詳しいログは非公開シートへ (2026-10-09)。

本番リポジトリは public で、Actions のログと成果物は誰でも読める。
会社名・人名・取引ID・取引先コード・取引名・ログインID・メール・
サービスアカウントのメール・バケット名・シートIDは公開ログに出さず、
private_log.detail() で非公開シート「実行ログ」へ送る。
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.job_application_sync import private_log as plog

PKG = Path(__file__).resolve().parents[1]          # scripts/job_application_sync
SCRIPTS = PKG.parent                               # scripts/


# ---------------------------------------------------------------- 伏字
def test_mask_name_先頭2文字だけ残す():
    assert plog.mask_name("株式会社アイデム") == "株式***"
    assert plog.mask_name("アイデム 山田案件") == "アイ***"
    assert plog.mask_name("A") == "A***"
    assert plog.mask_name("") == "" and plog.mask_name(None) == ""


def test_mask_id_先頭2と末尾2だけ残す():
    assert plog.mask_id("20451234567") == "20***67"
    assert plog.mask_id("RL000123") == "RL***23"
    assert plog.mask_id("sa-name@proj.iam.gserviceaccount.com") == "sa***om"
    assert plog.mask_id("abc") == "a***", "短い値は先頭1文字だけ"
    assert plog.mask_id("") == "" and plog.mask_id(None) == ""


def test_redact_はメールと指定の値を伏せる():
    out = plog.redact("login=user01 連絡先 taro@example.co.jp 理由=失敗", ["user01"])
    assert "user01" not in out and "taro@example.co.jp" not in out
    assert "us***01" in out and "理由=失敗" in out


# ---------------------------------------------------------------- detail
def test_detail_は標準出力に何も出さない(capsys):
    plog.detail("deal_written", company="株式会社アイデム", deal_id="20451234567",
                code="RL000123", email="taro@example.co.jp")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert plog.pending() == 1
    row = plog._take()[0]
    assert row[4] == "deal_written"
    assert json.loads(row[5]) == {"company": "株式会社アイデム", "deal_id": "20451234567",
                                  "code": "RL000123", "email": "taro@example.co.jp"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", row[0]), "日時は JST の文字列"


def test_detail_は上限を超えた分を件数だけ残す(monkeypatch):
    monkeypatch.setattr(plog, "MAX_ROWS_PER_RUN", 3)
    for i in range(5):
        plog.detail("x", i=i)
    rows = plog._take()
    assert len(rows) == 4
    assert rows[-1][4] == "truncated" and json.loads(rows[-1][5]) == {"省略した行": 2}


def test_detail_は長すぎる内容を切る():
    plog.detail("big", body="あ" * 20000)
    row = plog._take()[0]
    assert len(row[5]) <= plog.MAX_CELL_CHARS + 10 and row[5].endswith("…(省略)")


# ---------------------------------------------------------------- flush 失敗時
def test_flush_資格情報が無くても例外を上げずCIでは捨てる(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(plog, "LOCAL_FALLBACK", tmp_path / "unsent.jsonl")
    plog.detail("e1", company="株式会社アイデム")
    plog.detail("e2", deal_id="20451234567")
    assert plog.flush() == 0
    out = capsys.readouterr().out
    assert "2行は破棄" in out
    assert "アイデム" not in out and "20451234567" not in out, "警告は件数だけ"
    assert not (tmp_path / "unsent.jsonl").exists(), "CIではキャッシュ対象に溜めない"
    assert plog.pending() == 0


def test_flush_ローカル実行では手元のファイルに残す(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    dest = tmp_path / "unsent.jsonl"
    monkeypatch.setattr(plog, "LOCAL_FALLBACK", dest)
    plog.detail("e1", company="株式会社アイデム")
    assert plog.flush() == 0
    assert "株式会社アイデム" not in capsys.readouterr().out
    rec = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    assert rec["イベント"] == "e1" and "株式会社アイデム" in rec["内容"]


def test_flush_認証エラーでも例外を上げない(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("JAS_SHEET_ID", "1AbCdEfGhIjKlMnOpQrStUvWxYz")
    monkeypatch.setenv("SHEETS_AUTH_MODE", "sa")
    monkeypatch.delenv("GOOGLE_SA_JSON", raising=False)
    plog.detail("e1", x=1)
    assert plog.flush() == 0
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "1AbCdEf" not in out, "例外文 (シートIDを含みうる) は出さない"


def test_flush_空なら何もしない(monkeypatch):
    def boom():
        raise AssertionError("呼ばれてはいけない")
    monkeypatch.setattr(plog, "_open_book", boom)
    assert plog.flush() == 0


# ---------------------------------------------------------------- flush 成功時 (偽シート)
class _Cell:
    def __init__(self, v):
        self.value = v


class FakeWS:
    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.calls = []

    def append_rows(self, rows, value_input_option=None):
        self.calls.append("append_rows")
        self.rows.extend(rows)

    def acell(self, a1):
        return _Cell(self.rows[1][0] if len(self.rows) > 1 else None)

    def col_values(self, n):
        return [r[0] for r in self.rows]

    def delete_rows(self, start, end):
        self.calls.append(("delete_rows", start, end))
        del self.rows[start - 1:end]

    def clear(self):
        self.rows = []

    def resize(self, rows, cols):
        self.calls.append(("resize", rows, cols))

    def update(self, values, rng, value_input_option=None):
        self.rows = [list(r) for r in values]


class FakeBook:
    def __init__(self, tabs=None):
        self.tabs = dict(tabs or {})

    def worksheet(self, title):
        if title not in self.tabs:
            raise _gs().WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS()
        return self.tabs[title]


def _gs():
    import gspread
    return gspread


class FakeAL:
    gspread = None

    @staticmethod
    def sheet_retry(fn, *a, **kw):
        return fn(*a, **kw)


@pytest.fixture
def book(monkeypatch):
    FakeAL.gspread = _gs()
    b = FakeBook()
    monkeypatch.setattr(plog, "_open_book", lambda: (FakeAL, b))
    return b


def _ts(days_ago: float) -> str:
    return (datetime.now(plog.JST) - timedelta(days=days_ago)).strftime(plog.TS_FMT)


def test_flush_タブが無ければ見出し付きで作り1回の追記で書く(book):
    plog.detail("e1", company="株式会社アイデム")
    plog.detail("e2", deal_id="20451234567")
    assert plog.flush() == 2
    ws = book.tabs["実行ログ"]
    assert ws.rows[0] == plog.HEADER
    assert [r[4] for r in ws.rows[1:]] == ["e1", "e2"]
    assert ws.calls == ["append_rows"], "書き込みは1回だけ"


def test_flush_30日より古い行は先頭が31日を超えたときだけ消す(book):
    ws = FakeWS([plog.HEADER, [_ts(40), "", "", "", "old1", "{}"],
                 [_ts(32), "", "", "", "old2", "{}"], [_ts(5), "", "", "", "new", "{}"]])
    book.tabs["実行ログ"] = ws
    plog.detail("now")
    assert plog.flush() == 1
    assert [r[4] for r in ws.rows[1:]] == ["new", "now"]
    assert ("delete_rows", 2, 3) in ws.calls

    # 先頭が 30.5 日前 (31日未満) なら消しに行かない = 1日1回程度
    ws2 = FakeWS([plog.HEADER, [_ts(30.5), "", "", "", "a", "{}"]])
    book.tabs["実行ログ"] = ws2
    plog.detail("now")
    plog.flush()
    assert not any(isinstance(c, tuple) and c[0] == "delete_rows" for c in ws2.calls)


def test_replace_list_は毎回全置換し0件なら既存タブを該当なしにする(book):
    rows = [{"取引ID": "2045", "取引名": "株式会社アイデム"},
            {"取引ID": "2046", "取引名": "B社", "理由": "候補が複数"}]
    assert plog.replace_list("要対応_テスト", rows) is True
    ws = book.tabs["要対応_テスト"]
    assert ws.rows[0] == ["更新日時(JST)", "取引ID", "取引名", "理由"]
    assert ws.rows[1][1:] == ["2045", "株式会社アイデム", ""]
    assert ws.rows[2][1:] == ["2046", "B社", "候補が複数"]

    assert plog.replace_list("要対応_テスト", [], create=False) is True
    assert ws.rows[1][1] == "該当なし" and len(ws.rows) == 2
    assert plog.replace_list("要対応_無いタブ", [], create=False) is False
    assert "要対応_無いタブ" not in book.tabs, "0件のときは新しいタブを作らない"


# ---------------------------------------------------------------- 公開ログの監視
# print() / plog.public() の f-string に、伏せずに埋め込んではいけない値の名前。
SENSITIVE = {"company", "company_name", "dealname", "deal_id", "login_id", "email",
             "client_email", "取引名", "会社名", "取引先コード", "owner"}
SAFE_CALLS = {"mask_name", "mask_id", "redact", "_mask", "mask_secret", "scrub_secrets",
              "_scrub", "len", "type", "bool", "_public_line", "public_summary"}
# (ファイル名, 行の中身の一部): 既に伏せた値だと確認済みのもの
ALLOWED = {
    # process_aw_account が mask_secret 済みの login_id を返す
    ("applicant_sync.py", "{res.get('login_id')}"),
}


def _names(node) -> set:
    out = set()
    for m in ast.walk(node):
        if isinstance(m, ast.Call):
            f = m.func
            fn = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if fn in SAFE_CALLS:
                return set()                   # 伏字・件数に通しているなら可
            if fn == "get" and m.args and isinstance(m.args[0], ast.Constant):
                out.add(str(m.args[0].value))
        elif isinstance(m, ast.Name):
            out.add(m.id)
        elif isinstance(m, ast.Attribute):
            out.add(m.attr)
        elif isinstance(m, ast.Subscript) and isinstance(m.slice, ast.Constant):
            out.add(str(m.slice.value))
    return out


def _violations(path: Path) -> list:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    bad = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        fn = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if fn not in ("print", "public"):
            continue
        for a in n.args:
            for fv in ast.walk(a):
                if not isinstance(fv, ast.FormattedValue):
                    continue
                hit = _names(fv.value) & SENSITIVE
                if not hit:
                    continue
                seg = ast.get_source_segment(src, fv) or ""
                if any(path.name == f_ and s in seg for f_, s in ALLOWED):
                    continue
                bad.append(f"{path.relative_to(SCRIPTS)}:{fv.lineno}: {seg} "
                           f"({', '.join(sorted(hit))}) | {lines[n.lineno - 1].strip()[:80]}")
    return bad


def test_公開ログに伏せていない顧客情報を埋め込まない():
    files = [p for p in SCRIPTS.rglob("*.py") if "tests" not in p.parts]
    assert len(files) > 30
    bad = [v for p in files for v in _violations(p)]
    assert not bad, "公開ログ (print) に伏せずに出している値:\n" + "\n".join(bad)


def test_監視は伏せていない埋め込みを実際に見つける(tmp_path):
    p = SCRIPTS / "job_application_sync" / "_tmp_guard_probe.py"
    try:
        p.write_text("def f(company, deal, plog):\n"
                     "    print(f'{company[:18]}')\n"
                     "    print(f\"{deal['deal_id']}\")\n"
                     "    print(f'{plog.mask_name(company)} {len(company)}')\n",
                     encoding="utf-8")
        got = _violations(p)
    finally:
        p.unlink()
    assert len(got) == 2, got
    assert ":2:" in got[0] and ":3:" in got[1]
