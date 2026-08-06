# -*- coding: utf-8 -*-
"""求人LISTINGのパイプラインステージを kyuujin_status に揃える (2026-08-05)。

背景:
  求人の状態は3系統で並行管理されていた。実測(2026-08-05)では
    - kyuujin_status : 日次の媒体CSV取込で機械更新。**唯一の生きたSSOT**
                       (公開中2,358件は100%、公開終了は98.5%が当日CSVで裏付け)
    - hs_pipeline_stage : 2026-06-01のワンオフ実行以降、凍結。94%が未設定
    - shukkousuteetasu  : 完全未使用(0件)
  そのため「公開終了なのにボード上は募集中」等の食い違いが1,083件あった。
  本スクリプトは kyuujin_status を正としてステージを揃える。

対応表:
  公開前 → 募集準備中 / 公開中 → 募集中 / 公開終了 → 募集停止

保護ルール (既存値を壊さない):
  - 「選考進行中」「採用決定」に居る求人は**上書きしない**
    (人が動かした採用プロセスを機械が踏み潰さないため。現状0件だが将来のため)
  - kyuujin_status が未設定の求人は触らない (正が無いものを推測で動かさない)
  - 実行前に変更前の値を全件JSONへ保存 (ロールバック可能)

使い方:
  python scripts/job_listing_hubspot_match/sync_listing_stage.py            # dry-run
  python scripts/job_listing_hubspot_match/sync_listing_stage.py --actual   # 本番
  python scripts/job_listing_hubspot_match/sync_listing_stage.py --rollback <backup.json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent.parent
load_dotenv(_REPO / ".env")

BASE = "https://api.hubapi.com"
PIPELINE = "t_5f0228538e8b269ecd3b553b104c008c"      # 求人票管理
STAGE = {"公開前": "1343099546", "公開中": "1343099547", "公開終了": "1343099550"}
LABEL = {"1343099546": "募集準備中", "1343099547": "募集中",
         "1343099548": "選考進行中", "1343099549": "採用決定",
         "1343099550": "募集停止"}
PROTECTED = ("1343099548", "1343099549")            # 人が動かす領域は触らない


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _req(method: str, url: str, **kw):
    """429/5xx/一時断をリトライ。失敗は例外で顕在化させる(黙って進めない)。"""
    for i in range(5):
        try:
            r = requests.request(method, url, headers=_h(), timeout=90, **kw)
        except requests.RequestException:
            if i == 4:
                raise
            time.sleep(2 ** i * 2)
            continue
        if r.status_code in (200, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < 4:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


def fetch_all() -> dict:
    out, after = {}, None
    while True:
        p = {"limit": 100, "archived": "false",
             "properties": "kyuujin_status,hs_pipeline,hs_pipeline_stage,hs_name"}
        if after:
            p["after"] = after
        j = _req("GET", f"{BASE}/crm/v3/objects/0-420", params=p)
        for o in j.get("results", []):
            out[o["id"]] = o.get("properties") or {}
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if len(out) % 10000 < 100:
            print(f"  取得 {len(out):,}件...", flush=True)
        if not after:
            break
        time.sleep(0.08)
    return out


def plan(rows: dict) -> tuple:
    targets, stats = [], Counter()
    for lid, p in rows.items():
        st = p.get("kyuujin_status")
        cur = p.get("hs_pipeline_stage")
        if st not in STAGE:
            stats["対象外: kyuujin_status未設定"] += 1
            continue
        want = STAGE[st]
        if cur == want:
            stats["変更不要(既に一致)"] += 1
            continue
        if cur in PROTECTED:
            stats[f"保護: {LABEL[cur]}のため上書きしない"] += 1
            continue
        stats[f"{LABEL.get(cur, '(未設定)')} → {LABEL[want]}"] += 1
        targets.append({"id": lid, "before": cur, "after": want,
                        "status": st, "name": (p.get("hs_name") or "")[:40]})
    return targets, stats


def apply(targets: list, backup_path: Path) -> dict:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps(
        [{"id": t["id"], "before": t["before"], "before_label":
          LABEL.get(t["before"], "(未設定)")} for t in targets],
        ensure_ascii=False), encoding="utf-8")
    print(f"[backup] 変更前の値を保存: {backup_path} ({len(targets):,}件)", flush=True)
    ok = fail = 0
    for i in range(0, len(targets), 100):
        ch = targets[i:i + 100]
        body = {"inputs": [{"id": t["id"], "properties": {
            "hs_pipeline": PIPELINE, "hs_pipeline_stage": t["after"]}} for t in ch]}
        try:
            _req("POST", f"{BASE}/crm/v3/objects/0-420/batch/update", json=body)
            ok += len(ch)
        except Exception as e:  # noqa: BLE001
            fail += len(ch)
            print(f"  ★失敗 {i}-{i+len(ch)}: {type(e).__name__}: {str(e)[:100]}",
                  flush=True)
        if (i // 100) % 30 == 0:
            print(f"  更新 {i + len(ch):,}/{len(targets):,} (成功{ok:,})", flush=True)
        time.sleep(0.12)
    return {"ok": ok, "fail": fail}


def rollback(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"[rollback] {len(data):,}件を元に戻します", flush=True)
    ok = 0
    for i in range(0, len(data), 100):
        ch = data[i:i + 100]
        inputs = []
        for d in ch:
            props = ({"hs_pipeline_stage": d["before"]} if d["before"]
                     else {"hs_pipeline_stage": ""})
            inputs.append({"id": d["id"], "properties": props})
        _req("POST", f"{BASE}/crm/v3/objects/0-420/batch/update",
             json={"inputs": inputs})
        ok += len(ch)
        time.sleep(0.12)
    return {"restored": ok}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true", help="本番実行(既定はdry-run)")
    ap.add_argument("--rollback", default="", help="バックアップJSONから復元")
    ap.add_argument("--out-dir", default="data/job_application_sync")
    a = ap.parse_args(argv)
    if a.rollback:
        print(rollback(Path(a.rollback)))
        return
    print("LISTING全件を取得中...", flush=True)
    rows = fetch_all()
    print(f"LISTING {len(rows):,}件\n", flush=True)
    targets, stats = plan(rows)
    print("=== 変更計画 ===")
    for k, n in stats.most_common():
        print(f"   {n:7,}件  {k}")
    print(f"\n★更新対象 = {len(targets):,}件  (dry_run={not a.actual})")
    for t in targets[:5]:
        print(f"   例: {t['id']} {t['name'][:26]!r} "
              f"{LABEL.get(t['before'], '(未設定)')} → {LABEL[t['after']]}")
    if not a.actual:
        print("\n(dry-run のため書き込みません。--actual で実行)")
        return
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bk = Path(a.out_dir) / f"stage_backup_{stamp}.json"
    res = apply(targets, bk)
    print(f"\n=== 結果 === 成功 {res['ok']:,}件 / 失敗 {res['fail']:,}件")
    print(f"ロールバック: python {sys.argv[0]} --rollback {bk}")


if __name__ == "__main__":
    main()
