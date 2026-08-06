# -*- coding: utf-8 -*-
"""取引の暗黙知メモ → 配下の求人へ転記する (2026-08-06)。

## なぜこの向きなのか

求人は「新着」を取るためクローズ→出し直しで作り替えられる。旧新を結ぶIDが
無く、名前の類似度でも66%しか対応付かないため、**求人にメモを置くと
出し直すたびに失われる**。実測では公開中2,431件のうち記入済みメモを持つのは
ごく一部だった。

そこで発想を逆転し、**メモの正を親の取引に置く**。取引は求人を作り替えても
変わらないので、「継承」という作業自体が不要になる。求人はそこから転記を
受けるだけになる。

    取引(正)  ──転記──>  求人  ──既存のコピー──>  応募

2026-08-06 に既存の記入済みメモ6,145件を452取引へ集約した
(`rollup_memo_to_deal.py`)。本スクリプトはその続きで、**集約したメモを
配下の求人へ流す**。ここが無いと、集約したメモは取引カードに置いてあるだけで
現場(応募者の一次対応をする人)には届かない。

## やらないこと

- 人が求人に直接書いたメモは消さない。転記メモは別のNoteとして追加する
- 同じ内容を二度付けない(署名で冪等判定)
- 取引メモが更新されたら、求人側の転記メモも作り直す(内容ハッシュで比較)
- 公開終了の求人には転記しない(もう応募が来ないため。既定。--include-closed で変更可)

使い方:
  python scripts/job_application_sync/sync_deal_memo_to_listing.py            # 対象の確認のみ
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --actual   # 転記する
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --rollback <json>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")

# Windowsローカルの既定は cp932。ログ出力の1文字で処理全体が落ちるのは
# 本末転倒なので明示的に固定する (CIは PYTHONIOENCODING=utf-8 で問題ない)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from scripts.job_application_sync.hs_paging import (  # noqa: E402
    iter_all, post_retry)

BASE = "https://api.hubapi.com"
LISTING = "0-420"
DEAL = "0-3"
# 集約メモの署名 (rollup_memo_to_deal.py と一致させること)
ROLLUP_SIGNATURE = "📋 暗黙知メモ（取引単位・配下求人を網羅）"
# 転記メモの署名。人が書いたメモと区別し、貼り直しの冪等判定に使う。
# 本文にハッシュを埋めるので、取引メモが変わったことも検出できる。
TRANSFER_SIGNATURE = "📎 取引から転記された暗黙知メモ"
HASH_RE = re.compile(r"<!--memo:([0-9a-f]{12})-->")
# 応募が来ない求人には転記しない (既定)
SKIP_STATUS = ("公開終了",)


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def body_hash(text: str) -> str:
    """取引メモの内容ハッシュ。更新を検出して貼り直すために使う。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def transfer_body(deal_memo: str, deal_name: str, h: str) -> str:
    """求人へ貼る本文。出典と、貼り直し判定用のハッシュを埋める。"""
    inner = deal_memo
    # 取引側の見出しは転記先では冗長なので、転記の見出しに差し替える
    inner = inner.replace(ROLLUP_SIGNATURE, "").lstrip("\n")
    inner = inner.replace("この取引に紐づく求人へ自動転記されます。", "").lstrip("\n")
    return (f"{TRANSFER_SIGNATURE}\n"
            f"出典: {deal_name}\n"
            f"この内容は取引レコードで管理されています。"
            f"修正は取引側で行ってください（求人側で直しても次回上書きされます）。\n"
            f"<!--memo:{h}-->\n\n{inner}")


def collect_deal_memos() -> dict:
    """集約メモを持つ取引 → (本文, 取引名)。

    Search API を使わない理由: Note の本文検索は取りこぼしやすく、
    件数も1万件上限に触れうる。取引側から association を辿る。
    """
    # 集約メモは rollup_memo_to_deal が作った Note。署名で引く。
    notes = []
    after = None
    while True:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
             "value": "暗黙知メモ"}]}],
            "properties": ["hs_note_body"], "limit": 100}
        if after:
            body["after"] = after
        j = post_retry(f"{BASE}/crm/v3/objects/notes/search", body)
        notes += j.get("results") or []
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.1)
    notes = [n for n in notes
             if ROLLUP_SIGNATURE in ((n.get("properties") or {}).get("hs_note_body") or "")]
    print(f"集約メモ(Note) {len(notes):,}件", flush=True)
    if not notes:
        return {}
    # Note → Deal
    nid2deal = {}
    ids = [n["id"] for n in notes]
    for i in range(0, len(ids), 100):
        j = post_retry(f"{BASE}/crm/v4/associations/notes/{DEAL}/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in j.get("results", []):
            to = res.get("to") or []
            if to:
                nid2deal[str(res["from"]["id"])] = str(to[0]["toObjectId"])
        time.sleep(0.1)
    # 取引名
    dids = sorted(set(nid2deal.values()))
    dname = {}
    for i in range(0, len(dids), 100):
        j = post_retry(f"{BASE}/crm/v3/objects/{DEAL}/batch/read",
                       {"inputs": [{"id": x} for x in dids[i:i + 100]],
                        "properties": ["dealname"]})
        for o in j.get("results", []):
            dname[str(o["id"])] = (o.get("properties") or {}).get("dealname") or ""
        time.sleep(0.1)
    out = {}
    for n in notes:
        did = nid2deal.get(n["id"])
        if not did:
            continue
        raw = (n.get("properties") or {}).get("hs_note_body") or ""
        out[did] = (raw.replace("<br>", "\n"), dname.get(did, ""))
    print(f"  取引に紐づいたもの: {len(out):,}件", flush=True)
    return out


def listing_transfer_state(listing_ids: list) -> dict:
    """求人 → {"note_id":..., "hash":...}。転記メモが既にあるか・内容が同じか。"""
    state = {}
    for i in range(0, len(listing_ids), 100):
        chunk = listing_ids[i:i + 100]
        j = post_retry(f"{BASE}/crm/v4/associations/{LISTING}/notes/batch/read",
                       {"inputs": [{"id": x} for x in chunk]})
        pairs = []
        for res in j.get("results", []):
            lid = str(res["from"]["id"])
            for t in (res.get("to") or []):
                pairs.append((lid, str(t["toObjectId"])))
        if not pairs:
            time.sleep(0.08)
            continue
        nids = sorted({n for _l, n in pairs})
        bodies = {}
        for k in range(0, len(nids), 100):
            jj = post_retry(f"{BASE}/crm/v3/objects/notes/batch/read",
                            {"inputs": [{"id": x} for x in nids[k:k + 100]],
                             "properties": ["hs_note_body"]})
            for o in jj.get("results", []):
                bodies[str(o["id"])] = (
                    (o.get("properties") or {}).get("hs_note_body") or "")
            time.sleep(0.08)
        for lid, nid in pairs:
            b = bodies.get(nid, "")
            if TRANSFER_SIGNATURE in b:
                m = HASH_RE.search(b)
                state[lid] = {"note_id": nid, "hash": m.group(1) if m else ""}
        time.sleep(0.08)
    return state


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--include-closed", action="store_true",
                    help="公開終了の求人にも転記する")
    ap.add_argument("--rollback", default="")
    ap.add_argument("--out-dir", default="claudedocs")
    a = ap.parse_args(argv)

    if a.rollback:
        items = json.loads(Path(a.rollback).read_text(encoding="utf-8"))
        print(f"{len(items):,}件の転記メモを削除します", flush=True)
        ok = 0
        for n, it in enumerate(items, 1):
            r = requests.delete(f"{BASE}/crm/v3/objects/notes/{it['note_id']}",
                                headers=_h(), timeout=30)
            if r.status_code in (200, 204, 404):
                ok += 1
            if n % 100 == 0:
                print(f"  {n:,}/{len(items):,}", flush=True)
            time.sleep(0.08)
        print(f"削除 {ok:,}件")
        return 0

    memos = collect_deal_memos()
    if not memos:
        print("集約メモがありません。先に rollup_memo_to_deal.py を実行してください")
        return 1

    # 取引 → 配下の求人
    dids = list(memos)
    d2l = {}
    for i in range(0, len(dids), 100):
        j = post_retry(f"{BASE}/crm/v4/associations/{DEAL}/{LISTING}/batch/read",
                       {"inputs": [{"id": x} for x in dids[i:i + 100]]})
        for res in j.get("results", []):
            d2l[str(res["from"]["id"])] = [str(t["toObjectId"])
                                           for t in (res.get("to") or [])]
        time.sleep(0.1)
    all_lids = sorted({x for v in d2l.values() for x in v})
    print(f"配下の求人 {len(all_lids):,}件", flush=True)

    # 公開状態 (既定では公開終了に転記しない)
    status = {}
    for i in range(0, len(all_lids), 100):
        j = post_retry(f"{BASE}/crm/v3/objects/{LISTING}/batch/read",
                       {"inputs": [{"id": x} for x in all_lids[i:i + 100]],
                        "properties": ["kyuujin_status", "hs_name"]})
        for o in j.get("results", []):
            status[str(o["id"])] = o.get("properties") or {}
        time.sleep(0.08)

    targets = []
    for did, lids in d2l.items():
        memo, dname = memos[did]
        h = body_hash(memo)
        for lid in lids:
            st = (status.get(lid) or {}).get("kyuujin_status")
            if not a.include_closed and st in SKIP_STATUS:
                continue
            targets.append({"listing_id": lid, "deal_id": did,
                            "deal_name": dname, "hash": h, "memo": memo,
                            "status": st,
                            "job": (status.get(lid) or {}).get("hs_name", "")})
    print(f"転記の候補 {len(targets):,}件 "
          f"(公開終了を除く)" if not a.include_closed else "", flush=True)

    # 既に同じ内容が貼ってあるものは触らない
    print("既存の転記メモを確認中...", flush=True)
    state = listing_transfer_state([t["listing_id"] for t in targets])
    todo, same, update = [], 0, 0
    for t in targets:
        cur = state.get(t["listing_id"])
        if cur and cur["hash"] == t["hash"]:
            same += 1
            continue
        if cur:
            update += 1
            t["replace"] = cur["note_id"]
        todo.append(t)
    print(f"\n=== 転記の内訳 ===")
    print(f"  新規に貼る      : {len(todo) - update:,}件")
    print(f"  貼り直す(更新)  : {update:,}件")
    print(f"  同じ内容でスキップ: {same:,}件")

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"取引メモ転記案_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["求人ID", "求人名", "公開状態", "取引ID", "取引名", "操作"])
        for t in todo:
            w.writerow([t["listing_id"], t["job"], t["status"], t["deal_id"],
                        t["deal_name"], "貼り直し" if t.get("replace") else "新規"])
    print(f"案(CSV): {p.resolve()}")
    if not a.actual:
        print("\n(--actual で転記します)")
        return 0

    created, ok, fail = [], 0, 0
    for n, t in enumerate(todo, 1):
        try:
            body = transfer_body(t["memo"], t["deal_name"], t["hash"])
            # Note→求人の関連タイプは実測で typeId=899 (HUBSPOT_DEFINED)。
            # 推測で書かないこと。/crm/v4/associations/notes/0-420/labels で確認済み。
            res = post_retry(f"{BASE}/crm/v3/objects/notes",
                             {"properties": {
                                 "hs_note_body": body.replace("\n", "<br>"),
                                 "hs_timestamp": datetime.utcnow().strftime(
                                     "%Y-%m-%dT%H:%M:%SZ")},
                              "associations": [{"to": {"id": t["listing_id"]},
                                                "types": [{
                                  "associationCategory": "HUBSPOT_DEFINED",
                                  "associationTypeId": 899}]}]})
            created.append({"note_id": res.get("id"),
                            "listing_id": t["listing_id"]})
            # 古い転記メモを消す (貼り直しの場合)。新しい方を作ってから消す。
            if t.get("replace"):
                requests.delete(f"{BASE}/crm/v3/objects/notes/{t['replace']}",
                                headers=_h(), timeout=30)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ★失敗 listing={t['listing_id']}: "
                  f"{type(e).__name__}: {str(e)[:80]}", flush=True)
        if n % 100 == 0:
            print(f"  {n:,}/{len(todo):,} (成功 {ok:,} / 失敗 {fail:,})", flush=True)
        time.sleep(0.1)
    bk = (_REPO / "data" / "job_application_sync" /
          f"memo_transfer_{datetime.now():%Y%m%dT%H%M%S}.json")
    bk.parent.mkdir(parents=True, exist_ok=True)
    bk.write_text(json.dumps(created, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\n=== 結果 === 転記 {ok:,}件 / 失敗 {fail:,}件")
    print(f"記録: {bk.resolve()}")
    print(f"戻す場合: python {Path(__file__).name} --rollback {bk}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
