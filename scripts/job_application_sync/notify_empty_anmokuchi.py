# -*- coding: utf-8 -*-
"""一次対応が「必要」なのに8項目が空のまま進んだ取引を知らせる (2026-10-02)。

## なぜ要るか

一次対応の8項目 (初動対応・対応方法・足切り条件 など) は、9/17 の定例MTGで
「契約更新時に自動で引き継がず、求人出稿完了のタイミングで手入力する」と
決まった。9/10 からは求人出稿完了に入ると項目が表示される設定にしてある。

ところが「表示される」だけでは入力は担保されない。2026-10-02 実測:
9/10 以降に求人出稿完了へ入った「要否=必要」の生きた取引 74件のうち
**41件 (55%) が空のまま**先へ進んでいた。継続時はステージを飛ばすこともあり、
求人出稿完了を通らずに進んだ取引も19件あった。

空の取引に応募が来ると、BPOは条件が分からないまま一次対応することになる。
人が気づける形で毎晩一覧にする (ユーザー承認 2026-10-02「2. 空のまま進んだ
取引を知らせる」)。入力そのものは人がやる (9/17 決定)。

## 対象

  納品管理PLの生きている取引 (deal_stages.is_writable) のうち
    - 一次対応の要否 (itijitaiou) = true
    - ステージが求人出稿完了以降 (パイプラインの並び順で判定。APIから取る)
      ※求人出稿完了を通らずに先へ進んだ取引も含む
    - 8項目のどれにも中身が無い (deal_master.has_anmokuchi)。
      足切り条件・応募報告先はテンプレートのままなら空とみなす

## 送り先 (ハードコードしない・既定値も持たない)

  SLACK_EMPTY_ANMOKUCHI_WEBHOOK   … 送信先の Incoming Webhook URL
  SLACK_EMPTY_ANMOKUCHI_MENTIONS  … 一緒にメンションするSlackユーザーID
                                     (カンマ区切り。空ならメンションなし)
  どちらも未設定なら送らず、ログとCSVだけ残す。

使い方:
  python scripts/job_application_sync/notify_empty_anmokuchi.py          # dry-run (送らない)
  python scripts/job_application_sync/notify_empty_anmokuchi.py --send   # Slackへ送る
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if (_REPO / ".env").exists():
    load_dotenv(_REPO / ".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from scripts.job_application_sync import deal_master as DM  # noqa: E402
from scripts.job_application_sync import deal_stages as DS  # noqa: E402
from scripts.job_application_sync.hs_paging import search_all_by_id  # noqa: E402

BASE = "https://api.hubapi.com"
PIPELINE = "21596025"            # 納品管理
STAGE_PUBLISHED = "52016155"     # 求人出稿完了
PORTAL_ENV = "HUBSPOT_PORTAL_ID"
ENV_WEBHOOK = "SLACK_EMPTY_ANMOKUCHI_WEBHOOK"
ENV_MENTIONS = "SLACK_EMPTY_ANMOKUCHI_MENTIONS"
OUT_DIR = _REPO / "data" / "job_application_sync"
_JST = timezone(timedelta(hours=9))
# 1担当者あたりSlackに並べる件数。残りはCSVで見る。
MAX_LINKS_PER_OWNER = 10

DEAL_PROPS = (["dealname", "dealstage", "itijitaiou", "hubspot_owner_id",
               f"hs_v2_date_entered_{STAGE_PUBLISHED}"]
              + list(DM.ANMOKUCHI_PROPS))


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}"}


def stage_order() -> dict:
    """{ステージID: 並び順}。ステージの増減に追従するためAPIから取る。"""
    r = requests.get(f"{BASE}/crm/v3/pipelines/deals/{PIPELINE}",
                     headers=_h(), timeout=30)
    r.raise_for_status()
    return {s["id"]: s.get("displayOrder", 0) for s in r.json().get("stages", [])}


def pick_targets(deals: list, order: dict) -> list:
    """対象の取引だけに絞る (純関数)。"""
    floor = order.get(STAGE_PUBLISHED)
    if floor is None:
        raise RuntimeError(f"求人出稿完了 ({STAGE_PUBLISHED}) がパイプラインに見当たりません")
    out = []
    for d in deals:
        p = d.get("properties") or {}
        st = p.get("dealstage")
        if not DS.is_writable(st):
            continue
        if p.get("itijitaiou") != "true":
            continue
        if order.get(st, -1) < floor:
            continue
        if DM.has_anmokuchi(p):
            continue
        out.append(d)
    return out


def owner_name(owner_id: str, cache: dict) -> str:
    """担当者ID → 名前。無効化ユーザーは archived=true で引く。

    ★無効化ユーザーの /owners/{id} は 404 を **HTMLで**返すことがある
      (JSONとして読むと落ちる。2026-09-24 実測)。中身を見ずに状態コードで判断する。
    """
    if not owner_id:
        return "(担当者なし)"
    if owner_id in cache:
        return cache[owner_id]
    name = f"(担当者ID {owner_id})"
    for archived in (False, True):
        try:
            r = requests.get(f"{BASE}/crm/v3/owners/{owner_id}", headers=_h(),
                             params={"archived": "true"} if archived else None,
                             timeout=30)
        except requests.RequestException:
            continue
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            o = r.json()
            full = f"{o.get('lastName') or ''} {o.get('firstName') or ''}".strip()
            name = (full or o.get("email") or name) + ("（無効）" if archived else "")
            break
    cache[owner_id] = name
    return name


def deal_url(deal_id: str) -> str:
    portal = os.environ.get(PORTAL_ENV, "")
    return (f"https://app.hubspot.com/contacts/{portal}/record/0-3/{deal_id}"
            if portal else f"取引ID {deal_id}")


def group_by_owner(targets: list, names: dict) -> dict:
    """{担当者名: [取引]}。件数の多い順に並べ替えて返す。"""
    g = defaultdict(list)
    for d in targets:
        oid = (d.get("properties") or {}).get("hubspot_owner_id") or ""
        g[names.get(oid, "(担当者なし)")].append(d)
    return dict(sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def build_message(groups: dict, mentions: list, csv_name: str) -> str:
    total = sum(len(v) for v in groups.values())
    head = " ".join(f"<@{m}>" for m in mentions)
    lines = [f"{head} " if head else "",
             f"一次対応が「必要」なのに、一次対応の8項目が空のまま"
             f"求人出稿完了以降へ進んでいる取引が *{total}件* あります。",
             "応募が来るとBPOが条件を知らないまま対応することになるので、"
             "取引の「一次対応」の項目を入力してください。", ""]
    for owner, ds in groups.items():
        lines.append(f"*{owner}*  {len(ds)}件")
        for d in ds[:MAX_LINKS_PER_OWNER]:
            p = d.get("properties") or {}
            skipped = "（求人出稿完了を通らずに進行）" \
                if not p.get(f"hs_v2_date_entered_{STAGE_PUBLISHED}") else ""
            lines.append(f"  • <{deal_url(d['id'])}|{(p.get('dealname') or '')[:40]}>"
                         f"{skipped}")
        if len(ds) > MAX_LINKS_PER_OWNER:
            lines.append(f"  …ほか{len(ds) - MAX_LINKS_PER_OWNER}件")
    lines += ["", f"全件の一覧: 成果物「要対応リスト」の {csv_name}"]
    return "\n".join(x for x in lines if x is not None).lstrip()


def write_csv(groups: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["担当者", "取引名", "取引ID", "ステージ",
                    "求人出稿完了に入った日", "URL"])
        for owner, ds in groups.items():
            for d in ds:
                p = d.get("properties") or {}
                w.writerow([owner, p.get("dealname") or "", d["id"],
                            DS.label(p.get("dealstage")),
                            (p.get(f"hs_v2_date_entered_{STAGE_PUBLISHED}") or "")[:10]
                            or "(通らずに進行)",
                            deal_url(d["id"])])
    return path


def send_slack(text: str, webhook: str) -> bool:
    r = requests.post(webhook, json={"text": text}, timeout=30)
    return r.status_code == 200


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="Slackへ送る (既定は送らない)")
    a = ap.parse_args(argv)

    order = stage_order()
    deals = search_all_by_id("0-3", DEAL_PROPS, [
        {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE}])
    targets = pick_targets(deals, order)
    cache: dict = {}
    names = {oid: owner_name(oid, cache) for oid in
             {(d.get("properties") or {}).get("hubspot_owner_id") or "" for d in targets}}
    groups = group_by_owner(targets, names)
    today = datetime.now(_JST).strftime("%Y-%m-%d")
    csv_path = write_csv(groups, OUT_DIR / f"要対応_一次対応の項目が空の取引_{today}.csv")

    print(f"=== 一次対応の8項目が空の取引 ({'send' if a.send else 'dry-run'}) ===", flush=True)
    print(f"納品管理PL {len(deals):,}件 → 対象 {len(targets)}件", flush=True)
    for owner, ds in groups.items():
        print(f"  {owner}: {len(ds)}件", flush=True)
    print(f"CSV: {csv_path}", flush=True)

    webhook = os.environ.get(ENV_WEBHOOK, "").strip()
    mentions = [m.strip() for m in os.environ.get(ENV_MENTIONS, "").split(",") if m.strip()]
    if not targets:
        print("対象なし。通知しない", flush=True)
        return 0
    if not a.send:
        print("dry-run: Slackには送らない", flush=True)
        return 0
    if not webhook:
        print(f"{ENV_WEBHOOK} が未設定のため送らない (ログとCSVのみ)", flush=True)
        return 0
    ok = send_slack(build_message(groups, mentions, csv_path.name), webhook)
    print(f"Slack送信: {'成功' if ok else '失敗'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
