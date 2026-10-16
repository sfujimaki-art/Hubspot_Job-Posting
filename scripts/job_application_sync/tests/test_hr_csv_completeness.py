"""Synthetic counts only; all external actions are replaced with fakes."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.job_application_sync import hr_csv_guard as guard
from scripts.job_application_sync import hr_watcher as watcher
from scripts.job_application_sync.fetchers import hr_csv_fetcher as fetcher


def rows(n, shops=10):
    return [{"media_job_id": str(i), "shop_id": str(i % shops),
             "job_name": "fictional", "original_status": "公開"} for i in range(n)]


@pytest.fixture(autouse=True)
def local_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "LOG_DIR", tmp_path)


def seed(path, n=100):
    watcher.save_snapshot(path, watcher.csv_rows_to_snapshot(rows(n), "fictional.csv"))


def run(path, data, importer, **kwargs):
    return watcher.run(csv_path="fictional.csv", dry_run=False, skip_aw=True,
                       snapshot_dir=path, load_csv_fn=lambda _: data,
                       hrhacker_run_fn=importer, **kwargs)


def forbidden(*args, **kwargs):
    pytest.fail("Rejected exports must not reach downstream processing")


def test_normal_is_imported(tmp_path):
    seed(tmp_path)
    calls = []
    result = run(tmp_path, rows(90), lambda *a, **kw: calls.append(1) or {})
    assert result["completeness"]["incomplete"] is False
    assert result["diff"] == {"new": 0, "removed": 10, "changed": 0}
    assert calls == [1]


def test_missing_then_recovered_preserves_baseline_and_notifies_once(tmp_path, monkeypatch):
    seed(tmp_path)
    original = watcher.load_latest_snapshot(tmp_path)
    messages = []
    monkeypatch.setenv("SCHEDULE", "0 3 * * *")
    for _ in range(2):
        result = run(tmp_path, rows(5), forbidden, slack=True,
                     slack_notify_fn=lambda msg: messages.append(msg) or True,
                     branches_fetcher=forbidden, deal_finder=forbidden,
                     aw_orchestrate_fn=forbidden)
        assert result["skip_reason"] == "incomplete_csv"
        assert "diff" not in result
        assert watcher.load_latest_snapshot(tmp_path) == original
    assert len(messages) == 1
    assert "行数=5" in messages[0] and "比較元=100" in messages[0]
    recovered = run(tmp_path, rows(100), forbidden)
    assert recovered["skip_reason"] == "no_diff"
    assert recovered["diff"] == {"new": 0, "removed": 0, "changed": 0}


def test_no_baseline_passes(tmp_path):
    calls = []
    result = run(tmp_path, rows(5), lambda *a, **kw: calls.append(1) or {})
    assert result["completeness"]["reference"] is None
    assert calls == [1]


def test_shop_loss_caught_even_with_same_rows(tmp_path):
    seed(tmp_path)
    result = run(tmp_path, rows(100, shops=1), forbidden)
    assert result["skip_reason"] == "incomplete_csv"
    assert result["completeness"]["current"] == {"rows": 100, "shops": 1}


def test_threshold_boundary_and_recent_median(tmp_path):
    now = datetime.now(guard.JST)
    for days, n in [(0, 100), (1, 102), (2, 98), (8, 10000)]:
        watcher.save_snapshot(tmp_path, watcher.csv_rows_to_snapshot(rows(n), "fictional.csv"),
                              (now - timedelta(days=days)).strftime("%Y-%m-%d"))
    result = guard.assess(tmp_path, watcher.csv_rows_to_snapshot(rows(80), "fictional.csv"), now)
    assert result["reference"]["rows"] == 100
    assert result["baseline_samples"] == 3
    assert not result["incomplete"]
    assert guard.assess(tmp_path, watcher.csv_rows_to_snapshot(rows(79), "fictional.csv"), now)["incomplete"]


def test_dry_run_does_not_notify_or_consume_notification(tmp_path):
    seed(tmp_path)
    result = watcher.run(csv_path="fictional.csv", dry_run=True, snapshot_dir=tmp_path,
                         load_csv_fn=lambda _: rows(1), hrhacker_run_fn=forbidden,
                         slack=True, slack_notify_fn=forbidden)
    assert result["skip_reason"] == "incomplete_csv"
    assert not (tmp_path / "incomplete_notifications.json").exists()


def test_failed_notification_can_retry(tmp_path):
    assessment = {"current": {"rows": 1, "shops": 1}, "reference": {"rows": 100, "shops": 10}}
    with pytest.raises(RuntimeError, match="notification failed"):
        guard.notify_once(tmp_path, assessment, lambda _: False)
    assert guard.notify_once(tmp_path, assessment, lambda _: True)


def test_export_wait_ignores_old_completed_and_old_pending():
    previous = [{"kishin": "fictional-old", "dl": "old-file", "shuryou": "done"},
                {"kishin": "fictional-pending", "dl": None, "shuryou": ""}]
    assert not fetcher.is_new_completed_export(previous[0], previous)
    assert not fetcher.is_new_completed_export(
        {"kishin": "fictional-pending", "dl": "pending-file", "shuryou": "done"}, previous)
    assert not fetcher.is_new_completed_export(
        {"kishin": "fictional-new", "dl": None, "shuryou": ""}, previous)
    assert fetcher.is_new_completed_export(
        {"kishin": "fictional-new", "dl": "new-file", "shuryou": "done"}, previous)


def test_cli_supports_safe_workflow_status(tmp_path):
    args = watcher.parse_args(["--slack", "--status-file", str(tmp_path / "status.json")])
    assert args.slack and args.status_file == tmp_path / "status.json"


def test_workflow_stops_post_steps_on_rejection():
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/job_daily.yml").read_text()
    branch = workflow.split('            hr)\n', 1)[1].split('            hr-copy-backfill)', 1)[0]
    assert branch.index('incomplete_csv') < branch.index('exit 0') < branch.index('backfill_deal_shop_id_via_email.py')


def test_poll_waits_for_new_export_to_finish(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    old = {"kishin": "fictional-old", "dl": "old-file", "shuryou": "done"}
    pending = {"kishin": "fictional-new", "dl": None, "shuryou": ""}
    complete = {"kishin": "fictional-new", "dl": "new-file", "shuryou": "done"}
    page = AsyncMock()
    read = AsyncMock(side_effect=[[pending, old], [complete, old]])
    monkeypatch.setattr(fetcher, "_read_export_rows", read)
    monkeypatch.setattr(fetcher, "_dismiss_onboarding", AsyncMock())
    monkeypatch.setattr(fetcher.asyncio, "sleep", AsyncMock())
    monkeypatch.setenv("HR_CSV_KICK_WAIT_SEC", "0")
    result = asyncio.run(fetcher._poll_export_list(
        page, kick_iso="fictional-start", timeout_min=1, previous_exports=[old]))
    assert result == "new-file"
    assert read.await_count == 2
    page.reload.assert_awaited_once()
