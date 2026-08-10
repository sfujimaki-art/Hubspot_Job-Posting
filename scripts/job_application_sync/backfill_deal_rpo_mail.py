# -*- coding: utf-8 -*-
"""取引の RPOアドレス(kanri_mail_address) を補完する (2026-08-06)。

なぜ必要か:
  一次対応の要否・応募者管理シートの特定は、いずれも取引の
  `kanri_mail_address` を起点にする。しかし納品管理PL 3,454件のうち
  値が入っているのは480件(14%)しかなく、大半の経路が繋がっていない。

補完の優先順位 (この順に当てる):
  ① 同じ会社・拠点の**他の取引**に値があれば、それをコピーする
     現場が実際に入れた値なので最も信頼できる。契約更新で取引が
     作り直されると新しい取引が空になるが、値そのものは変わらない。
  ② 顧客管理シートの「リクロジアドレス」列から引く
     実測精度98.8% (正解データ336件で照合し、不一致4件)。
     不一致はシート側の値が実際と食い違っているもので、突合方法の
     問題ではない。→ 出典を必ず記録し、後から検証できるようにする。

やらないこと:
  - 既に値がある取引は触らない (人が入れたものを機械が上書きしない)
  - 候補が複数ある取引は触らない (どれが正しいか機械には決められない)
  - 会社名だけの緩い突合はしない (拠点違いを掴む。実測で別会社を
    引く例があった: ジャパンクリエイト北上営業所 → 出雲中央交通)

使い方:
  python scripts/job_application_sync/backfill_deal_rpo_mail.py            # 案の出力のみ
  python scripts/job_application_sync/backfill_deal_rpo_mail.py --actual   # 書き込み
  python scripts/job_application_sync/backfill_deal_rpo_mail.py --rollback <json>
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
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
os.environ.setdefault("SHEETS_AUTH_MODE", "sa")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from scripts.job_application_sync.hs_paging import (  # noqa: E402
    post_retry, search_all)

BASE = "https://api.hubapi.com"
PIPELINE_NOUHIN = "21596025"
# 取引名の接頭辞 (契約種別+枝番)。会社名+拠点名だけを残すために落とす。
KIND_PREFIX = re.compile(
    r"^(サブスク継続|AirWork広告運用|エントリーフォーム|マーケ関連|請負契約"
    r"|求人追加|一次対応|再契約|紹介料|スポット|単発|年契約|深耕|初回|紹介"
    r"|マーケ|請負)[①-⑳0-9]*[＿_]?")


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}",
            "Content-Type": "application/json"}


def norm(s: str) -> str:
    """会社名+拠点名の比較キー。**拠点名は落とさない**。

    落とすと別拠点・別会社を掴む。実測で「ジャパンクリエイト北上営業所」に
    出雲中央交通のアドレスが当たる例があった。
    """
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    s = re.sub(r"[\s　]", "", s)
    s = re.sub(r"[（(]([^）)]*)[）)]", r"\1", s)   # 括弧は外すが中身は残す
    # ★区切り記号は**消す**。取引名は「株式会社不二家＿平塚工場」のように
    #   会社名と拠点名を ＿ で繋ぐが、顧客管理シートは区切り無しで
    #   「株式会社不二家平塚工場」と書く。＿を _ に変換するだけでは永久に
    #   一致しない (2026-08-10 実測: 管理用メールが空の取引1,805件のうち、
    #   _ を残すと875件しかシートに当たらないが、消すと936件 = +61件)。
    #   _ を消してシート側のキーが衝突する件数は **0** なので、別会社を掴む
    #   恐れは無い。拠点名そのものは落とさない (落とすと別拠点を掴む。上記参照)。
    s = s.replace("＿", "").replace("_", "")
    return s.replace("／", "/").lower()


def deal_base(name: str) -> str:
    return norm(KIND_PREFIX.sub("", str(name or "")).strip())


def load_sheet_index() -> dict:
    """顧客管理シート: 会社名+拠点名 → {RPOアドレス}。"""
    from scripts.job_application_sync import applicant_queue as aq
    from scripts.job_application_sync.fetchers import account_loader as al
    gc = aq._sheets_client()
    av = gc.open_by_key(al.SHEET_ID).sheet1.get_all_values()
    cols = aq._resolve_account_columns(av[0], av[1:])
    idx = defaultdict(set)
    for r in av[1:]:
        if len(r) <= max(cols.values()):
            continue
        comp = r[cols["comp"]].strip()
        rec = r[cols["reclog"]].strip()
        if comp and rec and "@" in rec:
            idx[norm(comp)].add(rec)
    return idx


def _closed_stages() -> set:
    """解約済のステージID。ここへ値を入れても使われないので対象外にする。"""
    r = requests.get(f"{BASE}/crm/v3/pipelines/0-3/{PIPELINE_NOUHIN}",
                     headers=_h(), timeout=30)
    r.raise_for_status()
    return {s["id"] for s in r.json().get("stages", [])
            if "解約" in (s.get("label") or "")}


def build_plan() -> tuple:
    deals = search_all("0-3", ["dealname", "kanri_mail_address", "dealstage",
                               "hubspot_owner_id"],
                       [{"propertyName": "pipeline", "operator": "EQ",
                         "value": PIPELINE_NOUHIN}])
    closed = _closed_stages()
    n_all = len(deals)
    deals = [o for o in deals if o["properties"].get("dealstage") not in closed]
    print(f"納品管理PLの取引 {n_all:,}件 "
          f"(解約済 {n_all - len(deals):,}件を除外 → 対象 {len(deals):,}件)",
          flush=True)
    # ① 既存値の索引 (会社+拠点 → 値)
    by_key = defaultdict(set)
    for o in deals:
        v = (o["properties"].get("kanri_mail_address") or "").strip()
        if v:
            by_key[deal_base(o["properties"].get("dealname"))].add(v)
    sheet = load_sheet_index()
    print(f"  既存値を持つ会社+拠点: {len(by_key):,}種 / "
          f"シート索引: {len(sheet):,}種", flush=True)

    plan, ambiguous = [], []
    for o in deals:
        p = o["properties"]
        if (p.get("kanri_mail_address") or "").strip():
            continue                       # 既に値がある → 触らない
        k = deal_base(p.get("dealname"))
        ex, sh = by_key.get(k), sheet.get(k)
        if ex and len(ex) == 1:
            src, val = "他取引", list(ex)[0]
        elif ex:
            ambiguous.append({"取引ID": o["id"], "取引名": p.get("dealname"),
                              "理由": "他取引の値が複数種",
                              "候補": " | ".join(sorted(ex))})
            continue
        elif sh and len(sh) == 1:
            src, val = "シート", list(sh)[0]
        elif sh:
            ambiguous.append({"取引ID": o["id"], "取引名": p.get("dealname"),
                              "理由": "シートに複数候補",
                              "候補": " | ".join(sorted(sh))})
            continue
        else:
            continue                       # どちらにも無い → 現場に確認
        plan.append({"取引ID": o["id"], "取引名": p.get("dealname"),
                     "ステージ": p.get("dealstage"), "RPOアドレス": val,
                     "出典": src})
    return plan, ambiguous


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--rollback", default="")
    ap.add_argument("--out-dir", default="claudedocs")
    a = ap.parse_args(argv)

    if a.rollback:
        items = json.loads(Path(a.rollback).read_text(encoding="utf-8"))
        print(f"{len(items):,}件を元の値へ戻します", flush=True)
        ok = 0
        for i in range(0, len(items), 100):
            inputs = [{"id": x["取引ID"],
                       "properties": {"kanri_mail_address": x.get("変更前") or ""}}
                      for x in items[i:i + 100]]
            post_retry(f"{BASE}/crm/v3/objects/0-3/batch/update",
                       {"inputs": inputs})
            ok += len(inputs)
            time.sleep(0.15)
        print(f"戻した件数: {ok:,}")
        return 0

    plan, ambiguous = build_plan()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"RPOアドレス補完案_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["取引ID", "取引名", "ステージ",
                                          "RPOアドレス", "出典"])
        w.writeheader()
        w.writerows(plan)
    src_count = defaultdict(int)
    for x in plan:
        src_count[x["出典"]] += 1
    print(f"\n補完できる取引: {len(plan):,}件 "
          f"({dict(src_count)})")
    print(f"人の確認が要る : {len(ambiguous):,}件")
    print(f"案(CSV): {p.resolve()}")
    if ambiguous:
        q = out / f"RPOアドレス要確認_{datetime.now():%Y-%m-%d}.csv"
        with q.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["取引ID", "取引名", "理由", "候補"])
            w.writeheader()
            w.writerows(ambiguous)
        print(f"要確認(CSV): {q.resolve()}")
    if not a.actual:
        print("\n(--actual で書き込みます)")
        return 0

    # 変更前の値を記録してから書き込む
    backup = [{"取引ID": x["取引ID"], "変更前": "", "変更後": x["RPOアドレス"],
               "出典": x["出典"]} for x in plan]
    bk = (_REPO / "data" / "job_application_sync" /
          f"rpo_backfill_{datetime.now():%Y%m%dT%H%M%S}.json")
    bk.parent.mkdir(parents=True, exist_ok=True)
    bk.write_text(json.dumps(backup, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    ok = fail = 0
    for i in range(0, len(plan), 100):
        chunk = plan[i:i + 100]
        try:
            post_retry(f"{BASE}/crm/v3/objects/0-3/batch/update",
                       {"inputs": [
                           {"id": x["取引ID"],
                            "properties": {
                                "kanri_mail_address": x["RPOアドレス"]}}
                           for x in chunk]})
            ok += len(chunk)
        except Exception as e:  # noqa: BLE001
            fail += len(chunk)
            print(f"  ★失敗: {type(e).__name__}: {str(e)[:140]}")
        print(f"  {min(i + 100, len(plan)):,}/{len(plan):,}", flush=True)
        time.sleep(0.15)
    print(f"\n=== 結果 === 書込 {ok:,}件 / 失敗 {fail:,}件")
    print(f"変更の記録: {bk.resolve()}")
    print(f"戻す場合: python {Path(__file__).name} --rollback {bk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
