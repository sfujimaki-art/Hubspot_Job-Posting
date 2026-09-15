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

from scripts.job_application_sync.hs_paging import post_retry, search_all  # noqa: E402
from scripts.job_application_sync.listing_stage import (  # noqa: E402
    PROTECTED_STAGES, STATUS_TO_STAGE)

BASE = "https://api.hubapi.com"
PORTAL_ID = "23708633"    # リクロジ事業部。HubSpotレコードURLの組み立てに使う
RECENT_DAYS = 14          # 「最近作られた」の窓
UNLINKED_DAYS = 30        # 「直近に応募が来た」の窓 (顧客単位の要対応リスト)
SEARCH_CAP = 10000        # Search API の上限
SEARCH_WARN = 9000        # ここを超えたら「いずれ静かに壊れる」と警告
JST = timezone(timedelta(hours=9))

PIPELINE_NOUHIN = "21596025"   # リクロジ_納品管理
# ★「死んだ取引」= 契約がここで終わっているステージ。ステージIDで持つ。
#   ラベル部分一致(`"解約" in label`)にしない理由: 実測で
#   「Tヨミ：10％未満（継続に対して否定的意見、解約意向がある提案（10％未満））」
#   という**アクティブな**ヨミ段階が引っかかる。ラベルは人が編集するので、
#   文言に依存すると母数が黙って動く。
#   (2026-08-17 GET /crm/v3/pipelines/0-3/21596025 実測で全30ステージを確認)
DEAD_STAGES = {
    "1016664339": "解約済架電禁止先",
    "90598807": "解約済（充足）",
    "52016159": "解約済(成果不足)",
    "52016158": "解約済(会社方針・その他)",
    "66848546": "継続済",          # 契約が新しい取引へ移った = この取引はもう動かない
}
#: 直近1日あたりの応募の実測水準 (2026-08-17: 直近30日で2,227件 ≒ 74件/日)。
#: 下限監視に使う。0件は「静かになった」のであって「正常」ではない。
APPS_PER_DAY_FLOOR = 5


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _jst_today() -> datetime:
    """CIの runner は UTC。ファイル名の日付が現場の見る日付とズレないようにする。

    cron `40 16 * * *` は JST 翌01:40 に着弾するので、UTCの日付を使うと
    成果物のファイル名だけ**前日**になる。通知は今日の話をしているのに
    ファイルは昨日の日付、という状態を作らない。
    """
    return datetime.now(JST)


def _window_start_ms(days: int) -> tuple:
    """応募日(date型)の窓の開始を UTC 0時に丸めて返す → (epochms, 開始日, 終了日)。

    ★実行時刻で窓が1日ぶれるのを止める (2026-08-17)。yingmuri は date型で
      UTC 0時に格納されているのに、従来は `now(utc) - 30日` の**時刻付き**
      epoch を GTE に渡していた。実測: 同じ 8/17 でも 07:00 JST 実行なら
      2,227件、10:00 JST 実行なら 2,182件 (-45件 = 応募日 2026-07-18 の全件)。
      件数の増減を見る運用にすると、この時刻由来のぶれと実体の増減が
      区別できない。日付境界に丸めて、窓の実日付も呼び出し側へ返す。
      days=30 なら「今日を含む30日」= 開始は today-29日。
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(days, 1) - 1)
    ms = int(datetime(start.year, start.month, start.day,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return ms, start, end


def slack_notify(message: str) -> bool:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:300]}", flush=True)
        return False
    try:
        r = requests.post(url, json={"text": message}, timeout=15)
    except requests.RequestException as e:
        print(f"[slack送信失敗] {type(e).__name__}: {e}", flush=True)
        return False
    if r.status_code != 200:
        # ★失効(403)・削除(404)・payload拒否(400)を無音にしない。
        #   送れなかった日から誰にも届かないのに、ログは成功時と1文字も
        #   変わらなかった (2026-08-17 実測: 403/404/400 いずれも rc=0)。
        print(f"[slack送信失敗] HTTP {r.status_code}: {r.text[:200]}", flush=True)
        return False
    return True


def _total(obj: str, filters: list) -> int:
    """Search の総件数だけを取る (1件だけ引いて total を読む)。"""
    j = post_retry(f"{BASE}/crm/v3/objects/{obj}/search",
                   {"filterGroups": [{"filters": filters}],
                    "properties": ["hs_object_id"], "limit": 1}, timeout=30)
    return int(j.get("total") or 0)


#: batch/read の 207 errors[] のうち「良性(=本当に関連が無い)」を表す subCategory。
#: HubSpot は同じ 207 の errors[] に「関連が無い(良性)」と「読み取り自体が
#: 失敗した」を混ぜて返す。区別しないと**読めなかった求人を未紐付けとして数える**。
#:
#: ★判定に使うのは category ではなく **subCategory** (2026-08-17 実測)。
#:   関連が無いだけの正常系も category は "OBJECT_NOT_FOUND" で返る:
#:     {"category":"OBJECT_NOT_FOUND",
#:      "subCategory":"crm.associations.NO_ASSOCIATIONS_FOUND",
#:      "message":"No deal is associated with listing 566274304862."}
#:   一方、壊れたIDは
#:     {"category":"VALIDATION_ERROR",
#:      "subCategory":"crm.associations.INVALID_IDENTIFIER"}
#:   category で見ると前者まで異常扱いになり、実データで89件が誤検知した。
_BENIGN_ASSOC_SUBCATEGORIES = {"crm.associations.NO_ASSOCIATIONS_FOUND"}


def _assoc(from_type: str, to_type: str, ids: list) -> dict:
    """{from_id: [to_id,...]} を batch/read で取る。

    ★リトライ付きの post_retry を使う。監視は日次処理の末尾に走るので、
      429 が1回出ただけで落ちると「監視が赤い」だけが残る (2026-08-17 実測:
      429 で RuntimeError → Slackに英語の関数名と -1件 が投稿された)。
    ★207 の errors[] を検査する。良性(関連が無い)以外が混ざったら例外にする。
      黙って「未紐付け」に数えると、読めなかった日に誰も気づけない。
    """
    m: dict = {}
    bad: list = []
    for i in range(0, len(ids), 100):
        j = post_retry(
            f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
            {"inputs": [{"id": x} for x in ids[i:i + 100]]}, timeout=60)
        for res in j.get("results", []):
            m[str(res["from"]["id"])] = [str(t["toObjectId"])
                                         for t in (res.get("to") or [])]
        for err in (j.get("errors") or []):
            sub = err.get("subCategory") or ""
            if sub not in _BENIGN_ASSOC_SUBCATEGORIES:
                bad.append(f"{err.get('category')}/{sub}: "
                           f"{str(err.get('message'))[:80]}")
        time.sleep(0.1)
    if bad:
        raise RuntimeError(
            f"assoc {from_type}->{to_type} で読み取り失敗 {len(bad)}件 "
            f"(例: {bad[0]})。未紐付けと区別できないので中断します")
    return m


def _batch_props(obj: str, ids: list, props: list) -> dict:
    out: dict = {}
    ids = sorted(set(str(x) for x in ids))
    for i in range(0, len(ids), 100):
        j = post_retry(f"{BASE}/crm/v3/objects/{obj}/batch/read",
                       {"properties": props,
                        "inputs": [{"id": x} for x in ids[i:i + 100]]},
                       timeout=60)
        for o in j.get("results", []):
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


_HINT_CACHE: dict | None = None


def _reset_caches() -> None:
    """テスト用。索引のプロセス内キャッシュを捨てる。"""
    global _HINT_CACHE, _MAIL_CACHE
    _HINT_CACHE = None
    _MAIL_CACHE = None


def _company_hint() -> dict:
    """識別子 → 会社名候補 の索引を作る (best-effort)。

    ★「どの取引に入れるのか」が分からないと現場は動けない。
      しかし LISTING に会社名の列は無く、応募側の応募先取引名も
      **取引に紐付いていないから空**という循環になっている。
      そこで外側から引く:
        AW: airwork_account_login_id → 顧客管理シートの会社名
        HR: 店舗id → HR求人CSVの連絡先メール → シートのリクロジアドレス → 会社名

    ★1社に潰さない (2026-08-17)。従来は `idx.setdefault(k, comp)` の先勝ちで、
      さらに元の AccountResolver.idx_* が後勝ちdictなので**二重に潰していた**。
      実測: 顧客管理シート2,017行のうち126キーが2社以上で共用されており、
      そのすべてで1社だけを表示していた。この検査の「やること」は
      **会社名で取引を開いて鍵を入れる**なので、外れた行では人が別事業所の
      取引に書き込む。空欄より悪い。候補を全部持ち、2社以上なら会社名を
      出さず「候補が複数」として人へ回す (誤配より未解決が安全)。
      applicant_queue が同じ理由で idx_mail_multi を用意しているのに、
      ここだけその対策を通っていなかった。

    ★クローズ済み(解約済)顧客の行も索引には入れるが印を付ける。落とすと
      「名前が引けない」に化けて、なぜ引けないのかが分からなくなる。

    戻り値: {"idx": {key: [(会社名, closed:bool), ...]}, "ok": bool,
             "hr": {…pickの診断…}}
    ★2回目以降はプロセス内キャッシュを返すが、**失敗した結果はキャッシュしない**。
      以前は最初の失敗(空dict)が固定され、同一プロセスの後段チェックが
      作り直して回復することもできなかった。
    """
    global _HINT_CACHE
    if _HINT_CACHE is not None:
        return _HINT_CACHE
    res = {"idx": {}, "ok": False, "hr": {}}
    idx: dict = {}
    try:
        from scripts.job_application_sync import applicant_queue as _aq
        from scripts.job_application_sync import hr_offers_csv as _hr
        os.environ.setdefault("SHEETS_AUTH_MODE", "sa")
        r = _aq.AccountResolver().build()
        c = r.cols
        ci, cl = c["comp"], c.get("closed")

        def _add(key: str, row: list) -> None:
            if not key:
                return
            comp = row[ci] if len(row) > ci else ""
            if not comp:
                return
            closed = (cl is not None and len(row) > cl
                      and str(row[cl]).strip().upper() == "TRUE")
            lst = idx.setdefault(key, [])
            if (comp, closed) not in lst:
                lst.append((comp, closed))

        # AW: login_id / エイリアス / リクロジアドレス → 会社名
        for src in (r.idx_bid, r.idx_aid, r.idx_reclog, r.idx_alias):
            for k, row in src.items():
                _add(k, row)
        # ★後勝ちで潰れた候補を拾い直す。idx_mail_multi は
        #   「同じキーを共用する行を全部持つ」ために作られた索引。
        for k, rows in getattr(r, "idx_mail_multi", {}).items():
            for row in rows:
                _add(k, row)
        # HR: 店舗id → 連絡先メール → (上の索引で) 会社名
        hr = _hr.pick(_REPO / "scratchpad" / "csv_fetched" / "hr",
                      today=_jst_today().date())
        res["hr"] = hr
        for w in hr.get("warnings") or []:
            print(f"      (HR求人CSV) {w}", flush=True)
        if hr.get("path"):
            for sid, mails in _hr.load_shop_to_mail(hr["path"]).items():
                for mail in mails:
                    for pair in idx.get(mail) or []:
                        lst = idx.setdefault(f"shop:{sid}", [])
                        if pair not in lst:
                            lst.append(pair)
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        # ★失敗はキャッシュしない。一時障害を恒久化させない。
        print(f"      (会社名の索引を作れませんでした: {type(e).__name__}: {e})",
              flush=True)
        return {"idx": {}, "ok": False, "hr": {}}
    res["idx"] = idx
    _HINT_CACHE = res
    return res


def _company_of(hint: dict, key: str) -> tuple:
    """会社名を1つに決める。決まらなければ空を返す。

    戻り値: (会社名, 注記) — 注記は「候補が複数」「シート上はクローズ済」等。
    """
    cands = (hint.get("idx") or {}).get(key) or []
    if not cands:
        return "", ""
    live = [c for c, cl in cands if not cl]
    dead = [c for c, cl in cands if cl]
    names = live or dead
    uniq = sorted(set(names))
    if len(uniq) > 1:
        # 決めない。人が「候補」を見て判断する
        return "", "候補が複数: " + " / ".join(uniq[:4])
    return uniq[0], ("" if live else "シート上はクローズ済(解約済顧客)")


_MAIL_CACHE: dict | None = None


def _manage_mail_hint() -> dict:
    """AWログインID → 取引に入れる管理用メール(rpo.medica+…) の索引。

    ★再実装しない。sync_deal_association が本番の紐付けで使っている索引を
      そのまま借りる。紐付けが「どの値を見ているか」と、現場に「入れてください」と
      伝える値がズレると、入れても紐付かない指示を出すことになる。

    ★**キーを小文字化しない** (2026-08-17 是正)。本番の
      sync_deal_association.run() は `login2mail.get(login, "")` で
      大小文字を区別して引く。ここで lower() すると、シートが
      `Molino-saiyou` で求人が `molino-saiyou` のとき「入れてください」と
      案内するのに本番は引けない = 入れても紐付かない指示になる。
      実測: 索引831件中84件(10.1%)の login_id が大文字を含む。
      代わりに小文字索引を**別に**持ち、完全一致で外れて小文字一致で当たる
      ものは「表記が違う」という別の区分に回す。

    戻り値: {"exact": {…}, "lower": {…}, "ok": bool}
    ★失敗を {} で返すと「シートに未登録」と区別できない。ok で区別する。
    """
    global _MAIL_CACHE
    if _MAIL_CACHE is not None:
        return _MAIL_CACHE
    try:
        from scripts.job_application_sync.sync_deal_association import (
            build_login_to_mail)
        raw = {k.strip(): v for k, v in build_login_to_mail().items() if k.strip()}
    except Exception as e:  # noqa: BLE001
        print(f"      (管理用メールの索引を作れませんでした: {type(e).__name__}: {e})",
              flush=True)
        return {"exact": {}, "lower": {}, "ok": False}
    low: dict = {}
    for k, v in raw.items():
        low.setdefault(k.lower(), v)
    res = {"exact": raw, "lower": low, "ok": True}
    _MAIL_CACHE = res
    return res


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
    # ★明細(items)はここでは返さない (2026-08-17)。
    #   この項目と「直近30日に応募が来た求人で取引未紐付けの顧客」は同じ根本原因で
    #   必ず同時にNGになり、対象も重なる。両方が明細CSVとSlack上位5件を出すと、
    #   受け手には別々の2依頼に見え、しかもこちらは顧客単位に畳まれていないぶん
    #   行数だけ多い。**現場への依頼は顧客単位の1本に統一**し、この項目は
    #   停止検知(日々増えるかどうか)に役割を絞る。
    return {"name": f"直近{RECENT_DAYS}日の求人が取引に紐付いているか",
            "value": len(unlinked), "want": 0,
            "detail": [f"応募が来ている公開中の求人 {n_target:,}件中 "
                       f"未紐付け {len(unlinked):,}件 "
                       f"(作成された求人は {len(ids):,}件。他社求人を含むため母数から除外)",
                       "この項目は停止検知用です。何をどこに入れるかは"
                       f"「直近{UNLINKED_DAYS}日に応募が来た求人で取引未紐付けの顧客」"
                       "の要対応リストを見てください"]
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


_LISTING_PROPS = ["hs_name", "kyuujin_status", "id_hrhakkaa", "id_airwork",
                  "id_shop_hrhakkaa", "airwork_account_login_id",
                  # ★会社名が引けない行の救済 (2026-08-17)。会社名の逆引きは
                  #   best-effort で空になる行がある。媒体側の求人URLがあれば
                  #   人は求人を開いて顧客を特定できる。
                  "url_hrhakkaa", "url_airwork"]

_DEAL_PROPS = ["dealname", "pipeline", "dealstage", "hubspot_owner_id"]


def _is_dead_deal(props: dict) -> bool:
    """納品管理PLで契約が終わっているステージなら True。

    ★他パイプラインの取引は「生きている」側に倒す。判定材料が無い取引を
      勝手に死んだことにすると要対応リストが水増しされる。実測 2026-08-17:
      未紐付け判定に絡む取引で納品管理PL外のものは0件だった。
    """
    if (props.get("pipeline") or "") != PIPELINE_NOUHIN:
        return False
    return (props.get("dealstage") or "") in DEAD_STAGES


def _owner_names() -> dict:
    """owner_id → 名前。1回のGETで全件取れる。失敗しても握る(名前が出ないだけ)。"""
    try:
        r = requests.get(f"{BASE}/crm/v3/owners", headers=_h(),
                         params={"limit": 500}, timeout=30)
        if r.status_code != 200:
            return {}
        out = {}
        for o in r.json().get("results", []):
            nm = " ".join(x for x in [o.get("lastName"), o.get("firstName")] if x)
            out[str(o.get("id"))] = nm or (o.get("email") or "")
        return out
    except requests.RequestException:
        return {}


def _norm_company(s: str) -> str:
    """会社名の表記ゆれを吸収する。applicant_queue の実装を借りる。

    ★再実装しない。同じシートの同じ列を別ルールで正規化すると、
      「同じ顧客か」の判定が場所ごとに変わる。
    """
    try:
        from scripts.job_application_sync.applicant_queue import (
            _norm_company as _nc)
        return _nc(s)
    except Exception:  # noqa: BLE001  # pragma: no cover
        import re as _re
        return _re.sub(r"[\s　]", "", str(s or "")).lower()


def collect_unlinked_customers(days: int = UNLINKED_DAYS) -> dict:
    """「人が取引に鍵を入れる」要対応リストを顧客単位で作る (2026-08-17)。

    ## なぜ求人単位ではなく顧客単位か

    紐付けの鍵は求人ごとではなく**顧客ごと**に1つしか無い
    (HR=取引の店舗ID群 / AW=取引の管理用メール)。1顧客に未紐付け求人が
    5件あっても、人がやる作業は1回。求人単位で出すと同じ作業を5行に
    分けて依頼することになり、実際の作業量を見誤る。

    ## 絞り込みの順序 (母数の定義)

      1. 直近N日に**応募が来た**応募      … 軸は yingmuri(応募日)。
         hs_createdate(登録日)ではない。登録日だと過去分の一括取込で
         「媒体側に求人が既に無い救済不能な応募」が母数へ大量に混ざる
      2. その応募に紐付く求人
      3. その求人が**生きている取引に紐付いていない**もの
      4. **公開終了を除く**              … 終わった求人に鍵を入れても意味が無い
      5. 鍵(HR店舗ID / AWログインID)単位 → さらに会社名で畳む

    ## ★「取引に紐付いているか」だけでは足りない (2026-08-17 是正)

    従来は `not l2d.get(listing_id)` だけを見ていた。しかし納品管理PLの取引は
    契約更新のたびに新しく作られ、前の取引は「継続済」へ移る。**死んだ取引に
    だけ紐付いている求人**は、紐付きが在るので「健全」として母数から消えていた。

      実測 2026-08-17: 取引に紐付いている877求人のうち193件が死んだ取引にだけ
      紐付き(継続済149 / 解約済44)。応募463件。公開中に限れば求人141件/応募376件。
      うち114件は同じ会社+拠点に**生きた後継取引が実在**する誤紐付け。
      例: ある求人が「サブスク継続⑦＿○○」(継続済) に紐付いたままで、
          同じ会社の「サブスク継続⑧＿○○」が生きている、という形。

    報告されていた75求人/178応募より、黙って落ちていた側のほうが大きかった。
    死んだ取引にだけ紐付く求人も母数に入れ、対応区分で区別する
    (やることが「鍵を入れる」ではなく「生きている取引へ鍵を入れ替える」なので)。

    戻り値: {"funnel": {段階名: 件数}, "meta": {…}, "rows": [1顧客1行], "diag": {…}}
    """
    since, d_from, d_to = _window_start_ms(days)
    # yingmuri は date型。範囲指定は **epochミリ秒の文字列**。ISO文字列は HTTP 400。
    apps = search_all("0-421", ["yingmuri"],
                      [{"propertyName": "yingmuri", "operator": "GTE",
                        "value": str(since)}])
    app_ids = [str(o["id"]) for o in apps]
    funnel = {f"応募(応募日 {d_from}〜{d_to})": len(app_ids),
              "応募が紐付く求人": 0,
              "生きた取引に紐付いていない求人": 0,
              "公開終了を除く": 0,
              "要対応の鍵": 0,
              "要対応の顧客(会社に畳んだ後)": 0}
    meta = {"窓": f"{d_from}〜{d_to} ({days}日間)",
            "求人に紐付かない応募": 0,
            "死んだ取引にだけ紐付く求人": 0,
            "取引が1件も無い求人": 0,
            "求人が取得できず除外": 0,
            # ★応募が0件は「正常」ではなく「静かになった」。下限監視のフラグ。
            "入力ゼロ": not app_ids}
    if not app_ids:
        return {"funnel": funnel, "meta": meta, "rows": [], "diag": {}}

    # 応募 → 求人。「どの求人に何件の応募が来ているか」をここで数える
    apps_of: dict = {}
    linked_apps = set()
    for aid, lids in _assoc("0-421", "0-420", app_ids).items():
        for lid in lids:
            apps_of.setdefault(str(lid), set()).add(str(aid))
            linked_apps.add(str(aid))
    listing_ids = sorted(apps_of)
    funnel["応募が紐付く求人"] = len(listing_ids)
    # ★2,227 → 970 は脱落ではなく**応募を求人に集約しただけ**。矢印で連結すると
    #   1,257件が落ちたと誤読される。落ちた件数は別に数えて明示する。
    meta["求人に紐付かない応募"] = len(app_ids) - len(linked_apps)
    if not listing_ids:
        return {"funnel": funnel, "meta": meta, "rows": [], "diag": {}}

    l2d = _assoc("0-420", "0-3", listing_ids)
    deal_ids = sorted({d for v in l2d.values() for d in v})
    dprops = _batch_props("0-3", deal_ids, _DEAL_PROPS) if deal_ids else {}
    unlinked, dead_of = [], {}
    for lid in listing_ids:
        ds = l2d.get(lid) or []
        live = [d for d in ds if not _is_dead_deal(dprops.get(d) or {})]
        if live:
            continue
        unlinked.append(lid)
        if ds:
            dead_of[lid] = ds
            meta["死んだ取引にだけ紐付く求人"] += 1
        else:
            meta["取引が1件も無い求人"] += 1
    funnel["生きた取引に紐付いていない求人"] = len(unlinked)
    if not unlinked:
        return {"funnel": funnel, "meta": meta, "rows": [], "diag": {}}

    props = _batch_props("0-420", unlinked, _LISTING_PROPS)
    live_listings = [x for x in unlinked
                     if x in props and props[x].get("kyuujin_status") != "公開終了"]
    funnel["公開終了を除く"] = len(live_listings)
    # 消えた求人 (batch/read に載らない = アーカイブ/削除済) は鍵を入れても直らない
    meta["求人が取得できず除外"] = len(unlinked) - sum(1 for x in unlinked
                                                       if x in props)
    if not live_listings:
        return {"funnel": funnel, "meta": meta, "rows": [], "diag": {}}

    hint = _company_hint()
    mails = _manage_mail_hint()
    owners = _owner_names()

    # ── 鍵(HR店舗ID / AWログインID)単位に集約 ──────────────────────────
    groups: dict = {}
    for lid in live_listings:
        p = props[lid]
        shop = (p.get("id_shop_hrhakkaa") or "").strip()
        login = (p.get("airwork_account_login_id") or "").strip()
        is_hr = bool((p.get("id_hrhakkaa") or "").strip())
        if is_hr and shop:
            key, media, keyval = ("hr", shop), "HRハッカー", shop
        elif login:
            key, media, keyval = ("aw", login.lower()), "AirWork", login
        elif shop:
            key, media, keyval = ("hr", shop), "HRハッカー", shop
        else:
            # 鍵そのものが求人に無い。人には直せないので取込側の不具合として出す
            key, media, keyval = ("unknown", lid), \
                ("HRハッカー" if is_hr else "AirWork"), ""
        g = groups.setdefault(key, {"媒体": media, "鍵": keyval, "求人": [],
                                    "応募": set(), "死": set()})
        g["求人"].append(lid)
        g["応募"] |= apps_of.get(lid, set())
        for d in dead_of.get(lid, []):
            g["死"].add(d)
    funnel["要対応の鍵"] = len(groups)

    keyrows = []
    for (kind, _k), g in groups.items():
        keyval = g["鍵"]
        note = ""
        owner = ""
        # ── 会社名 ──────────────────────────────────────────────────
        if kind == "hr":
            comp, note = _company_of(hint, f"shop:{keyval}")
        elif kind == "aw":
            comp, note = _company_of(hint, keyval.lower())
        else:
            comp, note = "", ""
        # ★死んだ取引に紐付いている行は、**取引名がそのまま会社名の証拠**になる。
        #   シートの逆引きより強い根拠なので優先する (推測が入らない)。
        dead_desc = []
        for d in sorted(g["死"]):
            dp = dprops.get(d) or {}
            dead_desc.append(f"{dp.get('dealname','')}"
                             f"[{DEAD_STAGES.get(dp.get('dealstage',''), '')}]"
                             f"(ID {d})")
            if not owner and dp.get("hubspot_owner_id"):
                owner = owners.get(str(dp["hubspot_owner_id"]),
                                   str(dp["hubspot_owner_id"]))
        if g["死"] and not comp:
            first = dprops.get(sorted(g["死"])[0]) or {}
            if first.get("dealname"):
                comp = first["dealname"]
                note = "終了した取引の名前から (顧客名の表記は取引名のまま)"
        # ── 対応区分と入れる場所 ────────────────────────────────────
        if kind == "unknown":
            kubun = "求人に鍵が無い（取込の不具合）"
            place = ("この求人に店舗ID/AWログインIDが入っていない。"
                     "人では直せないので求人取込の不具合として調査する")
        elif kind == "hr":
            kubun = "取引に店舗IDを入れる"
            place = ("取引の「HRハッカー店舗ID（複数可・;区切り）」に "
                     f"{keyval} を ; で追記")
        else:
            # ★本番(sync_deal_association)は完全一致で引く。ここも完全一致で見る。
            km = (mails.get("exact") or {}).get(keyval, "")
            km_low = (mails.get("lower") or {}).get(keyval.lower(), "")
            if not mails.get("ok"):
                # ★索引を作れなかったことを「シートに無い」と断定しない。
                #   壊れているのは Sheets の認証/クォータ/通信であってシートではない。
                kubun = "判定不能（管理用メールの索引を作れず）"
                place = ("顧客管理シートを読めませんでした。この行の指示は"
                         "信頼できません。索引の失敗を先に直してください")
            elif km:
                kubun = "取引に管理用メールを入れる"
                place = ("取引の「管理用メールアドレス（rpo.medica+／複数可・;区切り）」に "
                         f"{km} を ; で追記")
            elif km_low:
                kubun = "シートと求人でAWログインIDの大小文字が違う（シートの表記を合わせる）"
                place = (f"求人側のログインIDは「{keyval}」ですが、顧客管理シートは"
                         f"別の表記で登録されています。本番の紐付けは大小文字を"
                         f"区別するため、シート側を「{keyval}」に揃えてください"
                         f"（現在の値で引ける管理用メール: {km_low}）")
            else:
                kubun = "顧客管理シートに未登録（先にシートを直す）"
                place = ("このAWログインIDは顧客管理シートに1行も無い。"
                         "取引ではなく先にシートへ行を足す（宛先はシート管理者）")
        if g["死"]:
            # 鍵は既にどこかに在る。無い場所へ入れるのではなく**移す**作業。
            kubun = "終了した取引にだけ紐付いている（生きている取引へ鍵を入れ替える）"
            place = ("現在の紐付け先は終了した取引です: " + " / ".join(dead_desc[:2])
                     + "。同じ顧客の**生きている**取引を開き、"
                     + (f"HRハッカー店舗ID に {keyval} を追記"
                        if kind == "hr" else
                        "管理用メールアドレス に同じアドレスを追記")
                     + "してください")

        names = [(props[x].get("hs_name") or "")[:40] for x in g["求人"]]
        urls = [(props[x].get("url_hrhakkaa") or props[x].get("url_airwork") or "")
                for x in g["求人"]]
        keyrows.append({
            "_comp_key": _norm_company(comp) if comp else "",
            "会社名": comp,
            "対応区分": kubun,
            "媒体": g["媒体"],
            "入れる鍵": keyval,
            "入れる場所": place,
            "応募数": len(g["応募"]),
            "影響する求人数": len(g["求人"]),
            "作業回数": 1,
            "現在の紐付け先(終了した取引)": " / ".join(dead_desc[:3]),
            "担当者(候補)": owner,
            "求人名の例": " / ".join([n for n in names if n][:3]),
            "求人URL(例)": next((u for u in urls if u), ""),
            # ★どの行にも必ず1つは開けるリンクがある状態にする。媒体側URLが
            #   空の求人が実在する(実測4件)。ポータルIDは固定なのでAPI呼び出しゼロ。
            "HubSpot求人リンク":
                f"https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-420/{g['求人'][0]}",
            "会社名の注記": note,
            "求人ID": ";".join(g["求人"][:10]),
            "_apps": g["応募"],
        })

    # ── 会社名で二次集約 ────────────────────────────────────────────
    # ★「顧客63件」は顧客数ではなく鍵の数だった (2026-08-17 是正)。
    #   実測: 会社名が引けた30行の実会社数は22社。1社が複数店舗IDを持つ
    #   (不二興産 1521249/1521250 等) / HRとAWの両方で未紐付け
    #   (不二家平塚工場 = HR 1207059 + AW ryou.sawaki@…) で行が割れていた。
    #   同じ取引を2回開かせる指示になるので、会社名が判明した行は畳む。
    #   会社名が引けない行は同一かどうか判定できないので畳まない(推測しない)。
    merged: dict = {}
    rows = []
    for r in keyrows:
        ck = r.pop("_comp_key")
        if not ck:
            r["応募数"] = len(r.pop("_apps"))
            rows.append(r)
            continue
        cur = merged.get(ck)
        if cur is None:
            merged[ck] = r
            continue
        cur["_apps"] |= r.pop("_apps")
        cur["影響する求人数"] += r["影響する求人数"]
        cur["作業回数"] += 1
        for col, sep in (("媒体", " / "), ("入れる鍵", " / "),
                         ("対応区分", " ＋ "), ("入れる場所", " ／ "),
                         ("現在の紐付け先(終了した取引)", " / "),
                         ("求人名の例", " / ")):
            a, b = cur.get(col, ""), r.get(col, "")
            if b and b not in a:
                cur[col] = f"{a}{sep}{b}" if a else b
        for col in ("担当者(候補)", "求人URL(例)", "会社名の注記"):
            if not cur.get(col) and r.get(col):
                cur[col] = r[col]
        cur["求人ID"] = ";".join((cur["求人ID"] + ";" + r["求人ID"]).split(";")[:10])
    for r in merged.values():
        r["応募数"] = len(r.pop("_apps"))
        rows.append(r)
    funnel["要対応の顧客(会社に畳んだ後)"] = len(rows)

    # 応募が多い順 = 放置の実害が大きい順。上位数件をSlackに出すのでここで決まる
    rows.sort(key=lambda r: (-r["応募数"], -r["影響する求人数"], r["会社名"]))
    diag = {"会社名なし": sum(1 for r in rows if not r["会社名"]),
            "会社名の候補が複数": sum(1 for r in rows
                                      if r["会社名の注記"].startswith("候補が複数")),
            "会社名の索引": "OK" if hint.get("ok") else "作成失敗",
            "管理用メールの索引": "OK" if mails.get("ok") else "作成失敗",
            "HR店舗索引": sum(1 for x in (hint.get("idx") or {})
                              if x.startswith("shop:")),
            "HR求人CSV": ((hint.get("hr") or {}).get("path").name
                          if (hint.get("hr") or {}).get("path") else "無し")}
    return {"funnel": funnel, "meta": meta, "rows": rows, "diag": diag}


def check_unlinked_listings_by_customer() -> dict:
    """↑を health_check の1項目として包む。"""
    res = collect_unlinked_customers()
    rows, f, meta = res["rows"], res["funnel"], res.get("meta") or {}
    diag = res.get("diag") or {}
    n_app = sum(r["応募数"] for r in rows)
    n_job = sum(r["影響する求人数"] for r in rows)

    # ★矢印で連結しない。段ごとに「何を数えたか」を書く (2026-08-17)。
    #   「応募2,227 → 求人970」は脱落ではなく集約なので、矢印だと
    #   1,257件が落ちたと誤読される。
    keys = list(f.keys())
    detail = [f"{keys[0]}: {f[keys[0]]:,}件"]
    if meta.get("求人に紐付かない応募"):
        detail[-1] += f" (うち求人に紐付かない応募 {meta['求人に紐付かない応募']:,}件)"
    detail += [f"  └ 求人に集約: {f['応募が紐付く求人']:,}求人",
               f"  └ 生きた取引に紐付いていない: "
               f"{f['生きた取引に紐付いていない求人']:,}求人 "
               f"(取引が1件も無い {meta.get('取引が1件も無い求人', 0):,} / "
               f"終了した取引にだけ紐付き {meta.get('死んだ取引にだけ紐付く求人', 0):,})",
               f"  └ 公開終了を除く: {f['公開終了を除く']:,}求人",
               f"  └ 要対応の鍵: {f['要対応の鍵']:,}件 → "
               f"会社に畳んで {f['要対応の顧客(会社に畳んだ後)']:,}件"]

    # ★応募が0件は「正常」ではない。取込が止まると検知が鳴るどころか静かになる、
    #   というのが health_check がそもそも直そうとした失敗形 (2026-08-17)。
    if meta.get("入力ゼロ"):
        return {
            "name": f"直近{UNLINKED_DAYS}日に応募が来た求人で取引未紐付けの顧客",
            "value": -1, "want": 0,
            "detail": [f"★{meta.get('窓')} の応募が **0件**。"
                       f"実測水準は約74件/日 (直近30日で2,227件) なので、"
                       f"応募の取込が止まっている疑いがあります",
                       "母数が0なので未紐付けの判定はできていません"],
            "action": "応募取込 (applicant_sync) が動いているかを先に確認する",
            "impact": "この項目が0件で『正常』に見えている間、未紐付けは検知できません",
        }
    if rows:
        detail.append(f"影響: 求人 {n_job:,}件 / 応募 {n_app:,}件")
        by: dict = {}
        for r in rows:
            by[r["対応区分"]] = by.get(r["対応区分"], 0) + 1
        detail.append("内訳: " + " / ".join(f"{k} {v:,}件" for k, v in
                                            sorted(by.items(), key=lambda x: -x[1])))
    if meta.get("求人が取得できず除外"):
        detail.append(f"(求人を取得できず除外: {meta['求人が取得できず除外']:,}件"
                      " — アーカイブ/削除済。鍵を入れても直らない)")
    # ★索引が作れなかったことを黙らない。空欄や区分が化ける原因はここにある。
    if diag.get("会社名の索引") != "OK":
        detail.append("★会社名の索引を作れませんでした（顧客管理シート/HR求人CSV）。"
                      "会社名は全て空です — シート側の不備ではありません")
    if diag.get("管理用メールの索引") != "OK":
        detail.append("★管理用メールの索引を作れませんでした。AirWork行の対応区分は"
                      "「判定不能」にしてあります（『シートに未登録』ではありません）")
    if diag.get("会社名なし"):
        msg = (f"会社名を逆引きできず空欄: {diag['会社名なし']:,}件 "
               "(「HubSpot求人リンク」から求人を開いて特定してください)")
        if diag.get("会社名の候補が複数"):
            msg += (f" ／ うち {diag['会社名の候補が複数']:,}件は候補が複数あるため"
                    "あえて出していません（別事業所に書き込む事故を防ぐため）")
        if not diag.get("HR店舗索引") and any(r["媒体"].startswith("HR")
                                              for r in rows):
            msg += (f" ※HR求人CSV={diag.get('HR求人CSV')} のため"
                    "HRの会社名は引けていません")
        detail.append(msg)
    return {
        "name": f"直近{UNLINKED_DAYS}日に応募が来た求人で取引未紐付けの顧客",
        "value": len(rows), "want": 0,
        "items": rows,
        # ★Slackに出す列を明示する。先頭N列という暗黙のルールだと、列を足した
        #   瞬間に「対応区分」や「入れる場所」が通知から消える (2026-08-17)。
        "slack_cols": ["会社名", "対応区分", "入れる鍵", "応募数", "影響する求人数"],
        "item_key": "入れる鍵",
        "action": "「会社名」で納品管理PL(リクロジ_納品管理)の取引を開き、"
                  "「入れる場所」のとおりに「入れる鍵」を入れる。"
                  "会社名が空の行は「HubSpot求人リンク」を開けば求人が分かる。"
                  "「顧客管理シートに未登録」の行は取引ではなく先にシートを直す"
                  "（宛先はシート管理者）。"
                  "「終了した取引にだけ紐付いている」の行は、鍵を入れる先を"
                  "**生きている取引**に替える作業。"
                  "取引は必ず存在する — 見つからない場合は突合ロジックの不具合として調査する",
        "impact": f"この顧客への応募は 一次対応の要否・担当者・応募先取引名 が"
                  f"空のまま入り続ける (直近{UNLINKED_DAYS}日で {n_app:,}件)",
        "detail": detail,
    }


def _artifact_hint() -> str:
    """CI実行時に「対象一覧をどこで取るか」を読者が開ける形で返す。

    ★ローカルの絶対パスをSlackに出しても、CIでは runner の使い捨てディスク
      (/home/runner/work/...) を指すだけで**誰も開けない**。実行ページのURLを出す。
    ★ただし upload-artifact ステップが**実在するときだけ**。GITHUB_* が在るか
      だけで判定すると、health_check.py だけが先に本番へ入ったとき成果物の無い
      ページへ読者を飛ばす。ワークフローが HEALTH_ARTIFACT_NAME を渡した時に限る。
    """
    srv = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    art = os.environ.get("HEALTH_ARTIFACT_NAME", "")
    if srv and repo and run and art:
        return (f"{srv}/{repo}/actions/runs/{run} の成果物「{art}」"
                "（GitHubのアカウントが要ります）")
    return ""


def publish_to_sheet(rows: list, title: str) -> str:
    """要対応リストを共有スプレッドシートへ全置換で書く (任意)。

    ★GitHubのアーティファクトはリポジトリのコラボレーターしか開けない。
      実際に取引へ値を入れる現場(BPO/営業)はGitHubアカウントを持たない前提
      なので、それを唯一の導線にすると上位数件しか届かない。
      HEALTH_LIST_SHEET_ID が設定されていればそこへ書き、URLを返す。
      未設定なら何もしない (失敗しても監視は落とさない)。
    """
    sid = os.environ.get("HEALTH_LIST_SHEET_ID", "")
    if not sid or not rows:
        return ""
    try:
        from scripts.job_application_sync.fetchers import account_loader as al
        gc = al.get_sheets_client()
        sh = al.sheet_retry(gc.open_by_key, sid)
        tab = title[:80] or "要対応"
        try:
            ws = al.sheet_retry(sh.worksheet, tab)
            al.sheet_retry(ws.clear)
        except al.gspread.WorksheetNotFound:
            # ★一時障害(5xx)で「タブが無い」と誤認して重複タブを作らないよう、
            #   捕まえるのは本当にタブが無い場合だけに絞る (2026-08-17)
            ws = sh.add_worksheet(title=tab, rows=max(len(rows) + 10, 100),
                                  cols=max(len(rows[0]) + 2, 20))
        cols = list(rows[0].keys())
        al.sheet_retry(ws.update,
                       [cols] + [[str(r.get(c, "")) for c in cols] for r in rows],
                       value_input_option="RAW")
        return f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    except Exception as e:  # noqa: BLE001
        print(f"      (共有シートへの書き出しに失敗: {type(e).__name__}: {e})",
              flush=True)
        return ""

CHECKS = [check_stage_consistency, check_recent_listings_linked,
          check_ichijitaiou_sync, check_search_cap,
          check_recent_applications_linked,
          check_unlinked_listings_by_customer]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack", action="store_true", help="異常をSlackへ通知")
    ap.add_argument("--out-dir", default="data/job_application_sync")
    ap.add_argument("--only", default="",
                    help="関数名の部分一致で1項目だけ実行 (検証用。CIでは使わない)")
    a = ap.parse_args(argv)

    checks = [f for f in CHECKS if a.only in f.__name__] if a.only else CHECKS
    if not checks:
        raise SystemExit(f"--only '{a.only}' に一致するチェックがありません: "
                         + ", ".join(f.__name__ for f in CHECKS))

    results, failed = [], []
    for fn in checks:
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
    snap_path = out / "health_check_latest.json"
    prev = _load_prev(snap_path)   # ★上書きする前に読む

    print(f"\n=== {len(results) - len(bad)}/{len(results)} 項目が正常 ===")
    # ★明細をCSVに落とす。件数だけ通知しても誰も動けない (2026-08-12)。
    #   通知は「誰が・何を・どこに入れるか」が分かる形にする。
    #   日付は JST。runner は UTC なので、そのままだと現場が見る日付の
    #   **前日**のファイル名になる (cron 40 16 * * * = JST 翌01:40)。
    csv_paths, sheet_urls = {}, {}
    today = _jst_today().strftime("%Y-%m-%d")
    for r in results:
        items = r.get("items") or []
        if not items:
            continue
        fn = out / f"要対応_{r['name'][:40]}_{today}.csv"
        # ★列は全行のキーの和集合。先頭行のキーで固定すると、行ごとに列構成が
        #   変わった瞬間 writerows が ValueError を投げ、except OSError では
        #   捕まらずに監視全体がトレースバックで落ちる。
        cols: list = []
        for it in items:
            for k in it:
                if k not in cols:
                    cols.append(k)
        try:
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore",
                                   restval="")
                w.writeheader()
                w.writerows(items)
            csv_paths[r["name"]] = str(fn.resolve())
            print(f"      要対応リスト: {fn.resolve()}")
        except Exception as e:  # noqa: BLE001  監視自体は落とさない
            print(f"      [警告] 要対応リストの書き出し失敗: {type(e).__name__}: {e}")
        u = publish_to_sheet(items, f"要対応_{r['name'][:60]}")
        if u:
            sheet_urls[r["name"]] = u
            print(f"      共有シート: {u}")

    # ★前回との差分。これが無いと同じ顔ぶれが毎日通知され、現場が直した分が
    #   効いているのかも分からない (「3項目が恒久NGだと誰も見なくなる」の再来)。
    diffs = {r["name"]: _diff_items(prev.get(r["name"]), r) for r in results}
    snap = {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checked_at_jst": _jst_today().strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
            # 次回の差分用。items 全体を持つと肥大するのでキーだけ残す。
            # ★判定できなかった項目(value<0)は前回値を据え置く。0件で上書きすると
            #   翌日「新規142件」という嘘の差分が出る。
            "keys": {r["name"]: (_item_keys(r) if r["value"] >= 0
                                 else prev.get(r["name"], []))
                     for r in results}}
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2,
                                    default=str), encoding="utf-8")

    slack_ok = None
    if a.slack and bad:
        msg = ["⚠️ 求人・応募データの健全性チェックで異常を検知しました"]
        for r in bad:
            msg.append(f"\n*{r['name']}: {r['value']:,}件* (あるべき値 {r['want']})")
            d = diffs.get(r["name"]) or {}
            if d.get("比較可能"):
                msg.append(f"　前回比: 新規 {d['新規']:,}件 / 継続 {d['継続']:,}件 "
                           f"/ 解消 {d['解消']:,}件 (前回 {d['前回']:,}件)")
            for line in r["detail"][:4]:
                msg.append(f"　{line}")
            if r.get("action"):
                msg.append(f"　▶ やること: {r['action']}")
            if r.get("impact"):
                msg.append(f"　▶ 放置すると: {r['impact']}")
            items = r.get("items") or []
            # ★一覧が実在するときだけ案内する。明細を持たない項目
            #   (件数だけの停止検知や、母数0で判定できなかった日) にまで
            #   「対象一覧はここ」と出すと、開いても何も無いページへ飛ばす。
            if items and csv_paths.get(r["name"]):
                where = (sheet_urls.get(r["name"]) or _artifact_hint()
                         or csv_paths[r["name"]])
                msg.append(f"　▶ 対象一覧: {where}")
            if items:
                msg.append("　▶ 上位5件 (応募が多い順 = 放置の実害が大きい順):")
            # ★出す列を明示指定する。「先頭5列」だと列を足した瞬間に
            #   「対応区分」や「入れる鍵」が黙って通知から消える。
            cols = list(r.get("slack_cols") or [])
            if items and not cols:
                cols = [k for k in items[0].keys()][:5]
            for it in items[:5]:
                parts = [f"{k}={it[k]}" for k in cols if str(it.get(k, "")) != ""]
                if not str(it.get("会社名", "")):
                    # 会社名が空の行は、代わりに必ず開けるリンクを出す
                    alt = it.get("求人URL(例)") or it.get("HubSpot求人リンク") or ""
                    if alt:
                        parts.append(f"求人={alt}")
                # ★項目の区切りは " | "。値の中で " / " を使っている列があるため
                #   (入れる鍵が複数店舗IDのとき「1439800 / 1439813」)、
                #   同じ記号だと列の境目が読めなくなる。
                msg.append("　　- " + " | ".join(parts))
            if len(items) > 5:
                msg.append(f"　　…ほか {len(items) - 5:,}件 (対象一覧を参照)")
        msg.append("\n※ このチェックは「処理が動いたか」ではなく"
                   "「結果があるべき状態か」を見ています")
        slack_ok = slack_notify("\n".join(msg))
        if slack_ok:
            print("[slack] 送信しました", flush=True)
        else:
            # ★送信失敗を無音にしない。「日次でSlackに飛ぶ」が成果物なので、
            #   飛ばなくなったことを検知できないと成果物が消えたのと同じ。
            print("::error title=Slack通知の送信に失敗::"
                  "要対応リストが誰にも届いていません", flush=True)

    # チェック自体が失敗したらCIを赤くする (監視の無音故障を作らない)。
    # 値が負(=データが取れていない/判定できていない)も同じ扱いにする。
    rc = 1 if (failed or any(r["value"] < 0 for r in results)) else 0
    if slack_ok is False:
        rc = 1
    return rc


def _item_keys(r: dict) -> list:
    """差分比較のための安定キー。items が無い項目は空。"""
    col = r.get("item_key")
    if not col:
        return []
    return sorted({str(it.get(col, "")) for it in (r.get("items") or [])
                   if str(it.get(col, ""))})


def _load_prev(path: Path) -> dict:
    """前回のスナップショットから {項目名: [キー]} を復元する (best-effort)。"""
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return j.get("keys") or {}


def _diff_items(prev_keys, r: dict) -> dict:
    now = set(_item_keys(r))
    if prev_keys is None or not r.get("item_key"):
        return {"比較可能": False}
    old = set(prev_keys)
    return {"比較可能": True, "新規": len(now - old), "継続": len(now & old),
            "解消": len(old - now), "前回": len(old)}


if __name__ == "__main__":
    sys.exit(main())
