"""Count-only completeness guard; rejected exports never become a baseline."""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

# A loss of more than 20% is too large for routine daily turnover. Compare both
# rows and distinct shops so a partial export concentrated in one shop is caught.
MIN_COMPLETE_RATIO = 0.80
BASELINE_DAYS = 7
JST = timezone(timedelta(hours=9))


def counts(snapshot: dict) -> dict:
    jobs = snapshot.get("jobs", {})
    return {
        "rows": snapshot.get("row_count", len(jobs)),
        "shops": len({j.get("shop_id") for j in jobs.values() if j.get("shop_id")}),
    }


def assess(snapshot_dir: Path, current: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    history = []
    for path in sorted(snapshot_dir.glob("*.json.gz")):
        day = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
        if 0 <= (now.date() - day).days < BASELINE_DAYS:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                history.append(counts(json.load(f)))
    reference = {k: median(h[k] for h in history) for k in ("rows", "shops")} if history else None
    current_counts = counts(current)
    incomplete = bool(reference and any(
        reference[k] > 0 and current_counts[k] < reference[k] * MIN_COMPLETE_RATIO
        for k in current_counts))
    return {"incomplete": incomplete, "current": current_counts, "reference": reference,
            "baseline_samples": len(history), "min_ratio": MIN_COMPLETE_RATIO}


def notify_once(snapshot_dir: Path, assessment: dict, notify, now: datetime | None = None) -> bool:
    """Persist successful deliveries in the cached state directory; failed sends retry."""
    now = now or datetime.now(JST)
    # Scheduled runs keep the nominal slot even when the runner starts late.
    slot = {"0 16 * * *": "01", "0 3 * * *": "12", "0 11 * * *": "20"}.get(
        os.environ.get("SCHEDULE", ""), now.strftime("%H"))
    key = f"{now.date()}:{slot}"
    path = snapshot_dir / "incomplete_notifications.json"
    sent = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if key in sent:
        return False
    current, reference = assessment["current"], assessment["reference"]
    message = (f"HRH CSV 欠け検知 {key} JST: 行数={current['rows']} "
               f"比較元={reference['rows']} / 店舗数={current['shops']} "
               f"比較元={reference['shops']}。新規・変更・消失判定、HubSpot更新、"
               "後続の店舗ID補完・持ち主判定・AW連動を停止。正常な控えを維持。")
    if not notify(message):
        raise RuntimeError("HRH incomplete CSV Slack notification failed")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    # Keep only the current day; schedule jobs are serialized by workflow concurrency.
    path.write_text(json.dumps([k for k in sent if k.startswith(str(now.date()))] + [key]),
                    encoding="utf-8")
    return True
