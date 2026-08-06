# -*- coding: utf-8 -*-
"""求人を「最新の取引」へ紐付け直す (2026-08-06)。

背景:
  取引は契約期間の単位で作られ、継続のたびに新しい取引が生まれる。
  求人が古い取引にぶら下がったままなのは問題ないが、**最新の取引に
  1件も求人が紐付いていない**のは業務上あってはならない (ユーザー確定)。
  既存の sync_deal_association.py は「既に何らかの取引に紐付いている求人」を
  無条件スキップするため、この状態は自動では永久に直らなかった。

方針 (ユーザー確定 2026-08-06):
  - 系列 = (会社, 契約種別, 事業所名)。契約種別が違えば別系列として扱う
    ※会社だけで束ねると別事業所の取引を新旧と誤認する (サカイ引越センターの
      びわこ/滋賀/京都北支社が同日契約、等の実例あり)
  - 系列内の順序は枝番: 新規 < ① < ② < …
  - **追加のみ。古い取引の紐付けは外さない**
  - 順序が決められない系列は**機械で触らず Slack で人に通知**

使い方:
  python scripts/job_application_sync/relink_to_latest_deal.py            # dry-run
  python scripts/job_application_sync/relink_to_latest_deal.py --actual   # 本番
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
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


try:
    from . import deal_series as ds
except ImportError:  # pragma: no cover — CIはスクリプト直実行
    import deal_series as ds  # type: ignore

BASE = "https://api.hubapi.com"
PIPELINE_NOUHIN = "21596025"          # リクロジ_納品管理
KAIYAKU_STAGES = {"1016664339", "90598807", "52016159", "52016158"}


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _req(method: str, url: str, **kw):
    for i in range(5):
        try:
            r = requests.request(method, url, headers=_h(), timeout=90, **kw)
        except requests.RequestException:
            if i == 4:
                raise
            time.sleep(2 ** i * 2)
            continue
        if r.status_code in (200, 201, 204, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < 4:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


def slack_notify(message: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[slack(dry-run,未送信)] {message[:200]}", flush=True)
        return False
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:200]}", flush=True)
        return False
    try:
        return requests.post(url, json={"text": message}, timeout=15).status_code == 200
    except requests.RequestException as e:
        print(f"[slack送信失敗] {e}", flush=True)
        return False


def collect() -> tuple:
    """納品管理PLの取引・会社紐付け・求人紐付けを取得。"""
    deals, after = {}, None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ",
             "value": PIPELINE_NOUHIN}]}],
            "properties": ["dealname", "dealstage", "createdate",
                           "contract_start_date"],
            "limit": 100,
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}]}
        if after:
            b["after"] = after
        r = _req("POST", f"{BASE}/crm/v3/objects/0-3/search", json=b)
        res = r.get("results", [])
        if not res:
            break
        for o in res:
            deals[o["id"]] = o.get("properties") or {}
        after = r.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.12)
    did = sorted(deals)
    d2c, d2l = {}, defaultdict(list)
    for i in range(0, len(did), 100):
        ch = did[i:i + 100]
        r = _req("POST", f"{BASE}/crm/v4/associations/0-3/0-2/batch/read",
                 json={"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            to = res.get("to") or []
            if to:
                d2c[str(res["from"]["id"])] = str(to[0]["toObjectId"])
        r = _req("POST", f"{BASE}/crm/v4/associations/0-3/0-420/batch/read",
                 json={"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            for t in (res.get("to") or []):
                d2l[str(res["from"]["id"])].append(str(t["toObjectId"]))
        time.sleep(0.12)
    return deals, d2c, d2l


def plan(deals: dict, d2c: dict, d2l: dict) -> tuple:
    """(付け替え対象, 人手確認が要る系列, 統計) を返す。"""
    series = defaultdict(list)
    for d, c in d2c.items():
        series[ds.series_key(c, deals[d].get("dealname"))].append(d)
    targets, ambiguous, stat = [], [], Counter()
    for key, members in series.items():
        if len(members) < 2:
            stat["単発(判定不要)"] += 1
            continue
        latest, amb = ds.pick_latest(members, deals)
        if amb:
            stat["★順序が決められない(人手確認)"] += 1
            ambiguous.append({
                "company": key[0], "kind": key[1] or "(新規)", "branch": key[2],
                "deals": [{"id": d, "name": deals[d].get("dealname"),
                           "created": (deals[d].get("createdate") or "")[:10],
                           "jobs": len(d2l.get(d, []))} for d in members]})
            continue
        if deals[latest].get("dealstage") in KAIYAKU_STAGES:
            stat["最新が解約済(対象外)"] += 1
            continue
        if d2l.get(latest):
            stat["最新に求人あり(正常)"] += 1
            continue
        old_jobs = [j for d in members if d != latest for j in d2l.get(d, [])]
        if not old_jobs:
            stat["旧にも求人なし(対象外)"] += 1
            continue
        stat["★付け替え対象"] += 1
        targets.append({"latest_deal": latest,
                        "latest_name": deals[latest].get("dealname"),
                        "kind": key[1] or "(新規)", "branch": key[2],
                        "jobs": sorted(set(old_jobs))})
    return targets, ambiguous, stat


def apply(targets: list, out_dir: Path) -> dict:
    """最新取引へ求人を紐付ける (追加のみ・旧の紐付けは残す)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bk = out_dir / f"relink_backup_{stamp}.json"
    bk.write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
    print(f"[backup] 付け替え内容を保存: {bk}", flush=True)
    ok = fail = 0
    for t in targets:
        for lid in t["jobs"]:
            try:
                _req("PUT",
                     f"{BASE}/crm/v4/objects/0-420/{lid}/associations/default"
                     f"/0-3/{t['latest_deal']}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  ★失敗 listing={lid} deal={t['latest_deal']}: "
                      f"{type(e).__name__}: {str(e)[:80]}", flush=True)
            time.sleep(0.08)
        if (ok + fail) % 100 < len(t["jobs"]):
            print(f"  進捗 {ok + fail}件 (成功{ok})", flush=True)
    return {"ok": ok, "fail": fail, "backup": str(bk)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    # CIは全スクリプトへ共通で --dry-run/--actual を渡すため受け口を用意する
    # (無いと unrecognized arguments で落ち、他の処理まで巻き添えになる)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--out-dir", default="data/job_application_sync")
    ap.add_argument("--report-dir", default="claudedocs")
    a = ap.parse_args(argv)
    if a.dry_run:
        a.actual = False
    print("取引・関連付けを取得中...", flush=True)
    deals, d2c, d2l = collect()
    print(f"取引 {len(deals):,}件 / 会社紐付き {len(d2c):,}件\n", flush=True)
    targets, ambiguous, stat = plan(deals, d2c, d2l)
    print("=== 判定結果 ===")
    for k, n in stat.most_common():
        print(f"   {n:5,}  {k}")
    njobs = sum(len(t["jobs"]) for t in targets)
    print(f"\n★付け替え対象: {len(targets):,}系列 / 求人 {njobs:,}件")
    print(f"★人手確認が必要: {len(ambiguous):,}系列  (dry_run={not a.actual})")
    for t in targets[:5]:
        print(f"   例: {t['kind']}/{t['branch'][:22]} -> "
              f"「{(t['latest_name'] or '')[:30]}」へ求人{len(t['jobs'])}件")
    # 人手確認リストは常にCSV出力
    rep = Path(a.report_dir)
    rep.mkdir(parents=True, exist_ok=True)
    csv_path = rep / f"取引紐付け_要確認_{datetime.now():%Y-%m-%d}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["契約種別", "事業所", "取引ID", "取引名", "作成日", "求人数"])
        for g in ambiguous:
            for d in g["deals"]:
                w.writerow([g["kind"], g["branch"], d["id"], d["name"],
                            d["created"], d["jobs"]])
    print(f"\n要確認リスト: {csv_path.resolve()}")
    if not a.actual:
        print("\n(dry-run のため書き込みません。--actual で実行)")
        return
    res = apply(targets, Path(a.out_dir))
    print(f"\n=== 結果 === 紐付け成功 {res['ok']:,}件 / 失敗 {res['fail']:,}件")
    # 人手確認をSlackへ通知 (ユーザー指定 2026-08-06)
    if ambiguous:
        lines = [f"⚠️ 求人の取引紐付け: {len(ambiguous)}系列が自動判定できません"
                 f"(順序が決められない取引が並んでいます)"]
        for g in ambiguous[:5]:
            names = " / ".join((d["name"] or "")[:24] for d in g["deals"][:3])
            lines.append(f"・{g['kind']} {g['branch'][:20]}: {names}")
        if len(ambiguous) > 5:
            lines.append(f"…ほか{len(ambiguous) - 5}系列")
        lines.append(f"一覧: {csv_path.name}")
        slack_notify("\n".join(lines))
    if res["fail"]:
        slack_notify(f"⚠️ 取引紐付けで {res['fail']}件 失敗しました(要確認)")


if __name__ == "__main__":
    main()
