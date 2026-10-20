"""シート1の見出しが壊れても応募を読めること・黙らないこと (2026-10-19)。

10/17 19:10〜10/19 8:45、シート1の1行目 (見出し) が通知メールで上書きされた。
見出しの「媒体」が消え、中身で探した媒体の列が「媒体リンク」(URL の列) になり、
応募を約37時間 1件も読めなかった。エラーにならず 0件で終わったので誰も気づけなかった。
"""
from __future__ import annotations

from scripts.job_application_sync import alert_text
from scripts.job_application_sync import applicant_queue as aq
from scripts.job_application_sync import applicant_sync as sync

GOOD_HEADER = ["", "媒体", "顧客", "応募日", "チェック", "", "担当CS",
               "応募管理シート", "媒体リンク"]
AW_FROM = "Airワーク 採用管理 <no-reply@rct.airwork.net>"
HR_FROM = "admin@hr-hacker.com"
AW_SUBJ = "【要確認】応募がありました【Airワーク 採用管理】"
HR_SUBJ = "採用ページから応募がありました"


def _row(subj, media, date, company, marker="", link=""):
    return [subj, media, f"担当 <rpo.medica+{company}@gmail.com>", date, "", marker,
            "担当者", f"{company}株式会社（担当）", link]


def _rows():
    rows = []
    # 古い行: 処理済み。「媒体リンク」列に URL が入っている (見出しが壊れたときの取り違えの元)
    for i in range(30):
        rows.append(_row(AW_SUBJ, AW_FROM, "2026/9/1", f"old{i}", "キュー済",
                         "https://ats.rct.airwork.net/interaction/1"))
        rows.append(_row(HR_SUBJ, HR_FROM, "2026/9/1", f"oldh{i}", "キュー済",
                         "https://hr-hacker.com/applicants/1"))
        # 他の媒体の通知 (自動送信の語を含まない差出人) で媒体列の該当率が下がる
        rows.append(_row("応募通知メール[Jobギア採用管理]", "info@job-gear-ats.com",
                         "2026/9/1", f"jg{i}", "キュー済",
                         "https://ats.rct.airwork.net/x"))
    # 期間内の未処理の応募 (媒体リンクは空)
    rows.append(_row(AW_SUBJ, AW_FROM, "2026/10/18", "aaa"))
    rows.append(_row(AW_SUBJ, AW_FROM, "2026/10/18", "bbb"))
    rows.append(_row(HR_SUBJ, HR_FROM, "2026/10/18", "ccc"))
    return rows


class _WS:
    def __init__(self, vals):
        self.vals = vals

    def get(self, _rng):
        return self.vals


class _GC:
    def __init__(self, vals):
        self.vals = vals

    def open_by_key(self, _sid):
        return self

    def worksheet(self, _tab):
        return _WS(self.vals)


def _read(monkeypatch, vals):
    monkeypatch.setattr(aq, "_sheets_client", lambda *a, **k: _GC(vals))
    return aq.read_new_items_from_sheet1("sid", cutoff_iso="2026-10-05",
                                         media_filter="BOTH")


def test_content_fallback_does_not_pick_the_url_column():
    """見出しが無いとき、媒体の列を URL の「媒体リンク」列 (I列) と取り違えない。"""
    rows = _rows()
    broken = ["x"] * len(GOOD_HEADER)
    cols = aq._resolve_s1_columns(broken, rows)
    assert cols["media"] == 1          # B列 (差出人の自動送信アドレス)
    assert cols["from"] == 2
    assert cols["company"] == 7


def test_good_header_reads_and_remembers_columns(monkeypatch):
    items = _read(monkeypatch, [GOOD_HEADER] + _rows())
    assert sorted(i.company for i in items) == ["aaa株式会社", "bbb株式会社", "ccc株式会社"]
    assert aq.LAST_S1_DIAG["column_source"] == "header"
    assert aq.LAST_S1_DIAG["recent_app_rows"] == 3
    assert (aq.LAST_S1_DIAG["recent_aw"], aq.LAST_S1_DIAG["recent_hr"]) == (2, 1)
    assert aq._load_s1_columns() == {"media": 1, "from": 2, "company": 7,
                                     "subject": 0, "date": 3, "marker": 5}


def test_header_overwritten_by_mail_still_reads_all(monkeypatch):
    """10/17〜19 の状態: 1行目が応募メール。覚えていた列で読み、1行目も応募として読む。"""
    good = _read(monkeypatch, [GOOD_HEADER] + _rows())
    row1 = _row(HR_SUBJ, HR_FROM, "2026/10/17", "top")
    items = _read(monkeypatch, [row1] + _rows())
    d = aq.LAST_S1_DIAG
    assert d["column_source"] == "saved"
    assert d["missing_by_name"] == ["media", "from", "company"]
    assert d["header_is_mail"] is True
    # 2行目以降の応募はすべて読め、ID も見出しが正常なときと同じ (処理済みが復活しない)
    assert [(i.row_id, i.sheet_row) for i in items[:-1]] == \
        [(i.row_id, i.sheet_row) for i in good]
    # 1行目の応募は最後に読む
    assert (items[-1].sheet_row, items[-1].company, items[-1].media_type) == \
        (1, "top株式会社", "HRハッカー")
    assert (d["recent_aw"], d["recent_hr"]) == (2, 2)


def test_header_broken_without_memory_uses_content(monkeypatch):
    row1 = _row(AW_SUBJ, AW_FROM, "2026/10/17", "top")
    items = _read(monkeypatch, [row1] + _rows())
    assert aq.LAST_S1_DIAG["column_source"] == "content"
    assert sorted(i.company for i in items) == \
        ["aaa株式会社", "bbb株式会社", "ccc株式会社", "top株式会社"]


def test_health_messages():
    ok = {"column_source": "header", "missing_by_name": [], "header_is_mail": False,
          "recent_app_rows": 50, "recent_aw": 30, "recent_hr": 20}
    assert alert_text.sheet1_health_messages(ok) == []
    kinds = [k for k, _ in alert_text.sheet1_health_messages(
        dict(ok, column_source="saved", missing_by_name=["media"], header_is_mail=True))]
    assert kinds == ["header"]
    assert [k for k, _ in alert_text.sheet1_health_messages(
        dict(ok, recent_app_rows=0, recent_aw=0, recent_hr=0))] == ["zero"]
    # 10/17〜19 の実際の状態: 応募行はあるのに媒体を判別できない
    msgs = alert_text.sheet1_health_messages(
        dict(ok, column_source="content", missing_by_name=["media", "from", "company"],
             recent_app_rows=554, recent_aw=0, recent_hr=0), "2026-10-03")
    assert [k for k, _ in msgs] == ["header", "media"]
    assert "554行" in msgs[1][1] and "2026-10-03 以降" in msgs[1][1]


class _Ledger:
    def __init__(self):
        self.m, self.saved = {}, 0

    def meta(self, kind, key):
        return self.m.get((kind, key), {})

    def set_meta(self, kind, key, **f):
        self.m.setdefault((kind, key), {}).update(f)

    def save(self):
        self.saved += 1


def test_report_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(sync, "slack_notify", lambda message, dry_run=False: sent.append(message))
    monkeypatch.setattr(aq, "LAST_S1_DIAG", {
        "column_source": "content", "missing_by_name": ["media"], "header_is_mail": True,
        "recent_app_rows": 0, "recent_aw": 0, "recent_hr": 0})
    led = _Ledger()
    sync._report_sheet1_health(led, "2026-10-05", dry_run=False)
    sync._report_sheet1_health(led, "2026-10-05", dry_run=False)
    assert len(sent) == 2                      # header と zero を 1 回ずつ
    assert "見出し" in sent[0] and "1件も読めません" in sent[1]
    assert led.saved == 2
