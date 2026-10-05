# -*- coding: utf-8 -*-
"""応募カードへ一次対応8項目(暗黙知)を後から届ける (2026-10-02)。

## なぜ要るか

応募作成時 (applicant_import.get_oubosaki_props) にも転記するが、
次の順番だと作成時には写せない:

    応募が来る (この時点で取引の8項目が空 = マスター無し)
      → 後日、現場が取引の8項目を入力する
      → しかし応募は二度と見に来ない

2026-10-02 実測: 生きている取引669件のうち8項目に中身があるのは68件だけ。
作成時だけに頼ると、大半の応募が空のまま残る。

## 何をするか (毎晩)

直近 WINDOW_DAYS 日 (かつ FLOOR 以降) に作られた応募のうち、

  1. 転記済みの印 (anmokuchi_tenki_nichiji) が**空**のもの
     → 応募→求人→取引 (同じ取引先コードの取引を含む) からマスターを決め、
       中身があれば8項目と印を書く。
  2. 一次対応の要否 (ichijitaiounoumu) が**空**のもの
     → 生きている取引の最新 (deal_master.latest_live) の要否を入れる。
       作成時と同じ「古い応募には付けない」ガードを通す。

## 守ること (2026-10-01 定例MTG決定)

- **印のある応募は絶対に触らない。** 親の取引の値が後で変わっても、
  一度応募へ連携した暗黙知は上書きしない。
- 要否も、入っていれば上書きしない。
- 窓は「FLOOR以降 かつ 直近 WINDOW_DAYS 日」。固定窓だと対象が毎日増え、
  夜間処理が60分で打ち切られる (2026-09-15/16 に backfill_appointment_memo で実害)。
- 既定はドライラン。--actual で書き込む。失敗があれば非0で終わる。
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:  # CI は env 直渡しなので dotenv 無しでも動く
    pass

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

try:
    from scripts.job_application_sync.hs_paging import (  # noqa: E402
        search_all_by_id, post_retry)
    from scripts.job_application_sync import deal_master as DM  # noqa: E402
    from scripts.job_application_sync import applicant_import as AI  # noqa: E402
except ImportError:  # script直実行
    from hs_paging import search_all_by_id, post_retry  # type: ignore
    import deal_master as DM  # type: ignore
    import applicant_import as AI  # type: ignore

BASE = "https://api.hubapi.com"
APPOINTMENT = "0-421"
LISTING = "0-420"
DEAL = "0-3"
# 応募カードの受け皿 (anmokuchi_*) を作ったのが 2026-10-02。それより前の応募は
# 対象にしない (MTG決定は「これから」の運用。過去の応募を遡って埋めない)。
FLOOR = "2026-10-01T00:00:00Z"
WINDOW_DAYS = 30
APPT_PROPS = ["hs_createdate", "yingmuri", "ichijitaiounoumu",
              DM.APPT_TRANSFERRED_AT]


def window_start(now=None) -> str:
    """実際に見る下限。FLOOR と「直近 WINDOW_DAYS 日」の**新しい方**。"""
    now = now or datetime.now(timezone.utc)
    rolling = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return max(FLOOR, rolling)


def plan_for_appt(appt: dict, group_ids: list, deals: dict,
                  now_ms: int, today: str = "") -> dict:
    """純関数: 1件の応募へ書くべきプロパティ。書くものが無ければ {}。

    appt: 応募の現在値 / group_ids: 応募に関係する取引ID群 / deals: {id: props}
    """
    out: dict = {}
    if not str(appt.get(DM.APPT_TRANSFERRED_AT) or "").strip():
        out.update(AI.anmokuchi_transfer_props(group_ids, deals, now_ms=now_ms))
    # 「未設定」(値 unset) も空とみなす (2026-10-02 逆証明: 値域は 必要/不要/unset)。
    # 応募日が空の応募には入れない: 古い応募か判断できず、BPOのキューに過去分が
    # 流れ込むおそれがある (実測: 応募日が空のAW応募 21件)。
    cur = str(appt.get("ichijitaiounoumu") or "").strip()
    if (not cur or cur == "unset") and str(appt.get("yingmuri") or "").strip():
        v = AI.deal_current_props(group_ids, deals).get("ichijitaiounoumu")
        if v:
            tmp = {"ichijitaiounoumu": v}
            AI.strip_ichijitaiou_if_stale(
                tmp, str(appt.get("yingmuri") or "")[:10], today=today)
            out.update(tmp)
    return out


def _batch_assoc(frm: str, to: str, ids: list) -> dict:
    out: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v4/associations/{frm}/{to}/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in r.get("results", []):
            out[str(res["from"]["id"])] = [str(t["toObjectId"])
                                           for t in res.get("to") or []]
        time.sleep(0.2)
    return out


def _batch_read(obj: str, ids: list, props: list) -> dict:
    out: dict = {}
    for i in range(0, len(ids), 100):
        r = post_retry(f"{BASE}/crm/v3/objects/{obj}/batch/read",
                       {"inputs": [{"id": x} for x in ids[i:i + 100]],
                        "properties": props})
        for x in r.get("results", []):
            out[str(x["id"])] = x.get("properties") or {}
        time.sleep(0.2)
    return out


def collect(since: str) -> tuple:
    """(対象応募{id: props}, 応募→取引群{id: [deal]}, 取引{id: props}, 内訳)。"""
    apps = {str(r["id"]): r.get("properties") or {} for r in search_all_by_id(
        APPOINTMENT, APPT_PROPS,
        [{"propertyName": "hs_createdate", "operator": "GTE", "value": since}])}
    stat = Counter(window=len(apps))
    need = {a: p for a, p in apps.items()
            if not str(p.get(DM.APPT_TRANSFERRED_AT) or "").strip()
            or str(p.get("ichijitaiounoumu") or "").strip() in ("", "unset")}
    stat["already_done"] = len(apps) - len(need)
    if not need:
        return need, {}, {}, stat
    a2l = _batch_assoc(APPOINTMENT, LISTING, sorted(need))
    lids = sorted({l for v in a2l.values() for l in v})
    l2d = _batch_assoc(LISTING, DEAL, lids) if lids else {}
    direct = sorted({d for v in l2d.values() for d in v})
    deals = _batch_read(DEAL, direct, AI.DEAL_READ_PROPS) if direct else {}
    # 同じ取引先コードの納品管理PL取引も群に加える (MTG「関連取引すべて」)
    codes = {str(p.get(DM.PROP_CODE) or "").strip() for p in deals.values()} - {""}
    pipeline = {str(r["id"]): r.get("properties") or {} for r in search_all_by_id(
        DEAL, AI.DEAL_READ_PROPS,
        [{"propertyName": "pipeline", "operator": "EQ",
          "value": AI.DELIVERY_PIPELINE}])}
    for did, p in pipeline.items():
        if str(p.get(DM.PROP_CODE) or "").strip() in codes:
            deals[did] = p
    # 店舗IDを別会社と共有している求人 (=どの会社の契約か決まらない)
    shop_index = DM.shop_code_index(pipeline)
    lprops = _batch_read(LISTING, lids, ["id_shop_hrhakkaa", DM.LISTING_OWNER]) if lids else {}
    lshop = {l: (p.get("id_shop_hrhakkaa") or "") for l, p in lprops.items()}
    lowner = {l: (p.get(DM.LISTING_OWNER) or "").strip() for l, p in lprops.items()}
    shared_l = {l for l, s in lshop.items() if DM.is_shared_shop(s, shop_index)}
    by_code_all = DM.group_by_code(pipeline)
    by_code = DM.group_by_code(deals)
    groups: dict = {}
    for a in need:
        # ★求人に持ち主コードが判定済み (2026-10-05) なら、その取引群をそのまま使う
        owners = {lowner.get(l) for l in a2l.get(a, [])} - {"", None}
        if len(owners) == 1:
            code = next(iter(owners))
            for did in by_code_all.get(code, []):
                deals.setdefault(did, pipeline[did])
            groups[a] = list(by_code_all.get(code, []))
            continue
        if len(owners) > 1:
            stat["shared_shop"] += 1     # 応募の求人の持ち主が複数社
            groups[a] = []
            continue
        if any(l in shared_l for l in a2l.get(a, [])):
            stat["shared_shop"] += 1     # 別会社の条件を入れない。人の確認へ
            groups[a] = []
            continue
        ids = [d for l in a2l.get(a, []) for d in l2d.get(l, [])]
        if DM.spans_codes(ids, deals):
            stat["shared_shop"] += 1     # 別会社の取引に同時に紐付いている
            groups[a] = []
            continue
        if not a2l.get(a):
            stat["no_listing"] += 1
        elif not ids:
            stat["no_deal"] += 1
        for c in {str(deals.get(d, {}).get(DM.PROP_CODE) or "").strip()
                  for d in ids} - {""}:
            ids += by_code.get(c, [])
        groups[a] = list(dict.fromkeys(ids))
    return need, groups, deals, stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="既定 (書き込まない)")
    a = ap.parse_args(argv)

    since = window_start()
    print(f"=== 応募への一次対応8項目の転記 ({since[:10]} 以降 / "
          f"{'actual' if a.actual else 'dry-run'}) ===", flush=True)
    need, groups, deals, stat = collect(since)
    now_ms = int(time.time() * 1000)
    plans = {}
    for appt_id, p in need.items():
        pl = plan_for_appt(p, groups.get(appt_id, []), deals, now_ms)
        if DM.APPT_TRANSFERRED_AT in pl:
            stat["transfer"] += 1
        elif not str(p.get(DM.APPT_TRANSFERRED_AT) or "").strip():
            stat["no_master"] += 1
        if "ichijitaiounoumu" in pl:
            stat["youhi_fill"] += 1
        if pl:
            plans[appt_id] = pl
    print(f"対象期間の応募 {stat['window']:,}件 (印・要否とも済み {stat['already_done']:,}件)"
          f" / 求人未紐付け {stat['no_listing']} / 求人に取引なし {stat['no_deal']}"
          f" / 別会社とまたがるため見送り {stat['shared_shop']}", flush=True)
    print(f"  8項目を転記 {stat['transfer']} / マスター無し(取引が空・翌晩再訪) "
          f"{stat['no_master']} / 要否を補完 {stat['youhi_fill']} / 書き込む応募 {len(plans)}",
          flush=True)
    if not a.actual:
        for appt_id, pl in list(plans.items())[:20]:
            print(f"  [dry] 応募 {appt_id} ← {sorted(pl)}", flush=True)
        return 0

    ok = failed = 0
    items = list(plans.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        try:
            r = post_retry(f"{BASE}/crm/v3/objects/{APPOINTMENT}/batch/update",
                           {"inputs": [{"id": k, "properties": v} for k, v in chunk]})
            done = {str(x["id"]) for x in r.get("results", [])}
            ok += len(done)
            for k, _ in chunk:
                if k not in done:
                    failed += 1
                    print(f"  [warn] 更新できず 応募={k}", flush=True)
        except Exception as e:  # noqa: BLE001
            failed += len(chunk)
            print(f"  [warn] バッチ更新失敗 {len(chunk)}件: {type(e).__name__}: {e}",
                  flush=True)
        time.sleep(0.2)
    print(f"=== 結果 === 更新 {ok} / 失敗 {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
