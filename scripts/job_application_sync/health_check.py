# -*- coding: utf-8 -*-
"""求人・応募データの健全性チェック (2026-08-06) — 検知のみ。

なぜ「処理が成功したか」ではなく「あるべき状態か」を見るのか:
  2026-08-06 の調査で、日次処理が3つの壊れ方をしていた。いずれも
  **エラーを出さず、ログ上は正常**だった:

    1. GASのトリガーが消え、deal-assoc は10日 / ichijitaiou は29日
       一度も起動されていなかった (成功ログが無いだけで、誰も気づけない)
    2. Search API の10,000件上限に触れると HTTP 400 が返るが、素朴な実装は
       「もう次が無い」と区別できず、34,666件中10,000件で完走扱いになる
    3. メモのコピーは正常に動いていたが、中身が空のテンプレートだった

  「動いたか」を見張る監視はこの3つを全部見逃す。**結果の状態を見る**なら
  どれも「あるべき値との差分」として現れる。だからこちらを監視する。

閾値を超えたら Slack へ通知する。修正はしない (何を直すかは人が決める)。

使い方:
  python scripts/job_application_sync/health_check.py           # 検知のみ
  python scripts/job_application_sync/health_check.py --slack   # Slack通知あり
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")

# Windowsローカルの既定は cp932 で、"—" のような文字で print が落ちる。
# 監視スクリプトが出力の文字化けで死ぬのは本末転倒なので明示的に固定する。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from scripts.job_application_sync.hs_paging import search_all  # noqa: E402
from scripts.job_application_sync.listing_stage import (  # noqa: E402
    PROTECTED_STAGES, STATUS_TO_STAGE)

BASE = "https://api.hubapi.com"
RECENT_DAYS = 14          # 「最近作られた」の窓
SEARCH_CAP = 10000        # Search API の上限
SEARCH_WARN = 9000        # ここを超えたら「いずれ静かに壊れる」と警告


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def slack_notify(message: str) -> bool:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:300]}", flush=True)
        return False
    try:
        return requests.post(url, json={"text": message},
                             timeout=15).status_code == 200
    except requests.RequestException as e:
        print(f"[slack送信失敗] {e}", flush=True)
        return False


def _total(obj: str, filters: list) -> int:
    """Search の総件数だけを取る (1件だけ引いて total を読む)。"""
    r = requests.post(f"{BASE}/crm/v3/objects/{obj}/search", headers=_h(),
                      json={"filterGroups": [{"filters": filters}],
                            "properties": ["hs_object_id"], "limit": 1},
                      timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"total {obj} HTTP {r.status_code}: {r.text[:120]}")
    return int(r.json().get("total") or 0)


def _assoc(from_type: str, to_type: str, ids: list) -> dict:
    """{from_id: [to_id,...]} を batch/read で取る。"""
    m: dict = {}
    for i in range(0, len(ids), 100):
        r = requests.post(
            f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
            headers=_h(), json={"inputs": [{"id": x} for x in ids[i:i + 100]]},
            timeout=60)
        if r.status_code not in (200, 207):
            raise RuntimeError(f"assoc HTTP {r.status_code}: {r.text[:120]}")
        for res in r.json().get("results", []):
            m[str(res["from"]["id"])] = [str(t["toObjectId"])
                                         for t in (res.get("to") or [])]
        time.sleep(0.1)
    return m


def _batch_props(obj: str, ids: list, props: list) -> dict:
    out: dict = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), 100):
        r = requests.post(f"{BASE}/crm/v3/objects/{obj}/batch/read",
                          headers=_h(),
                          json={"properties": props,
                                "inputs": [{"id": x} for x in ids[i:i + 100]]},
                          timeout=60)
        if r.status_code not in (200, 207):
            raise RuntimeError(f"batch/read HTTP {r.status_code}")
        for o in r.json().get("results", []):
            out[str(o["id"])] = o.get("properties") or {}
        time.sleep(0.1)
    return out


# ---------------------------------------------------------------- 各チェック

def check_stage_consistency() -> dict:
    """公開状態とボード上のステージが食い違っている求人。

    取込に組み込んだステージ追従が働いていれば 0 件になる。増えていれば
    取込が止まったか、追従の分岐に漏れがある。
    """
    bad = 0
    detail = []
    for status, want in STATUS_TO_STAGE.items():
        n = _total("0-420", [
            {"propertyName": "kyuujin_status", "operator": "EQ", "value": status},
            {"propertyName": "hs_pipeline_stage", "operator": "NEQ", "value": want},
            # ★HubSpot Search の NEQ / NOT_IN は **未設定レコードも一致扱いにする**。
            #   HAS_PROPERTY を併記しないと、下の「未設定」と同じ件数を二重に数える
            #   (2026-08-06: 実体24件を48件と報告していた)。
            {"propertyName": "hs_pipeline_stage", "operator": "HAS_PROPERTY"},
            # 人が動かしたステージは対象外 (機械は触らない領域)
            {"propertyName": "hs_pipeline_stage", "operator": "NOT_IN",
             "values": list(PROTECTED_STAGES)},
        ])
        if n:
            detail.append(f"{status}なのに別ステージ: {n:,}件")
        bad += n
    # ステージそのものが未設定 = ボードに出ない
    unset = _total("0-420", [
        {"propertyName": "kyuujin_status", "operator": "HAS_PROPERTY"},
        {"propertyName": "hs_pipeline_stage", "operator": "NOT_HAS_PROPERTY"}])
    if unset:
        detail.append(f"ステージ未設定(ボード非表示): {unset:,}件")
    return {"name": "求人ステージが実態と一致しているか", "value": bad + unset,
            "want": 0, "detail": detail}


def _company_hint() -> dict:
    """識別子 → 会社名 の索引を作る (best-effort)。

    ★「どの取引に入れるのか」が分からないと現場は動けない。
      しかし LISTING に会社名の列は無く、応募側の応募先取引名も
      **取引に紐付いていないから空**という循環になっている。
      そこで外側から引く:
        AW: airwork_account_login_id → 顧客管理シートの会社名
        HR: 店舗id → HR求人CSVの連絡先メール → シートのリクロジアドレス → 会社名
      監視スクリプトを重くしたくないので**失敗しても握って空を返す**
      (会社名が出ないだけで、件数と店舗ID/ログインIDは従来どおり出る)。
    """
    idx: dict = {}
    try:
        import csv as _csv
        import glob as _glob
        from scripts.job_application_sync import applicant_queue as _aq
        os.environ.setdefault("SHEETS_AUTH_MODE", "sa")
        r = _aq.AccountResolver().build()
        c = r.cols
        # AW: login_id / エイリアス / リクロジアドレス → 会社名
        for key_idx in ("bid", "aid", "reclog", "alias"):
            for k, row in {"bid": r.idx_bid, "aid": r.idx_aid,
                           "reclog": r.idx_reclog, "alias": r.idx_alias}[key_idx].items():
                comp = row[c["comp"]] if len(row) > c["comp"] else ""
                if k and comp:
                    idx.setdefault(k, comp)
        # HR: 店舗id → 連絡先メール → (上の索引で) 会社名
        cs = sorted(_glob.glob(str(_REPO / "scratchpad" / "csv_fetched" / "hr"
                                   / "hr_offers_all_*.csv")))
        if cs:
            for enc in ("cp932", "utf-8-sig"):
                try:
                    with open(cs[-1], encoding=enc) as f:
                        rd = _csv.reader(f)
                        hdr = next(rd)
                        si, mi = hdr.index("店舗id"), hdr.index("連絡先メールアドレス")
                        for row in rd:
                            if len(row) <= max(si, mi):
                                continue
                            sid, mail = row[si].strip(), row[mi].strip().lower()
                            if sid and mail and idx.get(mail):
                                idx.setdefault(f"shop:{sid}", idx[mail])
                    break
                except (UnicodeDecodeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001
        print(f"      (会社名の索引を作れませんでした: {type(e).__name__})", flush=True)
    return idx


def check_recent_listings_linked() -> dict:
    """最近作られた求人が取引に紐付いているか。

    sync_deal_association / relink_to_latest_deal が動いていれば 0 に近づく。
    止まれば日々増える = **停止検知を兼ねる**。
    """
    since = int((datetime.now(timezone.utc)
                 - timedelta(days=RECENT_DAYS)).timestamp() * 1000)
    rows = search_all("0-420", ["hs_name", "kyuujin_status", "hs_createdate",
                                "id_hrhakkaa", "id_airwork",
                                "airwork_account_login_id", "id_shop_hrhakkaa"],
                      [{"propertyName": "hs_createdate", "operator": "GTE",
                        "value": str(since)}])
    ids = [o["id"] for o in rows]
    if not ids:
        return {"name": f"直近{RECENT_DAYS}日の求人が取引に紐付いているか",
                "value": 0, "want": 0, "detail": ["対象求人なし"]}
    assoc = _assoc("0-420", "0-3", ids)
    # ★母数は「応募が来ている公開中の求人」に限る (2026-08-09 較正)。
    #   HRハッカーの求人はリクロジの顧客以外のものも取り込んでいるため、
    #   直近作成の求人を全部母数にすると**取引に紐付かないのが正常な求人**が
    #   大量に混ざり、この項目は永久にNGのままになる。
    #   実測: 直近14日の求人4,996件 → 現行判定で未紐付け189件。
    #         うち応募が来ている公開中のものは **52件** だけ。
    #   3項目が恒久NGだと誰も見なくなり、本物の異常が埋もれる(実際そうなっていた)。
    #   応募が来ている＝リクロジが扱っている求人なので、取引に紐付くべき。
    #   停止検知の役割は維持される(sync_deal_association が止まれば日々増える)。
    appt = _assoc("0-420", "0-421", ids)
    unlinked = [o for o in rows
                if not assoc.get(o["id"])
                and appt.get(o["id"])          # 応募が来ている求人だけ
                and (o.get("properties") or {}).get("kyuujin_status") != "公開終了"]
    n_target = sum(1 for o in rows
                   if appt.get(o["id"])
                   and (o.get("properties") or {}).get("kyuujin_status") != "公開終了")
    # ★件数だけでは誰も動けない。**何をどこに入れれば直るか**を明細で返す
    #   (2026-08-12)。health_check が5項目中3項目を恒久NGにしたまま誰にも
    #   見られていなかったのは、件数しか出していなかったことも一因。
    hint = _company_hint() if unlinked else {}
    items = []
    for o in unlinked:
        p_ = o.get("properties") or {}
        is_hr = bool((p_.get("id_hrhakkaa") or "").strip())
        items.append({
            "会社名(候補)": (hint.get(f"shop:{(p_.get('id_shop_hrhakkaa') or '').strip()}")
                             or hint.get((p_.get("airwork_account_login_id") or "").strip().lower())
                             or ""),
            "求人ID": o["id"],
            "求人名": (p_.get("hs_name") or "")[:60],
            "媒体": "HRハッカー" if is_hr else "AirWork",
            "媒体求人ID": (p_.get("id_hrhakkaa") or p_.get("id_airwork") or ""),
            "HR店舗ID": (p_.get("id_shop_hrhakkaa") or ""),
            "AWログインID": (p_.get("airwork_account_login_id") or ""),
            "入れる場所": ("取引の「HRハッカー店舗ID（複数可・;区切り）」に "
                          f"{(p_.get('id_shop_hrhakkaa') or '(店舗ID不明)')} を ; で追記"
                          if is_hr else
                          "取引の「管理用メールアドレス（rpo.medica+／複数可・;区切り）」に "
                          "顧客管理シートのリクロジアドレスを入れる"),
        })
    return {"name": f"直近{RECENT_DAYS}日の求人が取引に紐付いているか",
            "value": len(unlinked), "want": 0,
            "items": items,
            "action": "この求人が属する取引(納品管理PL)を開き、下の列に値を入れる。"
                      "取引は必ず存在する — 見つからない場合は突合ロジックの不具合として調査する",
            "impact": "この求人への応募は 一次対応の要否・担当者・応募先取引名 が空のまま入る",
            "detail": [f"応募が来ている公開中の求人 {n_target:,}件中 "
                       f"未紐付け {len(unlinked):,}件 "
                       f"(作成された求人は {len(ids):,}件。他社求人を含むため母数から除外)"]
                      + [f"  例: {(o.get('properties') or {}).get('hs_name','')[:34]}"
                         for o in unlinked[:3]]}


def check_ichijitaiou_sync() -> dict:
    """一次対応の要否が、紐づく取引と食い違っている求人。

    sync_ichijitaiou が動いていれば 0。29日止まっていた実績があるので見張る。
    全件だと重いので直近作成分に絞る (止まれば新しい求人から食い違う)。
    """
    since = int((datetime.now(timezone.utc)
                 - timedelta(days=RECENT_DAYS)).timestamp() * 1000)
    rows = search_all("0-420", ["ichijitaiounoumu_deforuto"],
                      [{"propertyName": "hs_createdate", "operator": "GTE",
                        "value": str(since)}])
    ids = [o["id"] for o in rows]
    if not ids:
        return {"name": "一次対応の要否が取引と一致しているか", "value": 0,
                "want": 0, "detail": ["対象求人なし"]}
    assoc = _assoc("0-420", "0-3", ids)
    deal_ids = [d for v in assoc.values() for d in v]
    if not deal_ids:
        return {"name": "一次対応の要否が取引と一致しているか", "value": 0,
                "want": 0, "detail": ["取引に紐付く求人なし"]}
    flags = _batch_props("0-3", deal_ids, ["itijitaiou"])
    mismatch = 0
    for o in rows:
        ds = assoc.get(o["id"]) or []
        if not ds:
            continue
        vals = [flags.get(d, {}).get("itijitaiou") for d in ds]
        want = "必要" if "true" in vals else ("不要" if "false" in vals else None)
        if want and (o.get("properties") or {}).get(
                "ichijitaiounoumu_deforuto") != want:
            mismatch += 1
    return {"name": "一次対応の要否が取引と一致しているか", "value": mismatch,
            "want": 0,
            "detail": [f"直近{RECENT_DAYS}日の求人 {len(ids):,}件中 "
                       f"食い違い {mismatch:,}件"]}


def check_search_cap() -> dict:
    """本番処理が使っている検索が、10,000件上限に近づいていないか。

    上限に触れた瞬間、処理は**エラーを出さずに一部しか処理しなくなる**。
    実測 (2026-08-06): customer_sheet_url 持ちの求人が9,230件で残り770件だった。
    先に気づけるよう、9,000件を超えたクエリを警告する。
    """
    # ★監視するのは「実際に search でページングしている本番クエリ」だけ。
    #   2026-08-09 較正: 「顧客シートURL持ちの求人 (drift)」を外した。
    #   check_sheet_url_drift は既に上限の無い list API (iter_all) へ移行済みで、
    #   customer_sheet_url を search で全件ページングするコードはもう存在しない
    #   (customer_sheet_sync は CONTAINS_TOKEN のシート単位検索で数十件)。
    #   それでも9,644件=96%と警告し続けており、**実在しない危険で3項目中1つを
    #   恒久的にNGにしていた**。誤警報は本物の異常を埋もれさせるので落とす。
    #   残す2つは今も search でページングしている:
    #     relink_to_latest_deal.py の 0-3 search / sync_ichijitaiou の _search_all
    queries = [
        ("納品管理PLの取引 (relink_to_latest_deal)", "0-3",
         [{"propertyName": "pipeline", "operator": "EQ", "value": "21596025"}]),
        ("管理用メール持ちの取引 (sync_ichijitaiou)", "0-3",
         [{"propertyName": "kanri_mail_address", "operator": "HAS_PROPERTY"}]),
    ]
    over, detail = 0, []
    for label, obj, f in queries:
        try:
            n = _total(obj, f)
        except RuntimeError as e:
            detail.append(f"{label}: 測定失敗 {e}")
            continue
        pct = n * 100 // SEARCH_CAP
        if n >= SEARCH_CAP:
            over += 1
            detail.append(f"★{label}: {n:,}件 = 上限超過。既に取りこぼしています")
        elif n >= SEARCH_WARN:
            over += 1
            detail.append(f"{label}: {n:,}件 (上限の{pct}%) — 近日中に頭打ち")
        else:
            detail.append(f"{label}: {n:,}件 (上限の{pct}%)")
    return {"name": "検索の10,000件上限に近づいていないか", "value": over,
            "want": 0, "detail": detail}


def check_recent_applications_linked() -> dict:
    """最近の応募が求人に紐付いているか (紐付かないと転記も一次対応も効かない)。

    ★軸は yingmuri(応募日)。hs_createdate(HubSpot登録日) ではない (2026-08-09 較正)。

    登録日で数えると、過去分をまとめて取り込んだ日に**古い応募が大量に母数へ入る**。
    それらは媒体側で求人が既に削除済みで紐付けようがなく、恒久的にNGになる。
    実測 (2026-08-08の実行): 登録日基準で「応募882件中155件が未紐付け」と出たが、
    同じ日を応募日基準で数えると **未紐付け0件**。155件は8/07の復旧で入った
    2024〜2026年の過去分だった (求人がAirWork側で削除済み=救済不能)。

    知りたいのは「今日来た応募がちゃんと求人に繋がっているか」なので応募日で見る。
    """
    since = int((datetime.now(timezone.utc)
                 - timedelta(days=3)).timestamp() * 1000)
    rows = search_all("0-421", ["yingmuri"],
                      [{"propertyName": "yingmuri", "operator": "GTE",
                        "value": str(since)}])
    ids = [o["id"] for o in rows]
    if not ids:
        return {"name": "直近3日の応募が求人に紐付いているか", "value": 0,
                "want": 0, "detail": ["対象応募なし"]}
    assoc = _assoc("0-421", "0-420", ids)
    unlinked = [i for i in ids if not assoc.get(i)]
    return {"name": "直近3日の応募が求人に紐付いているか", "value": len(unlinked),
            "want": 0,
            "detail": [f"応募 {len(ids):,}件中 求人未紐付け {len(unlinked):,}件 "
                       f"(応募日基準。登録日ではない)"]}


CHECKS = [check_stage_consistency, check_recent_listings_linked,
          check_ichijitaiou_sync, check_search_cap,
          check_recent_applications_linked]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack", action="store_true", help="異常をSlackへ通知")
    ap.add_argument("--out-dir", default="data/job_application_sync")
    a = ap.parse_args(argv)

    results, failed = [], []
    for fn in CHECKS:
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            # チェック自体の失敗も隠さない (監視が黙って死ぬのを防ぐ)
            r = {"name": fn.__name__, "value": -1, "want": 0,
                 "detail": [f"チェック実行に失敗: {type(e).__name__}: {e}"]}
            failed.append(fn.__name__)
        results.append(r)
        mark = "OK" if r["value"] == r["want"] else ("ERR" if r["value"] < 0 else "NG")
        print(f"[{mark}] {r['name']}: {r['value']}")
        for d in r["detail"]:
            print(f"      {d}")

    bad = [r for r in results if r["value"] != r["want"]]
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap = {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "results": results}
    (out / "health_check_latest.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== {len(results) - len(bad)}/{len(results)} 項目が正常 ===")
    # ★明細をCSVに落とす。件数だけ通知しても誰も動けない (2026-08-12)。
    #   health_check が5項目中3項目を恒久NGにしたまま誰にも見られていなかった
    #   のは、較正ミスに加えて「件数しか出していなかった」ことも一因。
    #   通知は「誰が・何を・どこに入れるか」が分かる形にする。
    csv_paths = {}
    for r in results:
        items = r.get("items") or []
        if not items:
            continue
        fn = out / f"要対応_{r['name'][:40]}_{datetime.now():%Y-%m-%d}.csv"
        try:
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(items[0].keys()))
                w.writeheader()
                w.writerows(items)
            csv_paths[r["name"]] = str(fn.resolve())
            print(f"      要対応リスト: {fn.resolve()}")
        except OSError as e:  # noqa: BLE001
            print(f"      ⚠️ 要対応リストの書き出し失敗: {e}")

    if a.slack and bad:
        msg = ["⚠️ 求人・応募データの健全性チェックで異常を検知しました"]
        for r in bad:
            msg.append(f"\n*{r['name']}: {r['value']:,}件* (あるべき値 {r['want']})")
            for d in r["detail"][:3]:
                msg.append(f"　{d}")
            if r.get("action"):
                msg.append(f"　▶ やること: {r['action']}")
            if r.get("impact"):
                msg.append(f"　▶ 放置すると: {r['impact']}")
            if csv_paths.get(r["name"]):
                msg.append(f"　▶ 対象一覧: {csv_paths[r['name']]}")
            for it in (r.get("items") or [])[:3]:
                msg.append("　　- " + " / ".join(
                    f"{k}={v}" for k, v in list(it.items())[:4] if v))
        msg.append("\n※ このチェックは「処理が動いたか」ではなく"
                   "「結果があるべき状態か」を見ています")
        slack_notify("\n".join(msg))
    # チェック自体が失敗したらCIを赤くする (監視の無音故障を作らない)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
