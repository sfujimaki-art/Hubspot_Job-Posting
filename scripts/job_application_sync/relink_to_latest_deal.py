# -*- coding: utf-8 -*-
"""求人を、同じ取引先コードの「生きている取引すべて」へ紐付ける (2026-10-02 作り直し)。

## なぜ作り直したか

旧実装 (2026-08-06〜) は系列ごとに「最新の取引」を1つ選び、**最新の取引に
求人が1件でもあれば何もしなかった**。そのため、古い取引にぶら下がった
求人は永久に古い取引のまま残った (2026-10-01 実測: 302系列が「最新に求人あり
(正常)」で素通り、付け替えたのは3系列。応募が終わった取引にしか届かない
会社が24社)。さらに系列を取引名の文字列から組んでいたため、接頭辞の
二重付け (「サブスク継続⑤＿再契約＿…」) や「再契約」の扱いで系列が切れていた。

2026-10-01 の定例MTGで方針が決まった: **関連する取引すべてに求人を紐付ける**。
取捨選択 (どれが最新か) を機械に決めさせると、いたちごっこになるため。

## 何をするか

  系列 = **取引先コード (code_of_customer)**。計上PLが契約単位で発番しており、
  契約更新で納品管理の取引が作り替えられてもコードは変わらない。

  各コードについて、そのコードのどれかの取引に紐付いている求人を、
  そのコードの**生きている取引すべて** (deal_stages.is_writable) に紐付ける。

  - **追加のみ**。古い取引との紐付けは外さない
  - 終わった取引 (継続済・満了済・解約済) へは新たに付けない
  - 生きている取引が1つも無いコードは触らない (付けても誰にも届かない)

## 機械で決めないもの (CSVで人へ回す)

  - 取引先コードが無い取引 (求人を持っている or 生きているもの)
  - 計上ごとに取引先コードが割れている取引
  - 別々の取引先コードの取引に同時に紐付いている求人
    (別会社と店舗IDを共有していた等。片方の系列へ広げると誤りが増える)

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

from scripts.job_application_sync import deal_stages as DS  # noqa: E402
from scripts.job_application_sync import deal_master as DM  # noqa: E402

BASE = "https://api.hubapi.com"

# 人へ回す区分
NG_NOCODE = "× 取引先コードが無い"
NG_SPLIT = "× 取引先コードが割れている"
NG_CROSS = "× 別々の取引先コードの取引に紐付いている求人"

TODO_TEXT = {
    NG_NOCODE: "計上の取引を作成し、この納品管理の取引と関連付けてください。"
               "取引先コードが無いと、契約更新をまたいで求人を運べません",
    NG_SPLIT: "計上の取引が複数あり取引先コードが一致しません。"
              "どれが正しい契約か決めて、納品管理の取引先コードに入れてください",
    NG_CROSS: "この求人が本当はどの契約のものか確認し、違う方の取引との紐付けを"
              "外してください (店舗IDの共有などで別会社に付いた可能性があります)",
}

MANUAL_COLS = ["区分", "やること", "取引ID", "取引名", "ステージ",
               "取引先コード", "作成日", "求人数"]


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


def _search_pipeline(pipeline: str, props: list) -> dict:
    """1パイプラインを全件。取得漏れは total と突合して落とす。

    ★hs_object_id 昇順で回す。1件でも欠けると契約グループから生きている
      取引が消え、紐付けるべき取引を見落とすので、黙って先へ進めない。
    """
    fg = [{"filters": [{"propertyName": "pipeline", "operator": "EQ",
                        "value": pipeline}]}]
    total = _req("POST", f"{BASE}/crm/v3/objects/0-3/search",
                 json={"filterGroups": fg, "limit": 1}).get("total", 0)
    out, last = {}, 0
    while True:
        b = {"filterGroups": [{"filters": fg[0]["filters"] + [
                {"propertyName": "hs_object_id", "operator": "GT",
                 "value": str(last)}]}],
             "properties": props, "limit": 100,
             "sorts": [{"propertyName": "hs_object_id",
                        "direction": "ASCENDING"}]}
        r = _req("POST", f"{BASE}/crm/v3/objects/0-3/search", json=b)
        rs = r.get("results", [])
        if not rs:
            break
        for o in rs:
            out[o["id"]] = o.get("properties") or {}
        nxt = int(rs[-1]["id"])
        if nxt <= last:
            raise RuntimeError(f"取引の取得でカーソルが進まない pipeline={pipeline}")
        last = nxt
        time.sleep(0.12)
    if len(out) != total:
        raise RuntimeError(
            f"取引の取得漏れ pipeline={pipeline}: {len(out)}/{total}件。"
            "欠けたまま判定すると生きている取引を見落とすので中止する")
    return out


def _batch_assoc(frm: str, to: str, ids: list) -> dict:
    """{from_id: [to_id, ...]}。100件ずつ (全件一度だと空が返る)."""
    m = defaultdict(list)
    for i in range(0, len(ids), 100):
        r = _req("POST", f"{BASE}/crm/v4/associations/{frm}/{to}/batch/read",
                 json={"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in r.get("results", []):
            for t in (res.get("to") or []):
                m[str(res["from"]["id"])].append(str(t["toObjectId"]))
        time.sleep(0.2)
    return m


def collect() -> dict:
    """判定に要るものを一度に取る (納品管理の取引 / 関連付け / 計上)。

    計上PLまで取るのは、取引先コードが**割れている**取引を見分けるため。
    納品管理側のコードは、無効化済みのワークフローが会社に紐づく取引へ
    一律に配っていた誤りを含みうる (backfill_deal_code_of_customer 参照)。
    """
    deals = _search_pipeline(DS.PIPELINE_NOUHIN,
                             ["dealname", "dealstage", "createdate",
                              "contract_start_date", DS.PROP_CODE])
    did = sorted(deals)
    d2l = _batch_assoc("0-3", "0-420", did)
    d2d = _batch_assoc("0-3", "0-3", did)
    keijo: dict = {}
    for pid in sorted(DS.PIPELINES_KEIJO):
        keijo.update(_search_pipeline(pid, [DS.PROP_CODE]))
    return {"deals": deals, "d2l": d2l, "d2d": d2d, "keijo": keijo}


def code_candidates(deal_id: str, d2d: dict, keijo: dict) -> list:
    """その取引にぶら下がる計上から取引先コードを集める (空は除く)。
    2件以上あれば「割れている」= 契約を1つに束ねられない。"""
    return sorted({(keijo[x].get(DS.PROP_CODE) or "").strip()
                   for x in d2d.get(deal_id, []) if x in keijo} - {""})


def _manual_row(kind: str, deal_id: str, props: dict, codes: list,
                n_jobs: int) -> dict:
    return {
        "区分": kind,
        "やること": TODO_TEXT[kind],
        "取引ID": deal_id,
        "取引名": props.get("dealname") or "",
        "ステージ": DS.label(props.get("dealstage")) or "(不明なステージ)",
        "取引先コード": (" / ".join(codes) if codes
                     else (props.get(DS.PROP_CODE) or "").strip()),
        "作成日": (props.get("createdate") or "")[:10],
        "求人数": n_jobs,
    }


def plan_all_live(deals: dict, d2l: dict, d2d: dict, keijo: dict) -> tuple:
    """取引先コードごとに「生きている取引すべて」へ紐付ける計画 (純関数)。

    Returns:
        (pairs=[(listing_id, deal_id)], manual=[行], stat=Counter)
    """
    stat = Counter()
    manual = []
    code_of: dict = {}                     # 系列に入れる取引 → コード
    for did in sorted(deals):
        p = deals[did]
        cands = code_candidates(did, d2d, keijo)
        notable = DS.is_writable(p.get("dealstage")) or bool(d2l.get(did))
        if len(cands) > 1:
            # 計上ごとにコードが食い違う。**系列にも入れない**。
            # 誤った系列へ求人を足す方が害が大きい。
            if notable:
                manual.append(_manual_row(NG_SPLIT, did, p, cands,
                                          len(d2l.get(did, []))))
                stat["★取引先コードが割れている(人の確認)"] += 1
            continue
        code = (p.get(DS.PROP_CODE) or "").strip()
        if not code:
            if notable:
                manual.append(_manual_row(NG_NOCODE, did, p, cands,
                                          len(d2l.get(did, []))))
                stat["★取引先コードが無い(人の確認)"] += 1
            continue
        code_of[did] = code

    # 求人 → 紐付いている取引のコード。別々のコードにまたがる求人は広げない。
    l_codes: dict = defaultdict(set)
    for did, code in code_of.items():
        for lid in d2l.get(did, []):
            l_codes[lid].add(code)
    cross = {lid for lid, cs in l_codes.items() if len(cs) > 1}
    if cross:
        stat["★別々の取引先コードに紐付いている求人(人の確認)"] = len(cross)
        seen = set()
        for did, code in sorted(code_of.items()):
            if did in seen:
                continue
            n = sum(1 for lid in d2l.get(did, []) if lid in cross)
            if n:
                seen.add(did)
                manual.append(_manual_row(NG_CROSS, did, deals[did], [code], n))

    groups: dict = defaultdict(list)
    for did, code in code_of.items():
        groups[code].append(did)

    pairs = []
    for code in sorted(groups):
        members = groups[code]
        live = sorted(d for d in members if DM.is_live(deals[d]))
        if not live:
            stat["生きている取引が無いコード(対象外)"] += 1
            continue
        jobs = sorted({lid for d in members for lid in d2l.get(d, [])} - cross)
        if not jobs:
            stat["求人の無いコード(対象外)"] += 1
            continue
        added = 0
        for d in live:
            have = set(d2l.get(d, []))
            for lid in jobs:
                if lid not in have:
                    pairs.append((lid, d))
                    added += 1
        stat["★追加の紐付けがあるコード" if added else "すでに全部紐付いているコード"] += 1
        if len(live) > 1:
            stat["(参考)生きている取引が複数あるコード"] += 1
    return pairs, manual, stat


def apply(pairs: list, out_dir: Path) -> dict:
    """紐付けを追加する (追加のみ・旧の紐付けは残す)。先に内容を保存する。"""
    from scripts.job_application_sync.sync_deal_association import associate_batch
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bk = out_dir / f"relink_backup_{stamp}.json"
    bk.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    print(f"[backup] 追加する紐付けを保存: {bk}", flush=True)
    ok, fail = associate_batch(pairs)
    return {"ok": len(ok), "fail": fail, "backup": str(bk)}


def write_manual_csv(rows: list, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ★utf-8-sig。Excelで開いたときに文字化けさせない。
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANUAL_COLS)
        w.writeheader()
        w.writerows(rows)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    # CIは全スクリプトへ共通で --dry-run/--actual を渡すため受け口を用意する
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--out-dir", default="data/job_application_sync")
    ap.add_argument("--report-dir", default="claudedocs")
    a = ap.parse_args(argv)
    if a.dry_run:
        a.actual = False
    print("取引・関連付けを取得中...", flush=True)
    c = collect()
    deals, d2l = c["deals"], c["d2l"]
    print(f"取引 {len(deals):,}件 / 計上 {len(c['keijo']):,}件\n", flush=True)
    pairs, manual, stat = plan_all_live(deals, d2l, c["d2d"], c["keijo"])
    print("=== 判定結果 (系列=取引先コード / 生きている取引すべてへ) ===")
    for k, n in stat.most_common():
        if not k.startswith("(参考)"):
            print(f"   {n:6,}  {k}")
    for k, n in sorted(stat.items()):
        if k.startswith("(参考)"):
            print(f"   {n:6,}  {k}")
    n_codes = len({(deals[d].get(DS.PROP_CODE) or "").strip() for _l, d in pairs})
    print(f"\n★追加する紐付け: {len(pairs):,}本 / 求人 {len({l for l, _ in pairs}):,}件 / "
          f"取引 {len({d for _, d in pairs}):,}件 / 取引先コード {n_codes:,}")
    print(f"★人の確認が必要: {len(manual):,}件  (dry_run={not a.actual})")
    csv_path = write_manual_csv(
        manual,
        Path(a.report_dir) / f"取引紐付け_要確認_{datetime.now():%Y-%m-%d}.csv")
    print(f"\n要確認リスト: {csv_path.resolve()}")
    if not a.actual:
        print("\n(dry-run のため書き込みません。--actual で実行)")
        return 0
    res = apply(pairs, Path(a.out_dir))
    print(f"\n=== 結果 === 紐付け成功 {res['ok']:,}件 / 失敗 {res['fail']:,}件")
    if res["fail"]:
        slack_notify(f"⚠️ 求人の取引紐付けで {res['fail']}件 失敗しました(要確認)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
