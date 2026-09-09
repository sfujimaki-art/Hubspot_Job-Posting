# -*- coding: utf-8 -*-
"""取引に「応募者管理シートURL」を新設し、求人票の値を集約する (2026-09-03)。

## なぜ移すか (ユーザー決定 2026-09-03「取引レコードに入れれば良くない?」)

応募が顧客の応募者管理シートへ転記される起点は求人票の `customer_sheet_url`
だった。しかしシートは**求人票単位ではなく契約単位**で存在する。実測:

    公開中の求人票 2,815件 / シートURLあり 1,184件
      取引あたりのシート種類数   1種:432  2種:1
      契約あたりのシート種類数   1種:312  2種:2   ← 99.4% が「1契約=1シート」

例外2契約 (RL00000787 / RL00000450) も「シートが2つ」ではなく、**古い取引と
新しい取引でシートが変わった**ケースだった。契約単位で持つべき裏付けになる。

求人票に置いていると:
  - 入力箇所が 2,815件 (取引なら 425件 / 契約 376。約7分の1)
  - **求人票を足すたびに入れ忘れが起きる**。その求人票への応募だけ転記されない

## このスクリプトがやること

1. 取引 (0-3) に `customer_sheet_url` を新設 (求人票と同名・同型)
2. 求人票の値を親の取引へ集約する
   - 生きている取引を優先。跡地しか無ければ跡地にも入れる
   - **既に値がある取引は触らない** (人が入れた値を機械が上書きしない)
   - 1取引に2種類のシートが集まったら**書かずに人へ回す** (勝手に選ばない)

既定はドライラン。--actual で書き込む。--rollback で書いた分を戻す。

## 移行後の参照 (別コミット)

customer_sheet_sync は「取引の値 → 無ければ求人票の値」の順で見る。
両方見るので無停止で切り替えられる。切り替え完了後に求人票側を読むのをやめる。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:
    pass

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

try:
    from scripts.job_application_sync.hs_paging import iter_all, post_retry  # noqa: E402
    from scripts.job_application_sync import deal_stages as DS  # noqa: E402
except ImportError:
    from hs_paging import iter_all, post_retry  # type: ignore
    import deal_stages as DS  # type: ignore

import os

BASE = "https://api.hubapi.com"
LISTING = "0-420"
DEAL = "0-3"
PROP = "customer_sheet_url"
LOG_DIR = _HERE / "logs"

# 求人票側と同じ定義にする (型が違うと突合できない)。グループは納品管理。
PROP_DEF = {
    "name": PROP,
    "label": "応募者管理シートURL",
    "type": "string",
    "fieldType": "text",
    "groupName": "納品管理",
    "description": ("顧客ごとの応募者管理シート(【○○御中】応募者管理シート)のURL。"
                    "応募者情報の転記先。契約単位で1つ。ここが空だと、その契約の"
                    "求人票へ来た応募は顧客シートへ転記されない。"
                    "社内台帳の「顧客管理シート」とは別物。"),
}


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def sheet_id(url: str) -> str:
    """URLの表記ゆれ (末尾の /edit#gid=0 等) を吸収した比較キー。"""
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", url or "")
    return m.group(1) if m else (url or "").strip()


# ---------------------------------------------------------------------------
# 1. プロパティ新設
# ---------------------------------------------------------------------------
def property_exists() -> dict | None:
    r = requests.get(f"{BASE}/crm/v3/properties/{DEAL}/{PROP}",
                     headers=_h(), timeout=30)
    return r.json() if r.status_code == 200 else None


def create_property(actual: bool) -> str:
    cur = property_exists()
    if cur:
        same = (cur.get("type") == PROP_DEF["type"]
                and cur.get("fieldType") == PROP_DEF["fieldType"])
        return f"既存 (label={cur.get('label')} type={cur.get('type')}/" \
               f"{cur.get('fieldType')} 同型={same})"
    if not actual:
        return "新規作成する (dry-run のため未実行)"
    r = requests.post(f"{BASE}/crm/v3/properties/{DEAL}", headers=_h(),
                      json=PROP_DEF, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"プロパティ作成失敗 HTTP {r.status_code}: {r.text[:200]}")
    return "新規作成した"


# ---------------------------------------------------------------------------
# 2. 求人票 → 取引 の集約
# ---------------------------------------------------------------------------
def collect() -> tuple:
    """(求人票→URL, 求人票→取引一覧, 取引→{name,stage,live,url}) を返す。"""
    lst = {}
    for r in iter_all(LISTING, [PROP, "kyuujin_status"]):
        p = r.get("properties") or {}
        url = (p.get(PROP) or "").strip()
        if url:
            lst[str(r["id"])] = {"url": url,
                                 "status": p.get("kyuujin_status") or ""}
    print(f"シートURLを持つ求人票 {len(lst):,}件", flush=True)

    d_of: dict = {}
    ids = sorted(lst)
    for i in range(0, len(ids), 100):
        j = post_retry(f"{BASE}/crm/v4/associations/{LISTING}/{DEAL}/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in j.get("results", []):
            d_of[str(res["from"]["id"])] = [str(t["toObjectId"])
                                            for t in res.get("to") or []]
        time.sleep(0.1)

    dids = sorted({d for v in d_of.values() for d in v})
    deals: dict = {}
    for i in range(0, len(dids), 100):
        j = post_retry(f"{BASE}/crm/v3/objects/deals/batch/read",
                       {"inputs": [{"id": d} for d in dids[i:i + 100]],
                        "properties": ["dealname", "dealstage",
                                       "code_of_customer", PROP]})
        for x in j.get("results", []):
            p = x["properties"]
            deals[str(x["id"])] = {
                "name": p.get("dealname") or "",
                "stage": p.get("dealstage") or "",
                "code": p.get("code_of_customer") or "",
                "url": (p.get(PROP) or "").strip(),
                # 「跡地(継続済/満了済)ではない」= 生きている。書込可でなくても
                # ヨミ段階の取引は生きている扱いにする (is_writable だと落ちる)
                "live": DS.classify(p.get("dealstage") or "") != DS.KIND_ENDED,
            }
        time.sleep(0.1)
    print(f"  ぶら下がる取引 {len(deals):,}件", flush=True)
    return lst, d_of, deals


OPEN_STATUS = "公開中"


def plan_rollup(lst: dict, d_of: dict, deals: dict) -> dict:
    """純関数: 取引ごとに書くURLを決める。

    - 既に値がある取引は触らない (人の入力を上書きしない)
    - ★公開中の求人票を優先。顧客がシートを引っ越すと、古いシートが公開終了の
      求人票にだけ残る。転記先は「今応募が来る求人票が指すシート」が正なので、
      公開中があればそれだけを見る (実測 2026-09-03: 混在5件のうち4件がこれ)
    - 生きている取引を優先して先に書く。跡地しか無ければ跡地にも入れる
    - それでも2種類のシートが残ったら書かずに人へ回す
    """
    cand: dict = defaultdict(dict)   # deal -> {sheet_id: [(listing, url)]}
    for lid, info in lst.items():
        for d in d_of.get(lid, []):
            if d not in deals:
                continue
            cand[d].setdefault(sheet_id(info["url"]), []).append(
                (lid, info["url"]))

    write, skip_has, defer = [], [], []
    for d, by_sheet in cand.items():
        cur = deals[d]
        if cur["url"]:
            skip_has.append({"deal_id": d, "url": cur["url"]})
            continue
        if len(by_sheet) >= 2:
            # 公開中の求人票から来た候補だけに絞れるなら、それを採る
            open_only = {s: v for s, v in by_sheet.items()
                         if any((lst.get(l) or {}).get("status") == OPEN_STATUS
                                for l, _ in v)}
            if len(open_only) == 1:
                by_sheet = open_only
        if len(by_sheet) >= 2:
            defer.append({"deal_id": d, "name": cur["name"], "code": cur["code"],
                          "sheets": [{"sheet": s, "listings": [l for l, _ in v]}
                                     for s, v in by_sheet.items()]})
            continue
        (_sid, pairs), = by_sheet.items()
        write.append({"deal_id": d, "name": cur["name"], "code": cur["code"],
                      "stage": cur["stage"], "live": cur["live"],
                      "url": pairs[0][1], "from_listings": [l for l, _ in pairs]})
    write.sort(key=lambda r: (not r["live"], r["code"]))
    return {"write": write, "skip_has": skip_has, "defer": defer}


def apply_writes(rows: list, actual: bool) -> dict:
    done, failed = [], []
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        if not actual:
            done += [{"deal_id": r["deal_id"], "url": r["url"]} for r in chunk]
            continue
        try:
            post_retry(f"{BASE}/crm/v3/objects/deals/batch/update",
                       {"inputs": [{"id": r["deal_id"],
                                    "properties": {PROP: r["url"]}}
                                   for r in chunk]})
            done += [{"deal_id": r["deal_id"], "url": r["url"]} for r in chunk]
        except Exception as e:  # noqa: BLE001
            failed += [{"deal_id": r["deal_id"],
                        "error": f"{type(e).__name__}: {str(e)[:120]}"}
                       for r in chunk]
        time.sleep(0.2)
        print(f"  書込 {min(i + 100, len(rows)):,}/{len(rows):,}", flush=True)
    return {"done": done, "failed": failed}


def rollback(path: str) -> int:
    """--actual で書いた取引の値を空に戻す。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("done") or []
    print(f"戻す対象 {len(rows):,}件", flush=True)
    for i in range(0, len(rows), 100):
        post_retry(f"{BASE}/crm/v3/objects/deals/batch/update",
                   {"inputs": [{"id": r["deal_id"], "properties": {PROP: ""}}
                               for r in rows[i:i + 100]]})
        time.sleep(0.2)
    print("戻した (プロパティ定義自体は消していない)", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="既定")
    ap.add_argument("--rollback", default="")
    a = ap.parse_args(argv)
    if a.rollback:
        return rollback(a.rollback)

    print(f"=== 取引へ応募者管理シートURLを集約 "
          f"({'actual' if a.actual else 'dry-run'}) ===", flush=True)
    print(f"プロパティ: {create_property(a.actual)}", flush=True)
    if not a.actual and not property_exists():
        print("  ※ dry-run では取引側の既存値を読めない (プロパティ未作成)。"
              "作成後にもう一度ドライランすること", flush=True)

    lst, d_of, deals = collect()
    plan = plan_rollup(lst, d_of, deals)
    print(f"\n=== 集約の内訳 ===\n"
          f"  書き込む          : {len(plan['write']):,}件\n"
          f"  既に値がある(触らない): {len(plan['skip_has']):,}件\n"
          f"  人へ回す(2種類混在) : {len(plan['defer']):,}件", flush=True)
    for d in plan["defer"]:
        print(f"   取引 {d['deal_id']} {d['code']} {d['name'][:30]}: "
              f"{len(d['sheets'])}種のシート", flush=True)

    res = apply_writes(plan["write"], a.actual)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = LOG_DIR / (f"setup_deal_sheet_url_{'actual' if a.actual else 'dry'}"
                   f"_{datetime.now():%Y%m%dT%H%M%S}.json")
    p.write_text(json.dumps({**res, "defer": plan["defer"],
                             "skip_has": plan["skip_has"]},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== 結果 === 書込 {len(res['done']):,} / 失敗 {len(res['failed']):,}\n"
          f"記録: {p}\n"
          f"戻す場合: python {Path(__file__).name} --rollback {p}", flush=True)
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
