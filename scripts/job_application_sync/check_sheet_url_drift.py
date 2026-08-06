# -*- coding: utf-8 -*-
"""顧客シートURLの乖離検知 (2026-08-06) — 検知のみ・自動修正しない。

なぜ必要か:
  顧客管理シートのD列リンク(会社名セルのハイパーリンク)が唯一の正だが、
  現場がリンクを貼り替えても HubSpot の customer_sheet_url は追従しない。
  2026-08-06 に SKテックで実害が発生した:
    - 現場が誤ったシートを紐付けていた → 後に修正
    - しかし HubSpot は旧URLのまま55件 → 使われていないシートへ転記され続けた
    - 2つのシートは名前がほぼ同一(「SKテック 御中」vs「SKテック御中」)で、
      名前の突合では発見できなかった

なぜ自動修正しないか:
  D列は人が編集する場所であり、一時的な削除・貼り間違いがそのまま
  HubSpot へ反映されると被害が広がる。**検知してSlackで知らせるに留める**。

使い方:
  python scripts/job_application_sync/check_sheet_url_drift.py           # 検知のみ
  python scripts/job_application_sync/check_sheet_url_drift.py --slack   # Slack通知あり
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")
os.environ.setdefault("SHEETS_AUTH_MODE", "sa")

BASE = "https://api.hubapi.com"
SHEET_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{30,})")


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def slack_notify(message: str) -> bool:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:200]}", flush=True)
        return False
    try:
        return requests.post(url, json={"text": message}, timeout=15).status_code == 200
    except requests.RequestException as e:
        print(f"[slack送信失敗] {e}", flush=True)
        return False


def read_master_links() -> dict:
    """顧客管理シートのD列リンク → {会社名: シートID}。"""
    from scripts.job_application_sync import applicant_queue as aq
    from scripts.job_application_sync.fetchers import account_loader as al
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    cred = Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=cred)
    gc = aq._sheets_client()
    av = gc.open_by_key(al.SHEET_ID).sheet1.get_all_values()
    cols = aq._resolve_account_columns(av[0], av[1:])
    ci = cols["comp"]
    meta = svc.spreadsheets().get(spreadsheetId=al.SHEET_ID,
                                  fields="sheets.properties.title").execute()
    title = meta["sheets"][0]["properties"]["title"]
    letter = aq._col_letter(ci)
    # 会社名セルを一括取得 (1行ずつ引くとAPI消費が大きいため範囲で取る)
    last = len(av)
    out = {}
    for start in range(2, last + 1, 500):
        end = min(start + 499, last)
        r = svc.spreadsheets().get(
            spreadsheetId=al.SHEET_ID, ranges=[f"{title}!{letter}{start}:{letter}{end}"],
            includeGridData=True,
            fields="sheets.data.rowData.values(formattedValue,hyperlink,"
                   "textFormatRuns(format/link))").execute()
        rows = (r["sheets"][0]["data"][0].get("rowData") or [])
        for off, rd in enumerate(rows):
            vals = rd.get("values") or [{}]
            cell = vals[0] if vals else {}
            name = (cell.get("formattedValue") or "").strip()
            if not name:
                continue
            link = cell.get("hyperlink")
            if not link:
                for tr in (cell.get("textFormatRuns") or []):
                    u = ((tr.get("format") or {}).get("link") or {}).get("uri")
                    if u:
                        link = u
                        break
            m = SHEET_ID_RE.search(link or "")
            out[name] = {"sheet_id": m.group(1) if m else None,
                         "row": start + off}
        time.sleep(0.2)
    return out


def hubspot_sheet_ids() -> dict:
    """HubSpotの求人が持つ customer_sheet_url → {シートID: 求人数}。"""
    counts = Counter()
    after = None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "customer_sheet_url", "operator": "HAS_PROPERTY"}]}],
            "properties": ["customer_sheet_url"], "limit": 100}
        if after:
            b["after"] = after
        for i in range(5):
            r = requests.post(f"{BASE}/crm/v3/objects/0-420/search",
                              headers=_h(), json=b, timeout=90)
            if r.status_code == 200:
                break
            time.sleep(2 ** i * 2)
        j = r.json()
        for o in j.get("results", []):
            u = (o.get("properties") or {}).get("customer_sheet_url") or ""
            m = SHEET_ID_RE.search(u)
            if m:
                counts[m.group(1)] += 1
        after = j.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.12)
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack", action="store_true", help="乖離をSlackへ通知")
    ap.add_argument("--out-dir", default="claudedocs")
    a = ap.parse_args(argv)
    print("顧客管理シートのD列リンクを取得中...", flush=True)
    master = read_master_links()
    linked = {k: v for k, v in master.items() if v["sheet_id"]}
    print(f"  会社 {len(master):,}件 / D列リンクあり {len(linked):,}件", flush=True)
    print("HubSpotの求人URLを集計中...", flush=True)
    hs = hubspot_sheet_ids()
    print(f"  HubSpotで使われているシート {len(hs):,}種 "
          f"(求人 {sum(hs.values()):,}件)\n", flush=True)
    master_ids = {v["sheet_id"] for v in linked.values()}
    # 乖離1: HubSpotが使っているのにD列のどこにも無いシート = 古い/誤ったURL
    orphan = {sid: n for sid, n in hs.items() if sid not in master_ids}
    # 乖離2: D列にリンクがあるのにHubSpot側が0件 = 未反映
    unused = {name: v["sheet_id"] for name, v in linked.items()
              if v["sheet_id"] not in hs}
    print("=== 乖離の検知結果 ===")
    print(f"  ★HubSpotが使用中だがD列に存在しないシート: {len(orphan):,}種 "
          f"(求人 {sum(orphan.values()):,}件)")
    print(f"   D列にあるがHubSpot未反映のシート        : {len(unused):,}件")
    for sid, n in sorted(orphan.items(), key=lambda x: -x[1])[:8]:
        print(f"      求人{n:4d}件  {sid[:26]}…")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"顧客シートURL乖離_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["種別", "会社名", "シートID", "HubSpot求人数"])
        for sid, n in sorted(orphan.items(), key=lambda x: -x[1]):
            w.writerow(["HubSpot使用中だがD列に無い", "", sid, n])
        for name, sid in unused.items():
            w.writerow(["D列にあるがHubSpot未反映", name, sid, 0])
    print(f"\n一覧: {p.resolve()}")
    if a.slack and (orphan or unused):
        msg = [f"⚠️ 顧客シートURLの乖離を検知しました"]
        if orphan:
            msg.append(f"・HubSpotが使用中だが顧客管理シートD列に無い: "
                       f"{len(orphan)}シート(求人{sum(orphan.values())}件)")
            msg.append("  → D列が貼り替えられた可能性。誤ったシートへ転記され続けます")
        if unused:
            msg.append(f"・D列にあるがHubSpot未反映: {len(unused)}件")
        msg.append(f"一覧: {p.name}")
        slack_notify("\n".join(msg))
    return {"orphan": len(orphan), "unused": len(unused)}


if __name__ == "__main__":
    main()
