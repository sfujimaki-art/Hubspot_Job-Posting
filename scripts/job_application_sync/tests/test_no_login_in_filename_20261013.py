"""ログに出るファイル名にログインIDの断片を入れない (2026-10-13)。

airwork_import のログファイル名は、ログインIDの @ や . を _ にしただけのものを
入れており、公開の Actions ログに「ログ: …/airwork_import_<ID>_dry_….json」と出ていた。
AW の取得ファイル (zip・応募CSV) も同じ作りで、例外文や saved: に名前が出る。
"""
from __future__ import annotations

import re

from scripts.job_application_sync import airwork_import as awi
from scripts.job_application_sync import private_log as plog
from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf
from scripts.job_application_sync.tests.test_aw_end_missing_20261013 import (
    FakeHubSpot, make_xlsx)

LOGIN = "AAA111"     # make_xlsx が書く client_code
MAIL_LOGIN = "saiyo.user@example.com"


def _fragments(login: str) -> set:
    """ID を記号で割った断片のうち、3文字以上のもの + _ 置換版。"""
    parts = {p for p in re.split(r"[^0-9A-Za-z]+", login) if len(p) >= 3}
    return parts | {re.sub(r"[^0-9A-Za-z_-]", "_", login), login}


def test_ファイル名用の識別子にIDの断片が入らない():
    for login in (LOGIN, MAIL_LOGIN):
        tag = plog.file_tag(login)
        assert re.fullmatch(r"[0-9a-f]{8}", tag)
        assert not any(f.lower() in tag for f in _fragments(login))
        assert awf.login_file_tag(login) == tag     # 2か所の値が揃っている
    assert plog.file_tag("") == awf.login_file_tag("") == "unknown"
    assert plog.file_tag(MAIL_LOGIN) != plog.file_tag(LOGIN)


def test_airwork_importのログファイル名と出力にIDが出ない(monkeypatch, tmp_path, capsys):
    h = FakeHubSpot([])
    monkeypatch.setattr(awi.requests, "post", h.post)
    monkeypatch.setattr(awi.time, "sleep", lambda *_: None)
    monkeypatch.setattr(awi, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "dummy-not-used")
    details = []
    monkeypatch.setattr(awi.plog, "detail", lambda ev, **f: details.append((ev, f)))
    x = make_xlsx(tmp_path / "input.xlsx", [("J1", "02")])

    awi.run(x, LOGIN, dry_run=True)

    out = capsys.readouterr().out
    files = [p.name for p in (tmp_path / "logs").iterdir()]
    assert len(files) == 1
    assert files[0].startswith(f"airwork_import_{plog.file_tag(LOGIN)}_dry_")
    assert "ログ:" in out and files[0] in out
    for frag in _fragments(LOGIN):
        assert frag not in files[0]
        assert frag not in out
    # どの ID のログかは非公開ログで引ける
    assert ("airwork_import_log", {"path": str(tmp_path / "logs" / files[0]),
                                   "login_id": LOGIN}) in details


def test_取得ファイル名をIDから作るコードが残っていない():
    """zip・応募CSV の保存名は login_file_tag を通す (ソースで確認)。"""
    from pathlib import Path
    root = Path(awf.__file__).resolve().parent
    src = (root / "aw_csv_fetcher.py").read_text(encoding="utf-8")
    src2 = (root / "aw_applicant_fetcher.py").read_text(encoding="utf-8")
    assert 'f"aw_{login_file_tag(login_id)}_{ts}.zip"' in src
    assert 'f"aw_applicants_{awf.login_file_tag(login_id)}_{ts}.csv"' in src2
    assert "sanitize_login_id" not in src2
    assert "safe_id" not in src
