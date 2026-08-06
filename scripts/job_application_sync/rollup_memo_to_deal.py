# -*- coding: utf-8 -*-
"""求人メモ → 取引メモへの集約 (2026-08-06) — 既定はCSV出力のみ。

なぜこうするか (ユーザー確定 2026-08-06):
  求人は「新着」を取るためクローズ→出し直しで作り替えられるが、旧新を結ぶIDが
  無く、名前類似度でも66%しか対応付かない。そこで発想を逆転し、**メモの正を
  親の取引レコードに置く**。求人は取引からメモを転記されるだけになり、
  出し直しても取引は変わらないので「継承」という概念自体が不要になる。

  現場に書き直させるのは非現実的なため、**既存の記入済みメモ6,145件を
  取引へ集約**して初期値を作る。

集約の方針:
  - メモは人が読むもの。職種ごとに条件が違うのは矛盾ではなく**そう書くべき情報**
  - 集約先で値が1種類の項目 → 「顧客共通」へ
  - 値が複数ある項目 → 「求人別の条件」へ求人名付きで列挙
  - 出典(どの求人の何件から作ったか)を必ず残す

使い方:
  python scripts/job_application_sync/rollup_memo_to_deal.py            # CSV出力のみ
  python scripts/job_application_sync/rollup_memo_to_deal.py --actual   # 取引へNote作成
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
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

BASE = "https://api.hubapi.com"
# 集約メモの署名 (求人側テンプレと区別し、再実行時の冪等判定に使う)
ROLLUP_SIGNATURE = "📋 暗黙知メモ（取引単位・配下求人を網羅）"
# 抽出対象の項目 (求人メモのテンプレ見出し)
FIELDS = ["年齢", "経験年数", "必須資格", "学歴NG", "前職業界NG", "前職企業NG",
          "確認事項", "必要書類", "回収タイミング", "提出形式", "確認担当"]
FIELD_RE = re.compile(r"(" + "|".join(FIELDS) + r")[ \t]*[:：][ \t]*([^\n]*)")
# テンプレの既定選択肢 (これは記入ではない)
DEFAULT_CHOICE = re.compile(r"^[^/]*/[^/]*/")


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _post(url: str, body: dict, retries: int = 4):
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=90)
        except requests.RequestException:
            if i < retries:
                time.sleep(2 ** i)
                continue
            raise
        if r.status_code in (200, 201, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


def strip_html(s: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", s or "")
    t = re.sub(r"</?p>", "\n", t)
    return re.sub(r"<[^>]+>", "", t)


def extract_fields(text: str) -> dict:
    """メモ本文 → {項目: 値}。既定選択肢の羅列は記入とみなさない。"""
    out = {}
    for m in FIELD_RE.finditer(text):
        k, v = m.group(1), m.group(2).strip()
        if not v or DEFAULT_CHOICE.match(v):
            continue
        out[k] = v
    return out


def build_rollup_body(entries: list) -> str:
    """集約メモ本文を組み立てる。

    entries: [{"job": 求人名, "fields": {項目: 値}}]
    単一値の項目は「顧客共通」、複数値は「求人別の条件」へ。
    """
    byfield = defaultdict(list)     # 項目 → [(値, 求人名)]
    for e in entries:
        for k, v in e["fields"].items():
            byfield[k].append((v, e["job"]))
    common, per_job = [], defaultdict(list)
    for k in FIELDS:
        vals = byfield.get(k) or []
        if not vals:
            continue
        uniq = sorted({v for v, _ in vals})
        if len(uniq) == 1:
            common.append(f"　{k}: {uniq[0]}")
        else:
            for v, job in vals:
                per_job[job].append(f"　　{k}: {v}")
    lines = [ROLLUP_SIGNATURE,
             "この取引に紐づく求人へ自動転記されます。"
             "求人ごとに違う条件は「求人別の条件」に記載しています。", ""]
    if common:
        lines += ["■ 全求人に共通"] + common + [""]
    if per_job:
        lines.append("■ 求人別の条件")
        for job, rows in per_job.items():
            lines.append(f"　【{job}】")
            lines += sorted(set(rows))
        lines.append("")
    lines.append(f"（出典: 求人{len(entries)}件のメモを "
                 f"{datetime.now():%Y-%m-%d} に集約）")
    return "\n".join(lines)


def collect(filled_ids: list) -> dict:
    """求人ID群 → 集約先ごとの entries。"""
    props, l2d, l2n = {}, {}, defaultdict(list)
    for i in range(0, len(filled_ids), 100):
        ch = filled_ids[i:i + 100]
        r = _post(f"{BASE}/crm/v3/objects/0-420/batch/read",
                  {"inputs": [{"id": x} for x in ch],
                   "properties": ["hs_name", "id_shop_hrhakkaa",
                                  "airwork_account_login_id"]})
        for o in r.get("results", []):
            props[o["id"]] = o.get("properties") or {}
        r = _post(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                  {"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            to = res.get("to") or []
            if to:
                l2d[str(res["from"]["id"])] = str(to[0]["toObjectId"])
        r = _post(f"{BASE}/crm/v4/associations/0-420/notes/batch/read",
                  {"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            for t in (res.get("to") or []):
                l2n[str(res["from"]["id"])].append(str(t["toObjectId"]))
        if (i // 100) % 20 == 0:
            print(f"  取得 {i:,}/{len(filled_ids):,}", flush=True)
        time.sleep(0.12)
    alln = sorted({n for v in l2n.values() for n in v})
    body = {}
    for i in range(0, len(alln), 100):
        r = _post(f"{BASE}/crm/v3/objects/notes/batch/read",
                  {"inputs": [{"id": x} for x in alln[i:i + 100]],
                   "properties": ["hs_note_body"]})
        for o in r.get("results", []):
            body[o["id"]] = strip_html((o.get("properties") or {}).get("hs_note_body"))
        time.sleep(0.1)
    groups = defaultdict(list)
    for lid in filled_ids:
        p = props.get(lid, {})
        did = l2d.get(lid)
        key = ("DEAL", did) if did else (
            "UNIT", p.get("id_shop_hrhakkaa") or p.get("airwork_account_login_id"))
        if not key[1]:
            continue
        fields = {}
        for n in l2n.get(lid, []):
            fields.update(extract_fields(body.get(n, "")))
        if fields:
            groups[key].append({"job": (p.get("hs_name") or "(名称なし)")[:40],
                                "listing": lid, "fields": fields})
    return groups


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true", help="取引へNoteを作成")
    ap.add_argument("--out-dir", default="claudedocs")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    scr = Path(os.environ.get("SCRATCH", "")) if os.environ.get("SCRATCH") else None
    src = (_REPO / "data" / "job_application_sync" / "filled_memo_listings.json")
    if not src.exists():
        raise SystemExit(f"事前調査の結果が必要です: {src}\n"
                         "(記入済みメモを持つ求人IDの一覧)")
    filled = json.loads(src.read_text(encoding="utf-8"))["filled"]
    if a.limit:
        filled = filled[:a.limit]
    print(f"記入済みメモを持つ求人 {len(filled):,}件 を集約します", flush=True)
    groups = collect(filled)
    print(f"\n集約先 = {len(groups):,}件", flush=True)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"取引メモ集約案_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["集約先種別", "集約先ID", "元求人数", "メモ本文"])
        for (kind, kid), entries in sorted(groups.items(),
                                           key=lambda x: -len(x[1])):
            w.writerow([kind, kid, len(entries), build_rollup_body(entries)])
    print(f"集約案(CSV): {p.resolve()}")
    big = sorted(groups.items(), key=lambda x: -len(x[1]))[:2]
    for (kind, kid), entries in big:
        print(f"\n===== 例: {kind}={kid} (元求人{len(entries)}件) =====")
        print(build_rollup_body(entries)[:900])
    if not a.actual:
        print("\n(既定はCSV出力のみ。--actual で取引へNote作成)")
        return
    # 本番: 取引へNote作成 (DEALのみ。UNITは取引が無いので対象外)
    ok = skip = fail = 0
    for (kind, kid), entries in groups.items():
        if kind != "DEAL":
            skip += 1
            continue
        try:
            _post(f"{BASE}/crm/v3/objects/notes",
                  {"properties": {
                      "hs_note_body": build_rollup_body(entries).replace("\n", "<br>"),
                      "hs_timestamp": datetime.utcnow().strftime(
                          "%Y-%m-%dT%H:%M:%SZ")},
                   "associations": [{"to": {"id": kid}, "types": [{
                       "associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": 214}]}]})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ★失敗 deal={kid}: {type(e).__name__}: {str(e)[:90]}",
                  flush=True)
        time.sleep(0.12)
    print(f"\n=== 結果 === 作成 {ok:,}件 / 失敗 {fail:,}件 / "
          f"取引なしでスキップ {skip:,}件")


if __name__ == "__main__":
    main()
