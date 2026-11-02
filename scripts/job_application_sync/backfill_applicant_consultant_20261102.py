"""既にある応募者の「担当コンサルタント」(dandangkonsarutanto) を埋める一回限りのスクリプト (2026-11-02)。

新しい応募は applicant_import.get_oubosaki_props が書く。これは過去分の穴埋め。
ユーザー決定: まだ動いている取引の分だけ。
  求人の持ち主コード (hr_owner_code) → そのコードの納品管理PL取引 → 今の契約 (latest_live)。
  その取引が生きていて、コンサル担当が入っているときだけ、紐付く応募者のうち空のものに名前を書く。
  持ち主コードが無い求人は対象外 (件数だけ出す)。既に値がある応募者は触らない。

既定はドライラン。--apply で書き込む。書き込む前に控え CSV を --backup-dir に出す。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.job_application_sync import applicant_import as ai  # noqa: E402
from scripts.job_application_sync import deal_master as DM  # noqa: E402

BASE = "https://api.hubapi.com"
PROP = "dandangkonsarutanto"


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def _req(s, method, url, **kw):
    for i in range(6):
        r = s.request(method, url, timeout=60, **kw)
        if r.status_code in (429, 502, 503, 504):
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _search_all(s, obj, filters, props):
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 200,
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
        if after:
            body["after"] = after
        j = _req(s, "post", f"{BASE}/crm/v3/objects/{obj}/search", json=body)
        out += j.get("results", [])
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out
        if len(out) >= 9800:   # search は 1万件で打ち切られる → 最後のIDから続ける
            last = out[-1]["id"]
            filters = [f for f in filters if f.get("propertyName") != "hs_object_id"] + [
                {"propertyName": "hs_object_id", "operator": "GT", "value": last}]
            return out + _search_all(s, obj, filters, props)


def plan(s) -> tuple:
    deals = {d["id"]: d.get("properties") or {} for d in _search_all(
        s, "deals", [{"propertyName": "pipeline", "operator": "EQ", "value": ai.DELIVERY_PIPELINE}],
        ai.DEAL_READ_PROPS)}
    by_code = DM.group_by_code(deals)
    listings = _search_all(s, "0-420", [{"propertyName": "hs_object_id", "operator": "HAS_PROPERTY"}],
                           [DM.LISTING_OWNER])
    stats = {"求人": len(listings), "持ち主コードなし": 0, "取引が生きていない": 0,
             "コンサル担当なし": 0, "対象求人": 0}
    listing_owner = {}
    for lp in listings:
        code = ((lp.get("properties") or {}).get(DM.LISTING_OWNER) or "").strip()
        if not code:
            stats["持ち主コードなし"] += 1
            continue
        group = by_code.get(code) or []
        did = DM.latest_live(group, deals)
        if not did or not DM.is_live(deals[did]):
            stats["取引が生きていない"] += 1
            continue
        cid = ai.consultant_owner_id(group, deals)
        if not cid:
            stats["コンサル担当なし"] += 1
            continue
        listing_owner[lp["id"]] = (cid, did)
        stats["対象求人"] += 1
    return listing_owner, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir", default="")
    a = ap.parse_args(argv)
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN") or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        print("HUBSPOT_ACCESS_TOKEN が未設定")
        return 2
    s = _session(token)
    cli = ai.RealHubSpotClient(token)
    listing_owner, stats = plan(s)
    print("求人:", stats)

    # 求人 → 応募者
    lids = list(listing_owner)
    appt_listing = {}
    for i in range(0, len(lids), 1000):
        j = _req(s, "post", f"{BASE}/crm/v4/associations/0-420/0-421/batch/read",
                 json={"inputs": [{"id": x} for x in lids[i:i + 1000]]})
        for r in j.get("results", []):
            for t in r.get("to", []):
                appt_listing.setdefault(str(t["toObjectId"]), str(r["from"]["id"]))
    # 応募者の今の値
    ids = list(appt_listing)
    cur = {}
    for i in range(0, len(ids), 100):
        j = _req(s, "post", f"{BASE}/crm/v3/objects/0-421/batch/read",
                 json={"inputs": [{"id": x} for x in ids[i:i + 100]], "properties": [PROP]})
        for r in j.get("results", []):
            cur[r["id"]] = (r.get("properties") or {}).get(PROP) or ""
    names, todo, skip_has, skip_noname = {}, [], 0, 0
    for aid, lid in appt_listing.items():
        if cur.get(aid, "").strip():
            skip_has += 1
            continue
        cid, did = listing_owner[lid]
        if cid not in names:
            names[cid] = cli.get_owner_full_name(cid)
        if not names[cid]:
            skip_noname += 1
            continue
        todo.append((aid, lid, did, cid, names[cid]))
    print(f"応募者: 紐付き {len(appt_listing)} / 書く {len(todo)} / 既に値あり {skip_has} / 名前が引けない {skip_noname}")
    print("担当ごとの件数:", sorted(((n, sum(1 for t in todo if t[4] == n)) for n in set(t[4] for t in todo)),
                              key=lambda x: -x[1])[:15])
    if not a.apply:
        print("ドライラン（--apply で書き込む）")
        return 0
    if not a.backup_dir:
        print("--apply には --backup-dir が要る")
        return 2
    bd = Path(a.backup_dir)
    bd.mkdir(parents=True, exist_ok=True)
    bp = bd / f"担当コンサルタント_書き込み控え_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(bp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["応募者ID", "求人ID", "取引ID", "コンサル担当ownerID", "書く値", "書く前の値"])
        for aid, lid, did, cid, n in todo:
            w.writerow([aid, lid, did, cid, n, cur.get(aid, "")])
    print("控え:", bp)
    done = 0
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        _req(s, "post", f"{BASE}/crm/v3/objects/0-421/batch/update",
             json={"inputs": [{"id": t[0], "properties": {PROP: t[4]}} for t in chunk]})
        done += len(chunk)
    print("書き込み:", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
