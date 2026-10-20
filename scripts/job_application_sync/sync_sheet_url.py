"""報告シートの URL を取引から求人票へ写す (Deal → LISTING、毎日)。

取引の customer_sheet_url が正本。求人票の customer_sheet_url は、
応募の customer_sheet_url_synced (計算項目) の元になり、HR_HR の応募の
架電画面がこれで報告シートを開く。取引にだけ入っていて求人票が空だと、
応募の画面からシートを開けない (2026-10-19 実測: ある取引の求人票
23件がすべて空)。

決め方は顧客シート転記と同じ customer_sheet_sync.owner_sheet_url
(求人票に紐付く取引のうちシートURLを持つものの latest_live)。

- 求人票が空のときだけ写す。
- 求人票にすでに別の値があれば書き換えない。件数と求人票IDを出すだけ
  (人が直した値・移行前の値を勝手に上書きしない)。

CLI:
  python sync_sheet_url.py [--dry-run|--actual]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync.hs_paging import (  # noqa: E402
    BASE, post_retry, search_all_by_id)
from scripts.job_application_sync.customer_sheet_sync import (  # noqa: E402
    _OWNER_PROPS, owner_sheet_url)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROP = "customer_sheet_url"


def _assoc(frm: str, to: str, ids: list[str]) -> dict:
    m: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v4/associations/{frm}/{to}/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in r.get("results", []):
            m[str(res["from"]["id"])] = [str(t["toObjectId"]) for t in res.get("to", [])]
        time.sleep(0.1)
    return m


def _batch_read(obj: str, ids: list[str], props: list[str]) -> dict:
    m: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v3/objects/{obj}/batch/read",
                       {"properties": props, "inputs": [{"id": x} for x in ids[i:i + 100]]})
        for o in r.get("results", []):
            m[str(o["id"])] = o.get("properties") or {}
        time.sleep(0.1)
    return m


def plan(lids, l2d: dict, deals: dict, listings: dict) -> tuple[list, list, Counter]:
    """求人票ごとに 写す / 同じ / 違う(触らない) / 取引にURLなし を決める。"""
    fill, differ, c = [], [], Counter()
    for lid in lids:
        want = owner_sheet_url(l2d.get(lid) or [], deals).strip()
        cur = ((listings.get(lid) or {}).get(PROP) or "").strip()
        if not want:
            c["no_url"] += 1
        elif cur == want:
            c["same"] += 1
        elif not cur:
            c["fill"] += 1
            fill.append({"id": lid, "properties": {PROP: want}})
        else:
            c["different_skipped"] += 1
            differ.append(lid)
    return fill, differ, c


def run(dry_run: bool = True) -> dict:
    deals_with = search_all_by_id("0-3", _OWNER_PROPS,
                                  [{"propertyName": PROP, "operator": "HAS_PROPERTY"}])
    d2l = _assoc("0-3", "0-420", [str(d["id"]) for d in deals_with])
    lids = sorted({l for v in d2l.values() for l in v})
    # 求人票に付くほかの取引 (URLなし・別URLの契約) も見て「今の契約」を決める
    l2d = _assoc("0-420", "0-3", lids)
    deals = _batch_read("0-3", sorted({d for v in l2d.values() for d in v}), _OWNER_PROPS)
    listings = _batch_read("0-420", lids, [PROP])
    fill, differ, c = plan(lids, l2d, deals, listings)
    applied = 0
    if not dry_run:
        for i in range(0, len(fill), 100):
            post_retry(f"{BASE}/crm/v3/objects/0-420/batch/update",
                       {"inputs": fill[i:i + 100]})
            applied += len(fill[i:i + 100])
            time.sleep(0.15)
    summary = {"deals_with_url": len(deals_with), "listings": len(lids),
               **dict(c), "applied": applied}
    print(f"[sync_sheet_url] {summary}", flush=True)
    if differ:
        print(f"[sync_sheet_url] 求人票と取引で値が違う (書き換えていない) 求人票ID "
              f"先頭20件: {', '.join(differ[:20])}", flush=True)
    return summary


def _args(argv=None):
    p = argparse.ArgumentParser(description="報告シートURL 取引→求人票 (空欄だけ)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    run(dry_run=_args().dry_run)
