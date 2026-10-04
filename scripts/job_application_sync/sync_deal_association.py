"""LISTING → 取引(Deal) 関連付けスイープ — WBS 1.11.9 要件漏れ是正 (§21.1/§3).

日次同期(hrhacker_import/airwork_import)は LISTING を作るが、取引との関連付けを
作っていなかった(要件漏れ)。そのため新規求人はDealに紐付かず、1次対応連動・
求人情報コピー(get_oubosaki_props)が空になっていた。

本スイープは、Deal関連が無いLISTINGを取引に関連付ける:
  HR: LISTING.id_shop_hrhakkaa → Deal.hrhacker_shop_ids(店舗ID群に含む)
  AW: LISTING.airwork_account_login_id → account_loader管理用メール
      → Deal.kanri_mail_address

2026-10-02 (定例MTG 2026-10-01「関連する取引すべてに紐付ける」):
  手がかりで当たった取引の取引先コードを見て、そのコードの**生きている取引
  すべて**に紐付ける。候補が別の取引先コードにまたがる (別会社と店舗IDを
  共有) ときは紐付けず人の確認へ回す。どの取引を見るかは deal_master.py。
  求人の担当者は、紐付く取引群の最新の生きている取引の担当者に毎晩そろえる。

CLI:
  python sync_deal_association.py [--dry-run|--actual] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_ENV = _REPO / ".env"
if _ENV.exists():
    load_dotenv(_ENV)

from scripts.job_application_sync.fetchers import account_loader as al  # noqa: E402
from scripts.job_application_sync.hs_paging import (  # noqa: E402
    list_all, search_all_by_id)
from scripts.job_application_sync import deal_stages as DS  # noqa: E402
from scripts.job_application_sync import deal_master as DM  # noqa: E402


# Windowsローカルの既定は cp932。ログ出力の1文字で処理全体が落ちるのは
# 本末転倒なので明示的に固定する (CIは PYTHONIOENCODING=utf-8 で問題ない)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://api.hubapi.com"


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}",
            "Content-Type": "application/json"}


def build_login_to_mail() -> dict:
    m = {}
    for a in al.iter_aw_accounts(active_only=False):
        lid = (a.get("login_id") or "").strip()
        km = (a.get("manage_mail") or "").strip().lower()
        if lid and km:
            m.setdefault(lid, km)
    return m


def _post_retry(url: str, body: dict, retries: int = 5) -> dict:
    """通信断・レート制限で落ちないPOST。

    34,718件の走査は347回のリクエストになるため、途中で1回でも
    DNS解決やコネクションが落ちると全体が失敗する。実際に
    2026-08-06 の実行が getaddrinfo failed で中断した。
    ネットワーク例外とHubSpot側の一時エラーは待って再試行する。
    """
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=60)
        except requests.RequestException as e:
            if i < retries:
                wait = 2 ** i
                print(f"    [retry {i+1}/{retries}] {type(e).__name__}: "
                      f"{wait}秒待って再試行", flush=True)
                time.sleep(wait)
                continue
            raise
        if r.status_code in (200, 201, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


# ── 取引の読み込みと索引 ─────────────────────────────────────────────────
# 2026-10-02: 索引を「キー → 取引1件」(setdefault の先勝ち) から
# 「キー → 取引のリスト」に変えた。
#
# 先勝ちだと、同じ店舗ID・管理メールを古い取引と新しい取引が両方持って
# いるとき、検索の既定順 (作成日の昇順) で**古い取引が勝つ**。新しく作られた
# 求人が終わった取引に付き、応募に古い契約の担当者・要否が入っていた
# (2026-10-01 実測: 応募が終わった取引にしか届かない会社 24社のうち、
#  HR求人27件中9件は生きている取引も同じ店舗IDを持っていた)。
#
# どの取引に付けるかは deal_master の規則で決める (2026-10-01 定例MTG:
# 関連する取引すべてに紐付ける)。

DEAL_PROPS = ["dealname", "dealstage", "createdate", "contract_start_date",
              DS.PROP_CODE, "hrhacker_shop_ids", "kanri_mail_address",
              "hubspot_owner_id"]


def load_deals() -> dict:
    """納品管理PLの取引を全件 {deal_id: properties}。

    店舗ID・管理メールを持つ取引は全件この PL にある (2026-10-02 実測:
    店舗ID持ち 1,277/1,277・管理メール持ち 2,147/2,147)。ここで1回だけ読み、
    以後は求人ごとにAPIを呼ばない (旧 _latest_of は求人1件ごとに
    batch/read を投げていた。全取引紐付けで呼び出しが数万回に増えるため)。
    """
    rows = search_all_by_id("0-3", DEAL_PROPS, [
        {"propertyName": "pipeline", "operator": "EQ",
         "value": DS.PIPELINE_NOUHIN}])
    return {str(r["id"]): (r.get("properties") or {}) for r in rows}


def _split(raw: str, lower: bool = False) -> list:
    out = []
    for x in (raw or "").replace(",", ";").split(";"):
        x = x.strip()
        if x:
            out.append(x.lower() if lower else x)
    return out


def build_indexes(deals: dict) -> tuple:
    """(店舗ID → [取引ID], 管理メール → [取引ID])。取引IDは昇順で並べる。

    ★1取引に複数アドレス・複数店舗IDが入る (";" or "," 区切り)。丸ごと
      1キーにすると個別の値で引けない (2026-08-10 実測: 複数持ちの取引27件)。
    """
    shop, mail = {}, {}
    for did in sorted(deals, key=lambda x: int(x) if x.isdigit() else x):
        p = deals[did]
        for sid in _split(p.get("hrhacker_shop_ids")):
            shop.setdefault(sid, []).append(did)
        for km in _split(p.get("kanri_mail_address"), lower=True):
            mail.setdefault(km, []).append(did)
    return shop, mail


# 紐付け先の決まり方 (集計とログに使う)
ST_CODE_LIVE = "同じ取引先コードの生きている取引すべて"
ST_CODE_NOLIVE = "生きている取引が無いので候補の最新"
ST_NOCODE = "取引先コード無しの候補の最新"
ST_MULTI = "候補が別の取引先コードにまたがる(人の確認)"


def resolve_targets(cands: list, deals: dict, by_code: dict) -> tuple:
    """求人の手がかり (店舗ID/管理メール) で当たった候補取引 → 紐付け先。

    Returns: (紐付ける取引IDのリスト, 決まり方)

    - 候補の取引先コードが1つ → そのコードの**生きている取引すべて**。
      生きている取引が無ければ、候補とそのコードの取引のうち最新1件。
    - 候補が別々の取引先コードにまたがる → 紐付けない (人の確認)。
      別会社と店舗IDを共有している (2026-10-01 実測: 94件)。どちらかを
      機械が選ぶと、応募が別会社の条件で対応される。
    - コード無しの取引しか無い → 候補の最新1件 (従来どおり)。
    """
    cands = [c for c in dict.fromkeys(cands) if c in deals]
    if not cands:
        return [], ""
    codes = {str(deals[c].get(DS.PROP_CODE) or "").strip() for c in cands} - {""}
    if len(codes) > 1:
        return [], ST_MULTI
    if len(codes) == 1:
        group = by_code.get(next(iter(codes)), [])
        live = DM.link_targets(group, deals)   # 生きている主契約 (無ければオプション)
        if live:
            return live, ST_CODE_LIVE
        return [DM.latest_live(cands + group, deals)], ST_CODE_NOLIVE
    return [DM.latest_live(cands, deals)], ST_NOCODE


def owner_source(deal_ids: list, deals: dict, by_code: dict):
    """求人の担当者を取る取引。紐付く取引の取引先コードが1つなら、その
    コードの取引群の latest_live。コードが無い/割れているなら紐付く取引の
    latest_live。"""
    ids = [d for d in deal_ids if d in deals]
    if not ids:
        return None
    codes = {str(deals[d].get(DS.PROP_CODE) or "").strip() for d in ids} - {""}
    if len(codes) > 1:
        return None         # 別会社の取引に同時に紐付いている = 担当が決まらない
    pool = list(ids)
    if len(codes) == 1:
        pool += by_code.get(next(iter(codes)), [])
    return DM.latest_live(pool, deals)


def plan_new_links(listings: list, has: dict, shop2deals: dict,
                   mail2deals: dict, login2mail: dict, deals: dict,
                   by_code: dict) -> tuple:
    """まだどの取引にも付いていない求人の紐付け計画 (純関数・APIなし)。

    既に何かに付いている求人はここでは触らない。同じ取引先コードの
    生きている取引への追加紐付けは relink_to_latest_deal の役割。

    Returns: (pairs=[(listing_id, deal_id, path)], stat=Counter, review=[行])
    """
    pairs, review = [], []
    stat = Counter()
    for o in listings:
        lid = str(o["id"])
        if has.get(lid):
            stat["already_linked"] += 1
            continue
        p = o.get("properties") or {}
        shop = (p.get("id_shop_hrhakkaa") or "").strip()
        login = (p.get("airwork_account_login_id") or "").strip()
        cands, path, key = [], "", ""
        if shop and shop in shop2deals:           # HR
            cands, path, key = shop2deals[shop], "hr", shop
        elif login:                               # AW
            km = login2mail.get(login, "")
            if km and km in mail2deals:
                cands, path, key = mail2deals[km], "aw", login
        if not cands:
            stat["unresolved"] += 1
            continue
        targets, how = resolve_targets(cands, deals, by_code)
        stat[how] += 1
        if how == ST_MULTI:
            review.append({
                "求人ID": lid,
                "手がかり": f"{'店舗ID' if path == 'hr' else 'AWログインID'}={key}",
                "候補の取引ID": " / ".join(cands),
                "候補の取引先コード": " / ".join(sorted(
                    {str(deals[c].get(DS.PROP_CODE) or "").strip() or "(無)"
                     for c in cands if c in deals})),
            })
            continue
        for did in targets:
            pairs.append((lid, did, path))
    return pairs, stat, review


def plan_owner(listings: list, l2deals: dict, deals: dict, by_code: dict,
               active_owners: set, shop_index: dict = None) -> list:
    """求人の担当者を、紐付く取引群の latest_live の担当者に**毎晩そろえる**。

    2026-10-02 是正: 旧実装は求人の担当者が空のときだけ埋めていたため、
    最初に付いた取引 (=最初の契約) の担当者のまま更新されなかった
    (2026-10-01 実測: 応募の担当者が無効 211件)。

    書き換えるのは次の両方を満たすときだけ (ユーザー決定 2026-10-02):
      - 取得元が**生きている取引**。契約が終わった会社の求人は、正しい
        担当者が決まらないので触らない (実測: 初回対象の約2,600件)
      - 取得元の担当者が**有効なユーザー**。有効な担当者を無効化ユーザーへ
        書き換えると悪化する (実測: 383件がこの形だった)
    取引の担当者が空なら求人の担当者は消さない。

    Returns: [(listing_id, owner_id)] — 今と違うものだけ
    """
    lowner = {str(o["id"]): ((o.get("properties") or {}).get("hubspot_owner_id") or "")
              for o in listings}
    lshop = {str(o["id"]): ((o.get("properties") or {}).get("id_shop_hrhakkaa") or "")
             for o in listings}
    out = []
    for lid, dids in l2deals.items():
        if shop_index and DM.is_shared_shop(lshop.get(lid), shop_index):
            continue            # 店舗IDを別会社と共有 = どの会社の担当か決まらない
        src = owner_source(dids, deals, by_code)
        if not src or not DM.is_live(deals.get(src)):
            continue
        ow = str((deals.get(src) or {}).get("hubspot_owner_id") or "")
        if ow and ow in active_owners and ow != str(lowner.get(lid, "")):
            out.append((lid, ow))
    return out


def load_active_owners() -> set:
    """有効なユーザー (HubSpot の owners API が既定で返す=無効化されていない)。"""
    out, after = set(), None
    while True:
        params = {"limit": 500}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE}/crm/v3/owners", headers=_h(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out |= {str(o["id"]) for o in j.get("results", [])}
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out


def _existing_deal_assoc(listing_ids: list) -> dict:
    """LISTING → Deal 関連を batch/read。{lid: [deal_id, ...]} (無関連は載らない)。

    ★2026-10-02: 1件に絞らず全部返す。どれを見るかは deal_master が決める。
      旧実装はここで求人1件ごとに _latest_of (取引の batch/read) を呼んでいた。
    """
    has = {}
    for i in range(0, len(listing_ids), 100):
        chunk = listing_ids[i:i + 100]
        r = _post_retry(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                        {"inputs": [{"id": x} for x in chunk]})
        if (i // 100) % 50 == 0:
            print(f"  [assoc] {i:,}/{len(listing_ids):,}", flush=True)
        for res in r.get("results", []):
            ids = [str(t.get("toObjectId")) for t in (res.get("to") or [])
                   if t.get("toObjectId")]
            if ids:
                has[str(res.get("from", {}).get("id"))] = ids
        time.sleep(0.1)
    return has


def associate_batch(pairs: list, sleep: float = 0.25) -> tuple:
    """LISTING→Deal の default 関連を100件ずつ作る。

    Returns: (成功した (listing_id, deal_id) の集合, 失敗件数)

    ★1件ずつの PUT をやめた (2026-10-02)。全取引紐付けで件数が増えるため。
      間隔は 0.2秒以上 (0.1秒で 429 を実測)。
    ★応答の形 (2026-10-04 実物で確認):
      成功 = HTTP 200, results に {from:{id}, to:{id}} が双方向で並ぶ
      無効なIDが1件でも混ざると **100件まとめて HTTP 400** (VALIDATION_ERROR)。
      そのときは1件ずつ作り直し、無効な1件だけを失敗にする。
      成功の判定は results に実際に返った組だけで行う (送っただけで成功にしない)。
    """
    url = f"{BASE}/crm/v4/associations/0-420/0-3/batch/associate/default"

    def _send(chunk: list):
        r = _post_retry(url, {"inputs": [{"from": {"id": lid}, "to": {"id": did}}
                                         for lid, did in chunk]})
        got = {(str(x.get("from", {}).get("id")), str(x.get("to", {}).get("id")))
               for x in (r.get("results") or [])}
        return {(lid, did) for lid, did in chunk if (str(lid), str(did)) in got}

    ok, fail = set(), 0
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        try:
            done = _send(chunk)
        except Exception as e:  # noqa: BLE001
            if "HTTP 400" not in str(e) or len(chunk) == 1:
                fail += len(chunk)
                print(f"    ★紐付け失敗 {len(chunk)}件: {type(e).__name__}: "
                      f"{str(e)[:120]}", flush=True)
                time.sleep(sleep)
                continue
            # 無効なIDが混ざっている。1件ずつ作り直して切り分ける
            print(f"    [split] 400のため{len(chunk)}件を1件ずつ作り直します", flush=True)
            done = set()
            for pair in chunk:
                try:
                    done |= _send([pair])
                except Exception as e1:  # noqa: BLE001
                    print(f"    ★紐付け失敗 求人={pair[0]} 取引={pair[1]}: "
                          f"{str(e1)[:120]}", flush=True)
                time.sleep(sleep)
        ok |= done
        fail += len(chunk) - len(done)
        time.sleep(sleep)
    return ok, fail


def _write_review(rows: list, out_dir: Path) -> Path | None:
    """別会社にまたがる求人の一覧。CI の成果物「要対応リスト」に乗る名前で出す。

    ★公開リポジトリの成果物なので、取引名(=顧客名)は入れずIDとコードだけ。
    """
    if not rows:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"要対応_求人の紐付け先が別会社にまたがる_{datetime.now():%Y-%m-%d}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def run(dry_run: bool = True, limit=None,
        out_dir: Path = _REPO / "data" / "job_application_sync") -> dict:
    _props = ["id_shop_hrhakkaa", "airwork_account_login_id", "id_hrhakkaa",
              "hubspot_owner_id"]
    # Search API は10,000件で HTTP 400 になり、素朴な実装では「もう次が無い」と
    # 区別できず静かに完走扱いになる。上限の無い list API を使う(2026-08-06)。
    listings = list_all("0-420", _props, limit=limit)
    lids = [str(o["id"]) for o in listings]
    print(f"[listing] 対象 {len(lids)}件", flush=True)
    has = _existing_deal_assoc(lids)
    deals = load_deals()
    by_code = DM.group_by_code(deals)
    shop2deals, mail2deals = build_indexes(deals)
    print(f"[deal] 納品管理PL {len(deals):,}件 / 店舗ID索引={len(shop2deals)} / "
          f"管理メール索引={len(mail2deals)} / 取引先コード {len(by_code):,}",
          flush=True)
    login2mail = build_login_to_mail()

    pairs, stat, review = plan_new_links(listings, has, shop2deals, mail2deals,
                                         login2mail, deals, by_code)
    linked_listings = {lid for lid, _d, _p in pairs}
    print(f"[plan] 新規に紐付ける求人 {len(linked_listings):,}件 / "
          f"紐付け {len(pairs):,}本", flush=True)
    for k, n in stat.most_common():
        print(f"   {n:6,}  {k}", flush=True)
    rp = _write_review(review, out_dir)
    if rp:
        print(f"[review] 別会社にまたがる求人 {len(review)}件 → {rp}", flush=True)

    created = {}
    fail = 0
    if dry_run:
        for lid, did, _p in pairs:
            created.setdefault(lid, []).append(did)
    else:
        # 戻せるように、作る紐付けを先に保存する (付け替えと同じ作法)
        out_dir.mkdir(parents=True, exist_ok=True)
        bk = out_dir / f"sync_assoc_backup_{datetime.now():%Y%m%dT%H%M%S}.json"
        bk.write_text(json.dumps([(lid, did) for lid, did, _p in pairs],
                                 ensure_ascii=False), encoding="utf-8")
        print(f"[backup] 作る紐付けを保存: {bk}", flush=True)
        ok, fail = associate_batch([(lid, did) for lid, did, _p in pairs])
        for lid, did in sorted(ok):
            created.setdefault(lid, []).append(did)
    hr_ok = len({lid for lid, _d, p in pairs if p == "hr" and lid in created})
    aw_ok = len({lid for lid, _d, p in pairs if p == "aw" and lid in created})

    # ── 担当者 (2026-07-27 ユーザー要望 / 2026-10-02 毎晩そろえる方式へ) ──
    # 取引→求人→応募者チェーンの1段目。2段目(求人→応募者)は applicant_import。
    l2deals = {k: list(v) for k, v in has.items()}
    for lid, dids in created.items():
        l2deals.setdefault(lid, [])
        l2deals[lid] += [d for d in dids if d not in l2deals[lid]]
    active = load_active_owners()
    if not active:
        # 空なら全件「無効」扱いになり何も書かないだけだが、APIの異常なので明示する
        raise SystemExit("有効なユーザー一覧が空です (owners API の異常)")
    to_set = plan_owner(listings, l2deals, deals, by_code, active,
                        DM.shop_code_index(deals))
    owner_set = 0
    if not dry_run:
        for i in range(0, len(to_set), 100):
            chunk = to_set[i:i + 100]
            try:
                _post_retry(f"{BASE}/crm/v3/objects/0-420/batch/update",
                            {"inputs": [{"id": lid,
                                         "properties": {"hubspot_owner_id": ow}}
                                        for lid, ow in chunk]})
                owner_set += len(chunk)
            except Exception as e:  # noqa: BLE001
                print(f"    ★担当者の更新失敗 {len(chunk)}件: {type(e).__name__}: "
                      f"{str(e)[:120]}", flush=True)
            time.sleep(0.25)
    summary = {"listings": len(lids), "already_linked": stat["already_linked"],
               "hr_associated": hr_ok, "aw_associated": aw_ok,
               "associations": len(pairs), "associate_failed": fail,
               "multi_code_review": len(review),
               "unresolved": stat["unresolved"],
               "owner_target": len(to_set), "owner_set": owner_set,
               "dry_run": dry_run}
    print(f"[sync_deal_association] {summary}", flush=True)
    return summary


def _args(argv=None):
    p = argparse.ArgumentParser(description="LISTING→Deal 関連付けスイープ")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _args()
    res = run(dry_run=a.dry_run, limit=a.limit)
    # 紐付けの失敗を黙って成功にしない (CI の rc に出す)
    sys.exit(1 if res.get("associate_failed") else 0)
