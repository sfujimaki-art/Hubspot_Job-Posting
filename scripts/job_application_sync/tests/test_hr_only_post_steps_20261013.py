"""HR だけの回でも顧客シート転記と relink を止めない (2026-10-13)。

以前は --media HR のとき AW の手前で return しており、後ろの顧客シート転記と
relink まで飛ばしていた。AW を専用PC (--media AW) へ移し Actions を --media HR に
すると、この2つがどこでも動かなくなる。後段は HR を含む回 (HR/BOTH) だけで走らせ、
AW だけの回では走らせない (二重実行を避ける)。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.job_application_sync import applicant_queue as aq
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync import customer_sheet_sync as css

_ORIG_LOCK = S.Lock


class _NoResolver:
    def build(self):
        return self

    def resolve(self, it):
        return None


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = SimpleNamespace(sheet=0, relink=[], aw=0)
    ledger = S.Ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(S, "Lock", lambda: _ORIG_LOCK(tmp_path / "lock"))
    monkeypatch.setattr(S, "Ledger", lambda merge_from="": ledger)
    monkeypatch.setattr(aq, "read_new_items_from_sheet1", lambda **k: [])
    monkeypatch.setattr(aq, "AccountResolver", lambda: _NoResolver())
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path / "sess")
    monkeypatch.setattr(S, "slack_notify", lambda message="", dry_run=False, rows=None: None)

    def fake_sheet(dry_run=True):
        state.sheet += 1
        return {"sheets": 0, "ok": 0, "fail": 0, "wrote": 0, "errors": []}

    def fake_relink(dry_run=False, limit=1000, target_job_ids=None):
        state.relink.append(target_job_ids)
        return {"relinked": 0}

    def fake_aw(*a, **k):
        state.aw += 1
        return {"ok": True}

    monkeypatch.setattr(css, "run_all", fake_sheet)
    monkeypatch.setattr(S, "relink", fake_relink)
    monkeypatch.setattr(S, "process_aw_account", fake_aw)
    return state


def _run(media, dry_run=False):
    return S.run(dry_run=dry_run, source="sheet1", media_filter=media,
                 hr_cutoff_iso="2026-10-01")


@pytest.mark.parametrize("media, expected", [("HR", 1), ("BOTH", 1), ("AW", 0)])
def test_後段はHRを含む回だけ1回走る(harness, media, expected):
    s = _run(media)
    assert harness.sheet == expected
    assert harness.relink == [None] * expected     # 定期 relink (ID 指定なし)
    assert ("relinked" in s) is bool(expected)


def test_HRだけの回は台帳を保存し完了ログまで進む(harness, capsys):
    s = _run("HR")
    out = capsys.readouterr().out
    assert out.count("[applicant_sync-done]") == 1
    assert s["accounts"] == 0 and harness.aw == 0     # AW は触らない


@pytest.mark.parametrize("media", ["HR", "BOTH", "AW"])
def test_dry_runでは後段を走らせない(harness, media):
    _run(media, dry_run=True)
    assert harness.sheet == 0 and harness.relink == []
