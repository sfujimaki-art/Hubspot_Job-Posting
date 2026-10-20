# -*- coding: utf-8 -*-
"""リクロジの応募者を HRハッカーの求人からリクロジの求人へ付け替える (一回限り・2026-10-20)。

背景
  2026-10-20 の本番初回では、リクロジの応募を HRハッカーの求人 (id_hrhakkaa) に紐付けた。
  同日のユーザー決定で、リクロジは専用の求人 (id_reclogi) を持つことになった
  (媒体ごとに求人を分ける。reclogi_applicants.py のモジュール説明)。既に入った応募者を付け替える。

やること (対象 = oubobaitaimei=リクロジメディア の応募者)
  1. タブ「リクロジ応募」の行 (HubSpot応募者ID で照合) から 応募求人ID・掲載求人URL を取る
  2. reclogi_applicants.prepare_listing と同じ方法でリクロジの求人を用意する
     (あれば使う / 無ければページから作り、その場で取引に付ける)
  3. 応募者の求人の紐付けを、今の求人 → リクロジの求人 に付け替える
  4. 応募先の項目 (get_oubosaki_props) と、タブの追加項目を更新する。
     既存値は既存ルールどおり守る: 担当者・一次対応の要否は空のときだけ (要否は7日ガードも)、
     一次対応8項目は転記の印 (anmokuchi_tenki_nichiji) がある応募者には触らない。
     oubokyuujinmemo はリクロジ求人ID にする (relink と重複判定がこの値で引く)

既定は dry-run (予定一覧を出すだけ。HubSpot には書かない)。
--actual では、書く前に全対象の今の紐付けとプロパティを --backup-dir に JSON で控える。

CLI:
  python scripts/job_application_sync/migrate_reclogi_listing_20261020.py
  python scripts/job_application_sync/migrate_reclogi_listing_20261020.py --actual --backup-dir <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync import applicant_import as ai  # noqa: E402
from scripts.job_application_sync import reclogi_applicants as rl  # noqa: E402
from scripts.job_application_sync.fetchers import account_loader as al  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 控えと更新判定に読む応募者のプロパティ
APPT_PROPS = (["oubobaitaimei", "oubokyuujinmemo", "yingmuri", "yingmujinglu",
               "hubspot_owner_id", "ichijitaiounoumu", "kokyakushiitotenkijoukyou",
               "bikou_hiaringu", "oubosaki_kyuujin_title", "oubosaki_shigotonaiyou",
               "oubosaki_shokushu", "oubosaki_kinmuchi", "oubosaki_kinmujikan",
               "oubosaki_kinmukeitai", "oubosaki_kyuuyokeitai", "oubosaki_kyuujin_url",
               "oubosaki_torihiki_name", "oubosaki_contract_plan", "oubosaki_riyou_service",
               "anmokuchi_tenki_nichiji", "anmokuchi_tenki_moto_deal_id"]
              + sorted(set(rl.EXTRA_COLUMNS.values()))
              + [f"anmokuchi_{x}" for x in ("ashigirijouken", "keikenumukakunin",
                                             "mensetsujijisanbutsu", "oubohoukokusaki",
                                             "shikakuhoyuukakunin", "shodoutaiou",
                                             "taiouhouhou", "taiouhouhousonotashousai")])


def fetch_reclogi_appointments(client) -> list:
    """oubobaitaimei=リクロジメディア の応募者 [{id, properties}] (全件)。"""
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "oubobaitaimei", "operator": "EQ", "value": ai.RECLOGI_MEDIA}]}],
            "properties": APPT_PROPS, "limit": 100}
        if after:
            body["after"] = after
        r = client._post("/crm/v3/objects/0-421/search", body)
        r.raise_for_status()
        j = r.json()
        out += j.get("results", [])
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out


def plan_update_props(current: dict, oubosaki: dict, extra: dict, rid: str,
                      owner: Optional[str] = None) -> dict:
    """付け替え後に応募者へ書く値 (純関数)。今と同じ値は書かない。

    - oubokyuujinmemo = リクロジ求人ID、応募経路 = RECLOGI_YINGMUJINGLU
    - 応募先の項目 (oubosaki_*) はリクロジの求人の値で更新する
    - 担当者・一次対応の要否は空のときだけ。要否は7日ガードを通す
    - 一次対応8項目と転記の印は、印がある応募者には書かない (取引から1回だけ写す決まり)
    - タブの追加項目 (保有資格・本人希望記入欄 など) は空のときだけ
    - 求人未特定 (顧客シート転記状況=対象外) だった応募者は 未転記 に戻す (relink と同じ)
    """
    cur = {k: str(v or "").strip() for k, v in (current or {}).items()}
    want: dict = {"oubokyuujinmemo": rid, "yingmujinglu": ai.RECLOGI_YINGMUJINGLU}
    transferred = bool(cur.get("anmokuchi_tenki_nichiji"))
    for k, v in (oubosaki or {}).items():
        if k.startswith("anmokuchi_") and transferred:
            continue
        if k == "ichijitaiounoumu":
            if not cur.get(k):
                p = {k: v}
                ai.strip_ichijitaiou_if_stale(p, cur.get("yingmuri", "")[:10])
                want.update(p)
            continue
        want[k] = v
    if owner and not cur.get("hubspot_owner_id"):
        want["hubspot_owner_id"] = owner
    for k, v in (extra or {}).items():
        if not cur.get(k):
            want[k] = v
    if cur.get("kokyakushiitotenkijoukyou") == "対象外":
        want["kokyakushiitotenkijoukyou"] = "未転記"
    return {k: v for k, v in want.items()
            if str(v or "").strip() and str(v).strip() != cur.get(k, "")}


def build_plan(appts: list, recs_by_appt: dict, client,
               fetch_page: Callable[[str], Optional[str]] = None) -> list:
    """予定一覧 (読むだけ)。同じリクロジ求人は1回だけ調べる。"""
    cache: dict = {}
    rows = []
    for o in appts:
        aid = str(o["id"])
        rec = recs_by_appt.get(aid)
        row = {"appointment_id": aid, "current_listings": client.get_appointment_listings(aid),
               "reclogi_id": "", "action": "", "listing_id": None, "deals": [], "deal_how": ""}
        if rec is None:
            row["action"] = "タブに行が無い (手で確認)"
            rows.append(row)
            continue
        rid = rl.reclogi_job_id(rec)
        row["reclogi_id"] = rid
        if rid not in cache:
            cache[rid] = rl.prepare_listing(client, rid, rec.get(rl.C_JOB_URL),
                                            rec.get(rl.C_HR_JOB_ID), rec.get(rl.C_SHOP_ID),
                                            dry_run=True, fetch_page=fetch_page)
        prep = cache[rid]
        row.update(action=prep["action"], listing_id=prep["listing_id"],
                   deals=prep["deals"], deal_how=prep["deal_how"])
        if prep["action"] == "既存":
            row["deals"] = client.get_listing_deals(prep["listing_id"])
        if row["listing_id"] and row["current_listings"] == [row["listing_id"]]:
            row["action"] = "付け替え済み"
        rows.append(row)
    return rows


def backup(appts: list, plan_rows: list, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = {r["appointment_id"]: r["current_listings"] for r in plan_rows}
    path = out_dir / f"reclogi_listing_migrate_backup_{datetime.now():%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(
        [{"id": str(o["id"]), "properties": o.get("properties") or {},
          "listings": cur.get(str(o["id"]), [])} for o in appts],
        ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply(appts: list, plan_rows: list, recs_by_appt: dict, client,
          fetch_page: Callable[[str], Optional[str]] = None,
          make_listing: Callable = None) -> dict:
    """付け替えを実行する。backup() の後に呼ぶ。"""
    stat = {"listing_created": 0, "relinked": 0, "updated": 0, "skipped": 0, "failed": 0}
    lid_of: dict = {}
    props_of = {str(o["id"]): o.get("properties") or {} for o in appts}
    for row in plan_rows:
        rid = row["reclogi_id"]
        if row["action"] in ("作成予定",) and rid not in lid_of:
            rec = recs_by_appt[row["appointment_id"]]
            prep = rl.prepare_listing(client, rid, rec.get(rl.C_JOB_URL),
                                      rec.get(rl.C_HR_JOB_ID), rec.get(rl.C_SHOP_ID),
                                      dry_run=False, fetch_page=fetch_page,
                                      make_listing=make_listing)
            lid_of[rid] = prep["listing_id"]
            stat["listing_created"] += prep["action"] == "作成"
        elif row["listing_id"]:
            lid_of.setdefault(rid, row["listing_id"])
    for row in plan_rows:
        aid, rid = row["appointment_id"], row["reclogi_id"]
        lid = lid_of.get(rid) if rid else None
        if not lid:
            stat["skipped"] += 1
            continue
        try:
            for old in row["current_listings"]:
                if old != lid:
                    client.remove_appointment_listing(aid, old)
            if lid not in row["current_listings"]:
                client.associate_appointment_to_listing(aid, lid)
                stat["relinked"] += 1
            rec = recs_by_appt[aid]
            props = plan_update_props(
                props_of.get(aid, {}),
                client.get_oubosaki_props(lid, ai.RECLOGI_MEDIA, "", rid),
                rl.to_applicant_row(rec).extra, rid, client.get_listing_owner(lid))
            if props:
                client.update_appointment(aid, props)
                stat["updated"] += 1
        except Exception as e:  # noqa: BLE001
            stat["failed"] += 1
            print(f"  ★失敗 応募者={aid}: {type(e).__name__}: {str(e)[:120]}", flush=True)
    return stat


def print_plan(rows: list) -> None:
    print("応募者ID\t今の求人\tリクロジ求人ID\t新しい求人\t取引")
    for r in rows:
        new = r["listing_id"] or ("(作る)" if r["action"] == "作成予定" else "-")
        print(f"{r['appointment_id']}\t{','.join(r['current_listings']) or '-'}\t"
              f"{r['reclogi_id'] or '-'}\t{new} [{r['action']}]\t"
              f"{len(r['deals'])}件 {','.join(r['deals'])} ({r['deal_how'] or '-'})")
    from collections import Counter
    print(f"\n対象 {len(rows)}件: {dict(Counter(r['action'] for r in rows))}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--actual", action="store_true", help="HubSpot に書く (既定は dry-run)")
    p.add_argument("--backup-dir", default="", help="--actual のとき必須。書く前の控えの置き場")
    a = p.parse_args(argv)
    if a.actual and not a.backup_dir:
        print("--actual には --backup-dir が必要です (書く前の控えを残すため)")
        return 2
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        print("HUBSPOT_ACCESS_TOKEN が未設定")
        return 2
    client = ai.RealHubSpotClient(token)
    ws, why = rl.open_tab()
    if ws is None:
        print(f"タブを開けない: {why}")
        return 2
    _idx, recs = rl.records_from_values(al.sheet_retry(ws.get_all_values))
    recs_by_appt = {r.get(rl.C_APPT_ID): r for r in recs if r.get(rl.C_APPT_ID)}
    appts = fetch_reclogi_appointments(client)
    rows = build_plan(appts, recs_by_appt, client)
    print_plan(rows)
    if not a.actual:
        print("\n(dry-run: HubSpot には書いていません)")
        return 0
    path = backup(appts, rows, Path(a.backup_dir))
    print(f"\n控え: {path}")
    stat = apply(appts, rows, recs_by_appt, client)
    print(f"結果: {stat}")
    return 1 if stat["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
