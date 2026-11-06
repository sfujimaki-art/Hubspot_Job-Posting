# -*- coding: utf-8 -*-
"""夜の巡回: AirWork の応募一覧と HubSpot を突き合わせる (2026-11-05)。"""
from datetime import date, datetime
from types import SimpleNamespace as NS

from scripts.job_application_sync import aw_applicant_check as ck
from scripts.job_application_sync import applicant_sync as asy


def _row(d, email="a@x.jp", phone="", job="J1", name="山田"):
    return NS(apply_date=d, email=email, phone=phone, media_job_id=job, name=name, media="AirWork")


class _Cli:
    def __init__(self, have):
        self.have, self.calls = have, []

    def find_existing_appointment(self, media, job, phone, email, d):
        self.calls.append((media, job, phone, email, d))
        return "1" if (email, d) in self.have else None


def test_window_is_until_yesterday():
    assert ck.window(date(2026, 11, 5), 3) == ("2026-11-02", "2026-11-04")


def test_find_missing_only_window_and_contactable():
    rows = [_row("2026-11-01"), _row("2026-11-03"), _row("2026-11-04", email="b@x.jp"),
            _row("2026-11-05"), _row("2026-11-03", email="", phone="")]
    cli = _Cli({("a@x.jp", "2026-11-03")})
    checked, missing = ck.find_missing(rows, cli, "2026-11-02", "2026-11-04")
    assert checked == 2
    assert [r.email for r in missing] == ["b@x.jp"]


def test_record_and_report_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ck, "STATE_PATH", tmp_path / "s.json")
    sent = []
    monkeypatch.setattr(asy, "slack_notify", lambda message, dry_run=False, rows=None:
                        sent.append((message, rows)) or True)
    now = datetime(2026, 11, 5, 9)
    assert ck.record("L1", "会社A", [_row("2026-11-04")], now) == 1
    assert ck.record("L1", "会社A", [_row("2026-11-04")], now) == 0   # 同じ応募は積まない
    assert ck.report(now=now) == 1
    assert "1件 (1社)" in sent[0][0] and len(sent[0][1]) == 1
    assert ck.report(now=now) == 0                                     # 2 回目は送らない
    assert len(sent) == 1


def test_failed_send_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ck, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(asy, "slack_notify", lambda message, dry_run=False, rows=None: False)
    now = datetime(2026, 11, 5, 9)
    ck.record("L1", "会社A", [_row("2026-11-04")], now)
    assert ck.report(now=now) == 1
    assert ck.report(now=now) == 1    # 届かなかった分は次の回にもう一度


def test_zero_applicants_is_not_error(tmp_path, monkeypatch):
    """応募 0 件の社は DL ボタンが無い。エラーにせず 0 件として返す (2026-11-06)。"""
    from scripts.job_application_sync.fetchers import aw_applicant_fetcher as af

    async def _fetch(*a, **k):
        raise af.NoApplicantsError("0件")
    monkeypatch.setattr(af, "fetch_aw_applicants", _fetch)
    monkeypatch.setattr(ck, "STATE_PATH", tmp_path / "s.json")
    out = ck.check_company("id", "pw", "社", tmp_path, "tok")
    assert out == {"checked": 0, "missing": 0, "new": 0, "empty": True}
    assert issubclass(af.NoApplicantsError, RuntimeError)  # 取り込み側の受け方は変わらない
    assert not (tmp_path / "s.json").exists()
