# -*- coding: utf-8 -*-
"""取引の暗黙知メモ → 配下の求人へ転記する (2026-08-06、2026-08-31 作り直し)。

## なぜこの向きなのか

求人は「新着」を取るためクローズ→出し直しで作り替えられる。旧新を結ぶIDが
無く、名前の類似度でも66%しか対応付かないため、**求人にメモを置くと
出し直すたびに失われる**。そこで**メモの正を親の取引に置き**、求人はそこから
転記を受けるだけにする。

    取引(正)  ──転記──>  求人  ──既存のコピー──>  応募

## 2026-08-31 に作り直した理由

旧実装は毎晩 1,800件前後の転記Noteを作り、同じ求人に最大45件が溜まっていた。
原因は3つ重なっていた (深掘り5回+逆証明5回で確定):

  1. 冪等判定の印 `<!--memo:hash-->` を本文に埋めていたが、**HubSpot が保存時に
     HTMLコメントを消す**。転記Note 5,117件で残存0件。比較が常に不一致になり、
     CIログ8晩で「同じ内容でスキップ」は毎晩0件だった。
  2. 貼り直しが **POST(新規作成)+DELETE(削除)** で、しかも (取引, 求人) の組ごとに
     作っていた。求人が「跡地」と「生存」の両方の取引に紐づき両方がメモを持つと、
     一晩に N件作って古い1件しか消さない → 純増 +(N-1)/晩。08-21 の契約またぎ
     継承で跡地+生存の両方がメモを持つ求人が 323件に急増し、増殖が顕在化した。
  3. 削除の戻り値を見ておらず、失敗しても「成功」と数えていた。

作り直しの3点:

  ① **1求人=1本文**。親取引が複数でも本文を1つに決める。生きている取引を優先し、
     跡地は生存が無いときだけ使う (relink 未了の求人にもメモを届けるため)。
     生存が複数で本文が違えば merge_group で両方残す (契約またぎ継承と同じ部品)。
     **別の取引先コードの取引が混在する求人は書かずに人へ回す**。実測6件で、
     過去に別々の顧客の取引へ紐づけられた履歴の残骸。merge すると A社の足切り
     基準が B社の求人に載り、一次対応が誤った条件で電話する。
  ② **既存の転記Noteがあれば PATCH(更新)**。無いときだけ作る。削除しない。
     集約側 (rollup_memo_to_deal) と同じ部品 (notes.patch_note)。
  ③ **本文で比較する**。印を捨て、署名・出典・注意書き・HTML装飾・空白を落とした
     本体同士を比べる。1文字違えば「違う」、HubSpot の変形は吸収する (実測)。

模擬実行 (2026-08-31): 公開中1,419求人 → 変化なし 1,417 / PATCH 2 / 新規 0。
旧実装は同じ晩に 1,790件を作っていた。

## 掃除 (--cleanup)

溜まった余剰 (求人ごとに1件を残して他) は、この日次処理では消さない。
`--cleanup` を付けた手動実行だけが消す。削除は戻せないので、人が結果を見てから
流す。消す前に本文を JSON へ退避し、**変更履歴に人の編集 (CRM_UI) があれば消さず
に報告する** (実測で1件あった。作成時のプロパティには現れず履歴でしか分からない)。
応募へのコピー (COPIED_NOTE_MARKER) は同じ署名を含むが別物なので触らない。

## やらないこと

- 人が求人に直接書いたメモは消さない。転記メモは別のNoteとして扱う
- 公開終了の求人には転記しない (応募が来ないため。既定。--include-closed で変更可)
- 別コードが混在する求人には書かない (人へ回す)

使い方:
  python scripts/job_application_sync/sync_deal_memo_to_listing.py                     # 確認のみ
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --actual            # 転記 (作成/更新)
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --cleanup           # 余剰の確認のみ
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --cleanup --actual  # 余剰を削除
  python scripts/job_application_sync/sync_deal_memo_to_listing.py --rollback <json>
"""
from __future__ import annotations

import argparse
import csv
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

from scripts.job_application_sync import deal_stages as DS       # noqa: E402
from scripts.job_application_sync.hs_paging import (              # noqa: E402
    post_retry, search_all_by_id)
from scripts.job_application_sync.notes import (                  # noqa: E402
    COPIED_NOTE_MARKER, ROLLUP_SIGNATURE, TRANSFER_SIGNATURE,
    human_edited, patch_note)
from scripts.job_application_sync.rollup_memo_to_deal import (    # noqa: E402
    strip_html)
from scripts.job_application_sync.rollup_merge import merge_group  # noqa: E402

BASE = "https://api.hubapi.com"
LISTING = "0-420"
DEAL = "0-3"
# 応募が来ない求人には転記しない (既定)
SKIP_STATUS = ("公開終了",)
# Note→求人の関連タイプは実測で typeId=899 (HUBSPOT_DEFINED)。推測で書かないこと。
NOTE_TO_LISTING_TYPE = 899
NOTICE = ("この内容は取引レコードで管理されています。"
          "修正は取引側で行ってください（求人側で直しても次回上書きされます）。")
LOG_DIR = _REPO / "data" / "job_application_sync"


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# 本文の組み立てと比較 (純関数)
# --------------------------------------------------------------------------
def transfer_body(deal_memo: str, source: str) -> str:
    """求人へ貼る本文。署名・出典・注意書きを付ける。印は埋めない (消されるため)。"""
    inner = (deal_memo or "").replace(ROLLUP_SIGNATURE, "").lstrip("\n")
    inner = inner.replace("この取引に紐づく求人へ自動転記されます。", "").lstrip("\n")
    return f"{TRANSFER_SIGNATURE}\n出典: {source}\n{NOTICE}\n\n{inner}"


_NOTICE_RE = re.compile(
    r"この内容は取引レコードで管理されています。.*?次回上書きされます）。", re.S)


def transfer_core(body: str) -> str:
    """比較用に正規化した本体。

    署名・出典行・注意書き・旧実装の印・HTML装飾・空白の違いを落とす。
    「出典」は取引名なので、同じ内容でも親が変われば変わる。それで「違う」と
    判定すると毎晩書き直すことになるため、比較からは外す。
    """
    t = strip_html(body or "")
    t = re.sub(r"<!--memo:[0-9a-f]+-->", "", t)
    t = t.replace(TRANSFER_SIGNATURE, "")
    t = re.sub(r"^.*出典:.*$", "", t, flags=re.M)
    t = _NOTICE_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------
# HubSpot から読む
# --------------------------------------------------------------------------
def _batch(url: str, ids: list, **extra) -> list:
    out = []
    for i in range(0, len(ids), 100):
        j = post_retry(url, {"inputs": [{"id": x} for x in ids[i:i + 100]], **extra})
        out += j.get("results") or []
        time.sleep(0.05)
    return out


def collect_deal_memos() -> dict:
    """集約メモを持つ取引 → {deal_id: {memo, name, code, live, stage}}。

    ★search は hs_object_id カーソルで回す (search_all_by_id)。after 方式は
      10,000件で HTTP 400 になり、rollup_memo_to_deal が4晩落ちた原因と同じ。
    """
    notes = [n for n in search_all_by_id(
                 "notes", ["hs_note_body"],
                 [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
                   "value": "暗黙知メモ"}])
             if ROLLUP_SIGNATURE in ((n.get("properties") or {}).get("hs_note_body") or "")]
    print(f"集約メモ(Note) {len(notes):,}件", flush=True)
    if not notes:
        return {}
    nid2deal = {}
    for res in _batch(f"{BASE}/crm/v4/associations/notes/{DEAL}/batch/read",
                      [n["id"] for n in notes]):
        to = res.get("to") or []
        if to:
            nid2deal[str(res["from"]["id"])] = str(to[0]["toObjectId"])
    dids = sorted(set(nid2deal.values()))
    deal = {}
    for o in _batch(f"{BASE}/crm/v3/objects/{DEAL}/batch/read", dids,
                    properties=["dealname", "dealstage", DS.PROP_CODE]):
        deal[str(o["id"])] = o.get("properties") or {}
    out = {}
    for n in notes:
        did = nid2deal.get(n["id"])
        if not did or did not in deal:
            continue
        p = deal[did]
        raw = (n.get("properties") or {}).get("hs_note_body") or ""
        out[did] = {"memo": raw.replace("<br>", "\n"),
                    "name": p.get("dealname") or "",
                    "code": (p.get(DS.PROP_CODE) or "").strip(),
                    "live": DS.is_writable(p.get("dealstage")),
                    "stage": DS.label(p.get("dealstage"))}
    print(f"  取引に紐づいたもの: {len(out):,}件 "
          f"(生存 {sum(1 for v in out.values() if v['live']):,})", flush=True)
    return out


def deal_listings(deal_ids: list) -> dict:
    d2l = {}
    for res in _batch(f"{BASE}/crm/v4/associations/{DEAL}/{LISTING}/batch/read", deal_ids):
        d2l[str(res["from"]["id"])] = [str(t["toObjectId"]) for t in (res.get("to") or [])]
    return d2l


def listing_status(listing_ids: list) -> dict:
    st = {}
    for o in _batch(f"{BASE}/crm/v3/objects/{LISTING}/batch/read", listing_ids,
                    properties=["kyuujin_status", "hs_name"]):
        p = o.get("properties") or {}
        st[str(o["id"])] = {"status": p.get("kyuujin_status"), "name": p.get("hs_name") or ""}
    return st


def is_transfer_note(body: str) -> bool:
    """③取引→求人の転記メモか。

    ★応募へのコピー (②) は転記本文を丸ごと複製するので同じ署名を含む。
      COPIED_NOTE_MARKER で除外しないと、②を余剰と誤認して消してしまう
      (実測: 署名を持つ 5,512件のうち 395件が②だった)。
    """
    b = body or ""
    return TRANSFER_SIGNATURE in b and COPIED_NOTE_MARKER not in b


def listing_transfer_notes(listing_ids: list) -> dict:
    """求人 → [{note_id, body, created}] (③転記メモだけ・作成日昇順)。"""
    pairs = []
    for res in _batch(f"{BASE}/crm/v4/associations/{LISTING}/notes/batch/read", listing_ids):
        lid = str(res["from"]["id"])
        for t in (res.get("to") or []):
            pairs.append((lid, str(t["toObjectId"])))
    nids = sorted({n for _l, n in pairs})
    info = {}
    for o in _batch(f"{BASE}/crm/v3/objects/notes/batch/read", nids,
                    properties=["hs_note_body", "hs_createdate"]):
        p = o.get("properties") or {}
        info[str(o["id"])] = {"body": p.get("hs_note_body") or "",
                              "created": p.get("hs_createdate") or ""}
    out = defaultdict(list)
    for lid, nid in pairs:
        it = info.get(nid)
        if it and is_transfer_note(it["body"]):
            out[lid].append({"note_id": nid, **it})
    for lid in out:
        out[lid].sort(key=lambda x: x["created"])
    return dict(out)


# --------------------------------------------------------------------------
# 計画 (純関数・テスト対象)
# --------------------------------------------------------------------------
def plan_transfers(memos: dict, d2l: dict, status: dict, notes_by_listing: dict,
                   include_closed: bool = False, today: str = "") -> dict:
    """求人ごとに「何を・どのNoteに」書くかを決める。API は呼ばない。

    memos            : collect_deal_memos() の戻り
    d2l              : {deal_id: [listing_id]}
    status           : {listing_id: {"status","name"}}
    notes_by_listing : {listing_id: [{note_id, body, created}]}  ③転記メモだけ

    戻り: {"create": [...], "patch": [...], "nochange": [lid], "deferred": [...],
           "surplus": {lid: [note_id]}, "skipped_closed": int}
    """
    today = today or f"{datetime.now():%Y-%m-%d}"
    l2d = defaultdict(list)
    for d, ls in d2l.items():
        if d not in memos:
            continue
        for l in ls:
            l2d[l].append(d)

    plan = {"create": [], "patch": [], "nochange": [], "deferred": [],
            "surplus": {}, "skipped_closed": 0}
    for lid in sorted(l2d):
        st = (status.get(lid) or {}).get("status")
        if not include_closed and st in SKIP_STATUS:
            plan["skipped_closed"] += 1
            continue
        parents = l2d[lid]
        # ★生きている取引を優先。跡地は生存が無いときだけ (relink 未了の求人用)。
        live = [d for d in parents if memos[d]["live"]]
        src = live or parents
        codes = {memos[d]["code"] for d in src}
        if len(codes) > 1:
            # ★別契約のメモを1つの求人に混ぜない。人へ回す。
            plan["deferred"].append({
                "listing_id": lid,
                "求人名": (status.get(lid) or {}).get("name", ""),
                "理由": "別の取引先コードの取引が混在",
                "取引": [{"deal_id": d, "取引名": memos[d]["name"],
                          "取引先コード": memos[d]["code"], "ステージ": memos[d]["stage"]}
                         for d in src]})
            continue
        distinct = {}
        for d in src:
            distinct.setdefault(transfer_core(memos[d]["memo"]), d)
        if len(distinct) == 1:
            d = next(iter(distinct.values()))
            body = transfer_body(memos[d]["memo"], memos[d]["name"])
        else:
            # 同じ契約で本文が違う (生存が複数・片方だけ更新など)。省略せず両方残す。
            merged = merge_group(
                [{"name": memos[d]["name"], "body": memos[d]["memo"]} for d in src], today)
            body = transfer_body(merged, " / ".join(memos[d]["name"] for d in src))
        want = transfer_core(body)

        rec = {"listing_id": lid,
               "求人名": (status.get(lid) or {}).get("name", ""),
               "公開状態": st,
               "出典取引": list(src),
               "body": body}
        existing = notes_by_listing.get(lid) or []
        if not existing:
            plan["create"].append(rec)
            continue
        match = [n for n in existing if transfer_core(n["body"]) == want]
        # 残す1件: 既に一致しているものがあればそれ。無ければ最新 (積み上げ式なので
        # 最新が現在の内容に最も近い)。残さなかった分は掃除の対象。
        keep = match[0] if match else max(existing, key=lambda n: n.get("created") or "")
        surplus = [n["note_id"] for n in existing if n["note_id"] != keep["note_id"]]
        if surplus:
            plan["surplus"][lid] = surplus
        if match:
            plan["nochange"].append(lid)
        else:
            plan["patch"].append({**rec, "note_id": keep["note_id"],
                                  "before_body": keep["body"]})
    return plan


# --------------------------------------------------------------------------
# HubSpot へ書く
# --------------------------------------------------------------------------
def _create_note(listing_id: str, body: str) -> str:
    res = post_retry(f"{BASE}/crm/v3/objects/notes", {
        "properties": {"hs_note_body": body.replace("\n", "<br>"),
                       "hs_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        "associations": [{"to": {"id": listing_id},
                          "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                     "associationTypeId": NOTE_TO_LISTING_TYPE}]}]})
    return str(res.get("id"))


def _delete_note(note_id: str, retries: int = 4) -> str:
    """削除する。戻り: "deleted" | "already_gone"。失敗は例外 (黙って成功扱いにしない)。"""
    for i in range(retries + 1):
        try:
            r = requests.delete(f"{BASE}/crm/v3/objects/notes/{note_id}",
                                headers=_h(), timeout=30)
        except requests.RequestException:
            if i < retries:
                time.sleep(2 ** i)
                continue
            raise
        if r.status_code in (200, 204):
            return "deleted"
        if r.status_code == 404:
            return "already_gone"
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"DELETE Note {note_id} HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError(f"DELETE Note {note_id} retry exhausted")


def _write_log(name: str, payload: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = LOG_DIR / f"{name}_{datetime.now():%Y%m%dT%H%M%S}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def apply_transfers(plan: dict) -> dict:
    """create/patch を実行。PATCH 前に人の編集があれば止めて報告する。"""
    created, patched, human, failed = [], [], [], []
    for n, rec in enumerate(plan["create"], 1):
        try:
            nid = _create_note(rec["listing_id"], rec["body"])
            created.append({"note_id": nid, "listing_id": rec["listing_id"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"listing_id": rec["listing_id"], "op": "create",
                           "error": f"{type(e).__name__}: {str(e)[:120]}"})
        if n % 100 == 0:
            print(f"  作成 {n:,}/{len(plan['create']):,}", flush=True)
        time.sleep(0.1)
    for n, rec in enumerate(plan["patch"], 1):
        try:
            if human_edited(rec["note_id"]):
                # ★本文に「求人側で直しても上書きされます」と書いてはあるが、
                #   実際に直した人がいたら黙って潰さず報告する (実測で1件あった)。
                human.append({"listing_id": rec["listing_id"], "note_id": rec["note_id"],
                              "求人名": rec.get("求人名", "")})
                continue
            patch_note(rec["note_id"], rec["body"])
            patched.append({"note_id": rec["note_id"], "listing_id": rec["listing_id"],
                            "before_body": rec["before_body"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"listing_id": rec["listing_id"], "op": "patch",
                           "note_id": rec["note_id"],
                           "error": f"{type(e).__name__}: {str(e)[:120]}"})
        if n % 100 == 0:
            print(f"  更新 {n:,}/{len(plan['patch']):,}", flush=True)
        time.sleep(0.1)
    return {"created": created, "patched": patched, "human_edited": human, "failed": failed}


def apply_cleanup(plan: dict, notes_by_listing: dict, actual: bool) -> dict:
    """余剰 (求人ごとに残す1件以外) を消す。人の編集があるものは消さない。"""
    body_of = {n["note_id"]: n["body"]
               for ns in notes_by_listing.values() for n in ns}
    deleted, human, failed = [], [], []
    total = sum(len(v) for v in plan["surplus"].values())
    n = 0
    for lid, nids in plan["surplus"].items():
        for nid in nids:
            n += 1
            try:
                if human_edited(nid):
                    human.append({"listing_id": lid, "note_id": nid})
                    continue
                if actual:
                    r = _delete_note(nid)
                    deleted.append({"note_id": nid, "listing_id": lid,
                                    "body": body_of.get(nid, ""), "result": r})
            except Exception as e:  # noqa: BLE001
                failed.append({"listing_id": lid, "note_id": nid,
                               "error": f"{type(e).__name__}: {str(e)[:120]}"})
            if n % 200 == 0:
                print(f"  掃除 {n:,}/{total:,}", flush=True)
            time.sleep(0.08)
    return {"deleted": deleted, "human_edited": human, "failed": failed, "total": total}


def rollback(path: str) -> int:
    """--actual で書いた分を戻す。作成→削除 / 更新→前の本文へ / 削除→作り直し。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    n_del = n_pat = n_rec = 0
    for it in data.get("created", []):
        _delete_note(it["note_id"])
        n_del += 1
        time.sleep(0.08)
    for it in data.get("patched", []):
        patch_note(it["note_id"], it["before_body"].replace("<br>", "\n"))
        n_pat += 1
        time.sleep(0.08)
    for it in data.get("deleted", []):
        _create_note(it["listing_id"], it["body"].replace("<br>", "\n"))
        n_rec += 1
        time.sleep(0.1)
    print(f"ロールバック: 作成分を削除 {n_del} / 更新分を戻す {n_pat} / 削除分を作り直し {n_rec}")
    return 0


def _print_deferred(deferred: list) -> None:
    if not deferred:
        return
    print(f"\n★ 人へ回す (書いていません): {len(deferred)}件")
    for d in deferred[:10]:
        print(f"   求人 {d['listing_id']} {d.get('求人名', '')[:28]}  {d['理由']}")
        for t in d["取引"]:
            print(f"      取引 {t['deal_id']} {t['取引先コード'] or '(コード無し)'} "
                  f"{t['取引名'][:28]} [{t['ステージ']}]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    # 手動 dispatch の試し実行は $FLAG="--dry-run" で呼ぶ。既定が dry なので何もしない
    # が、受け取れないと argparse が exit 2 で落ちる (旧版も同じ穴があった)。
    ap.add_argument("--dry-run", action="store_true", help="既定 (書き込まない)")
    ap.add_argument("--include-closed", action="store_true", help="公開終了の求人にも転記する")
    ap.add_argument("--cleanup", action="store_true",
                    help="余剰の転記メモを消す (--actual が無ければ確認のみ)")
    ap.add_argument("--rollback", default="")
    ap.add_argument("--out-dir", default="claudedocs")
    a = ap.parse_args(argv)
    if a.rollback:
        return rollback(a.rollback)

    memos = collect_deal_memos()
    if not memos:
        print("集約メモがありません。先に rollup_memo_to_deal.py を実行してください")
        return 1
    d2l = deal_listings(sorted(memos))
    lids = sorted({x for v in d2l.values() for x in v})
    print(f"配下の求人 {len(lids):,}件", flush=True)
    status = listing_status(lids)
    targets = [l for l in lids if a.include_closed or
               (status.get(l) or {}).get("status") not in SKIP_STATUS]
    print(f"転記の対象 {len(targets):,}件 (公開終了を除く)", flush=True)
    print("既存の転記メモを確認中...", flush=True)
    notes_by = listing_transfer_notes(targets)
    plan = plan_transfers(memos, d2l, status, notes_by, include_closed=a.include_closed)

    print(f"\n=== 転記の内訳 ===")
    print(f"  新規に貼る        : {len(plan['create']):,}件")
    print(f"  更新する(PATCH)   : {len(plan['patch']):,}件")
    print(f"  同じ内容で触らない: {len(plan['nochange']):,}件")
    print(f"  人へ回す          : {len(plan['deferred']):,}件")
    n_sur = sum(len(v) for v in plan["surplus"].values())
    print(f"  余剰(掃除対象)    : {n_sur:,}件 / {len(plan['surplus']):,}求人")
    _print_deferred(plan["deferred"])

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"取引メモ転記案_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["求人ID", "求人名", "公開状態", "操作", "出典取引", "既存Note"])
        for r in plan["create"]:
            w.writerow([r["listing_id"], r["求人名"], r["公開状態"], "新規", " / ".join(r["出典取引"]), ""])
        for r in plan["patch"]:
            w.writerow([r["listing_id"], r["求人名"], r["公開状態"], "更新", " / ".join(r["出典取引"]), r["note_id"]])
        for r in plan["deferred"]:
            w.writerow([r["listing_id"], r["求人名"], "", "人へ回す", r["理由"], ""])
    print(f"案(CSV): {p.resolve()}")

    rc = 0
    if a.cleanup:
        print(f"\n=== 掃除 ({'削除します' if a.actual else '確認のみ'}) ===")
        res = apply_cleanup(plan, notes_by, actual=a.actual)
        print(f"  対象 {res['total']:,} / 削除 {len(res['deleted']):,} / "
              f"人の編集あり(残す) {len(res['human_edited']):,} / 失敗 {len(res['failed']):,}")
        if a.actual and (res["deleted"] or res["failed"]):
            lp = _write_log("memo_cleanup", res)
            print(f"  記録: {lp.resolve()}\n  戻す場合: python {Path(__file__).name} --rollback {lp}")
        if res["failed"]:
            rc = 1
        return rc

    if not a.actual:
        print("\n(--actual で転記します。余剰の掃除は --cleanup)")
        return 0
    res = apply_transfers(plan)
    print(f"\n=== 結果 === 作成 {len(res['created']):,} / 更新 {len(res['patched']):,} / "
          f"人の編集あり(見送り) {len(res['human_edited']):,} / 失敗 {len(res['failed']):,}")
    for h in res["human_edited"][:10]:
        print(f"   ★求人 {h['listing_id']} {h.get('求人名', '')[:28]} の転記Note {h['note_id']} に人の編集。上書きせず")
    for f_ in res["failed"][:10]:
        print(f"   ★失敗 {f_}")
    if res["created"] or res["patched"]:
        lp = _write_log("memo_transfer", res)
        print(f"記録: {lp.resolve()}\n戻す場合: python {Path(__file__).name} --rollback {lp}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
