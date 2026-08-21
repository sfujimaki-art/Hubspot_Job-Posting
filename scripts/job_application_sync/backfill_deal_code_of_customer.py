# -*- coding: utf-8 -*-
"""納品管理の取引に「取引先コード」を補完する (2026-08-18)。

## なぜ要るか

求人メモの正を取引に置いた。しかし**取引も契約更新のたびに作り替えられ、IDが
変わる**。実測(2026-08-18): 納品管理PL 3,478件のうち跡地(継続済/満了済)1,449件、
生きている637件。求人で起きていた「作り替えでメモが失われる」問題を、規模を
小さくして取引に移しただけの状態だった。

既存キーでは新旧を繋げない (跡地1,449件から生きた取引を一意に特定できた割合):

    取引↔取引の直接関連    235件 (16%)   現場が関連付けていない
    管理用メール           574件 (40%)   跡地347件がキー未保有
    会社レコード           515件 (36%)   事業所単位なので1社に複数の生きた取引
    媒体アカウント         166件 (11%)   求人942件が鍵未保有

**取引先コード(code_of_customer)だけが契約単位の安定キー**。計上PLの取引が
100%保有しており(3,617/3,630)、法人が同じでも契約が違えば別コードになる。
納品管理から計上への関連を辿ればコードが取れる。

## 取得できるか (2026-08-18 実測)

    納品管理 3,478件 → コード解決 2,572件 (74%)
      未解決906件の内訳: 解約済567 / 契約終了326 / **生きている13件のみ**
    2026-06以降に作られた取引 → 96%

## なぜ「新規作成の検知」ではなく夜間の総ざらいなのか

**取引が作られた時点では計上がまだ存在しない**(ユーザー確認: HubSpotの
ワークフローが走ってから作られる)。実測でも、直近3ヶ月の267件のうち作成当日に
計上があるのは41%だけ。

    1日以内 75% / 3日以内 87% / 7日以内 94% / 30日以内 99% / 最終的に100%

つまり作成イベントで1回だけ動かすと6割弱を取りこぼす。**未解決のものを毎晩
ぜんぶ再挑戦する**作りにすれば、経理作業でコードが発行され次第、翌朝には埋まる。
ステージ遷移の検知も不要になる。

## 既存値が計上と食い違うとき (実測5件) は計上で上書きする

当初は「人が入れた値を機械が消してはいけない」と考えて上書きしない設計にした。
**その前提が誤りだった。** 変更履歴(propertiesWithHistory)を見たところ、5件とも
`AUTOMATION_PLATFORM` が入れた値で、人の手は入っていなかった。

犯人は**無効化済み**のワークフロー 1611321133
「請求先&取引先コード転写　会社⇒取引」:

    property_name: code_of_customer
    association:   associationTypeId 342   ← 会社に紐づく取引"全部"
    value:         {{ enrolled_object.torihiki }}

対になる 1607454797「取引⇒会社」が取引のコードを会社の `torihiki` に写し、
これがそれを同じ会社の取引すべてへ配る。**会社を経由するので事業所も契約も
区別されない。** 実測した誤りは全てこれで説明がつく:

    あさひ産業 群馬営業所   → 本社の RL00000033 (同じ会社に本社契約が多数)
    デルタテック川越      → 茨城の RL00001207 (同一会社の2事業所が混在)
    ルミナ・メンテナンス   → 別法人(全国梱包運輸倉庫)の RL00001235
                            (この取引は MERGE_OBJECTS の履歴も持つ)

正常な経路は 316165404 の レコード作成(actionTypeId 0-14) で、計上から取引を
新規作成する際に値をコピーする。こちらは契約単位なので正しい。
**転写ペア3本は既に無効なので、新しい誤りはもう発生しない。**

## やらないこと

- 計上が複数ぶら下がりコードが割れている取引(実測21件)には書かない。
  機械では決められない。当面は無視する(ユーザー判断 2026-08-18)。
- 跡地(解約済/継続済)にも書く。**新旧を繋ぐのが目的なので跡地にこそ要る。**
- 上書きした前値は log の `applied[].before` に残す。`--rollback` で戻せる。

使い方:
  python scripts/job_application_sync/backfill_deal_code_of_customer.py            # 確認のみ
  python scripts/job_application_sync/backfill_deal_code_of_customer.py --actual   # 書き込む
  python scripts/job_application_sync/backfill_deal_code_of_customer.py --rollback <json>
"""
from __future__ import annotations

import argparse
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

from scripts.job_application_sync import deal_stages as DS      # noqa: E402
from scripts.job_application_sync.hs_paging import post_retry   # noqa: E402

BASE = "https://api.hubapi.com"
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 生きている取引がこの日数を過ぎてもコードを取れないなら人へ回す。
# 実測(直近3ヶ月)で30日以内に99%が解決するため、超えるものは異常。
STALE_DAYS = int(os.environ.get("JAS_CODE_STALE_DAYS", "30"))

# --actual を人の承認なしに走らせないための最小待ち時間。
# 別実装 backfill_deal_code_apply.py の承認ゲートをここへ移植した
# (2026-08-19。同じ目的のスクリプトを2本持たない)。
GATE_HOURS = int(os.environ.get("JAS_CODE_GATE_HOURS", "7"))
# 無人(日次)で書いてよい上限。これを超えるか、既存値の上書きを含むときは
# 人の承認を要求する。日次の差分は実測2件なので通常は掛からない。
UNATTENDED_MAX = int(os.environ.get("JAS_CODE_UNATTENDED_MAX", "50"))


def check_time_gate(proceed_after, now=None, min_hours: int = GATE_HOURS):
    """--proceed-after は「今から min_hours 先」以降でなければ受け付けない。

    ★なぜ「先の時刻」を要求するのか
      過去や直近を許すと、その場で思いついた値を入れて即実行できてしまい、
      ゲートが名前だけになる。深夜枠で人が承認した時刻を先に宣言させ、
      それより前の起動を拒否することで「宣言 → 待機 → 実行」を強制する。

    戻り: (通してよいか, 理由)
    """
    now = now or datetime.now(timezone.utc)
    if proceed_after.tzinfo is None:
        return False, "--proceed-after にはタイムゾーンが要る (例 +09:00)"
    earliest = now + timedelta(hours=min_hours)
    if proceed_after < earliest:
        short = (earliest - proceed_after).total_seconds() / 3600
        return False, (f"タイムゲート未到達: now={now.isoformat()} / "
                       f"proceed_after={proceed_after.isoformat()} / "
                       f"必須 now+{min_hours}h={earliest.isoformat()} / "
                       f"不足 {short:.1f}h")
    return True, ""


def _headers() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def search_pipeline(pipeline: str, props: list) -> dict:
    """1パイプラインの取引を全件取る。

    ★取得漏れを自分で検知する。search の paging は一時障害で途中終了しうる
      (2026-08-17 に実測: sorts 無しで 3,478件中 600件しか取れなかった)。
      total と突合し、合わなければ落とす。黙って少ない件数で走らせない。
    """
    fg = [{"filters": [{"propertyName": "pipeline",
                        "operator": "EQ", "value": pipeline}]}]
    total = post_retry(f"{BASE}/crm/v3/objects/0-3/search",
                       {"filterGroups": fg, "limit": 1}).get("total", 0)
    out: dict = {}
    after = None
    while True:
        body = {"filterGroups": fg, "properties": props, "limit": 100,
                "sorts": [{"propertyName": "hs_object_id",
                           "direction": "ASCENDING"}]}
        if after:
            body["after"] = after
        j = post_retry(f"{BASE}/crm/v3/objects/0-3/search", body)
        for o in j.get("results", []):
            out[o["id"]] = o.get("properties") or {}
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.05)
    if len(out) != total:
        raise RuntimeError(
            f"取引の取得漏れ pipeline={pipeline}: {len(out)}/{total}件。"
            "不完全なデータで補完すると誤った値を書くため中止する")
    return out


def read_deal_links(deal_ids: list) -> dict:
    """取引 → 関連する取引ID一覧。★100件ずつ (全件一度だと空が返る)."""
    m: dict = {}
    for i in range(0, len(deal_ids), 100):
        r = post_retry(f"{BASE}/crm/v4/associations/0-3/0-3/batch/read",
                       {"inputs": [{"id": x} for x in deal_ids[i:i + 100]]})
        for row in r.get("results", []):
            m[str(row["from"]["id"])] = [str(t["toObjectId"])
                                         for t in row.get("to", [])]
        time.sleep(0.03)
    return m


def codes_from_keijo(deal_id: str, links: dict, keijo: dict) -> set:
    """その取引にぶら下がる計上から取引先コードを集める (空は除く)."""
    out = set()
    for other in links.get(deal_id, []):
        p = keijo.get(other)
        if not p:
            continue
        c = (p.get(DS.PROP_CODE) or "").strip()
        if c:
            out.add(c)
    return out


def _dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def plan_updates(nouhin: dict, links: dict, keijo: dict, now=None) -> dict:
    """書き込み計画を作る純関数 (テスト対象)。API は呼ばない。

    戻り: {"write": [...], "conflict": [...], "mismatch": [...],
           "stale": [...], "waiting": int}
    """
    now = now or datetime.now(timezone.utc)
    write, conflict, mismatch, stale = [], [], [], []
    waiting = 0
    for did, p in nouhin.items():
        own = (p.get(DS.PROP_CODE) or "").strip()
        cands = codes_from_keijo(did, links, keijo)
        name = p.get("dealname") or ""
        if len(cands) > 1:
            # 計上が複数でコードが割れている。機械では決められない。
            conflict.append({"deal_id": did, "取引名": name,
                             "候補": sorted(cands), "現在値": own})
            continue
        if own:
            # ★既存値と計上が食い違うときは計上で上書きする (2026-08-18 承認)。
            #   当初は「人の入力を守る」ため上書きしない設計にしたが、
            #   変更履歴を調べたら**全件 AUTOMATION_PLATFORM が入れた誤り**だった。
            #   犯人は無効化済みのワークフロー 1611321133
            #   「請求先&取引先コード転写 会社⇒取引」で、会社に紐づく取引"全部"へ
            #   同じコードを配る作りだったため、事業所も契約も区別されなかった。
            #     あさひ産業 群馬営業所 → 本社の RL00000033
            #     デルタテック川越   → 茨城の RL00001207
            #     ルミナ・メンテナンス → 別法人(全国梱包運輸倉庫)の RL00001235
            #   計上側は契約単位で発番されており、そちらが正しい。
            if cands and own not in cands:
                mismatch.append({"deal_id": did, "取引名": name,
                                 "現在値": own, "計上": sorted(cands)[0]})
                write.append({"deal_id": did, "取引名": name,
                              "code": sorted(cands)[0],
                              "before": own,
                              "stage": DS.label(p.get("dealstage"))})
            continue
        if cands:
            write.append({"deal_id": did, "取引名": name,
                          "code": sorted(cands)[0],
                          "stage": DS.label(p.get("dealstage"))})
            continue
        # まだ計上が無い。翌晩に再挑戦する。
        waiting += 1
        created = _dt(p.get("createdate"))
        if (DS.is_writable(p.get("dealstage")) and created
                and (now - created) > timedelta(days=STALE_DAYS)):
            stale.append({"deal_id": did, "取引名": name,
                          "ステージ": DS.label(p.get("dealstage")),
                          "作成日": (p.get("createdate") or "")[:10],
                          "経過日数": (now - created).days})
    return {"write": write, "conflict": conflict, "mismatch": mismatch,
            "stale": stale, "waiting": waiting}


def slack(message: str) -> bool:
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message[:300]}", flush=True)
        return False
    try:
        return requests.post(url, json={"text": message},
                             timeout=15).status_code == 200
    except requests.RequestException as e:  # noqa: BLE001
        print(f"[slack送信失敗] {e}", flush=True)
        return False


def stale_message(stale: list) -> str:
    """人へ回す通知。件数だけでなく「何を・どこに・放置するとどうなるか」まで書く."""
    head = (f"取引先コードが {STALE_DAYS}日 埋まらない取引が {len(stale)}件 あります\n"
            "　▶ やること: 計上の取引を作成し、納品管理の取引と関連付けてください\n"
            "　▶ 放置すると: 契約更新で取引が作り替わったとき、前の取引に溜めた"
            "求人メモ(足切り基準・ヒアリング項目)を引き継げません\n"
            "　▶ 対象:\n")
    lines = [f"　　・{s['取引名'][:34]} (取引 {s['deal_id']} / "
             f"{s['ステージ']} / 作成 {s['作成日']} / {s['経過日数']}日経過)"
             for s in stale[:15]]
    if len(stale) > 15:
        lines.append(f"　　…ほか {len(stale) - 15}件")
    return head + "\n".join(lines)


def main(dry_run: bool, limit, do_slack: bool,
         proceed_after=None, operator: str = "") -> int:
    print(f"=== backfill_deal_code_of_customer (dry_run={dry_run}) ===",
          flush=True)
    nouhin = search_pipeline(DS.PIPELINE_NOUHIN,
                             ["dealstage", "dealname", DS.PROP_CODE,
                              "createdate"])
    print(f"納品管理PL: {len(nouhin):,}件", flush=True)
    keijo: dict = {}
    for pid in sorted(DS.PIPELINES_KEIJO):
        d = search_pipeline(pid, [DS.PROP_CODE, "dealname"])
        keijo.update(d)
        print(f"  計上 {pid}: {len(d):,}件", flush=True)
    links = read_deal_links(sorted(nouhin))
    plan = plan_updates(nouhin, links, keijo)

    print(f"\n書込対象 {len(plan['write']):,}件 / "
          f"計上待ち {plan['waiting']:,}件 / "
          f"コード割れ {len(plan['conflict']):,}件 / "
          f"既存値と食い違い {len(plan['mismatch']):,}件 / "
          f"{STALE_DAYS}日超の要対応 {len(plan['stale']):,}件", flush=True)
    for w in plan["write"][:6]:
        print(f"    {w['deal_id']} {w['取引名'][:30]:<32} -> {w['code']}")

    items = plan["write"][:limit] if limit else plan["write"]
    ok = ng = 0
    done = []
    if not dry_run:
        # ★プランが確定してから承認を判定する。件数と上書きの有無で決まるため。
        why = require_gate(plan, proceed_after, operator)
        if why:
            print("", flush=True)
            print(f"[gate] {why}", flush=True)
            return 2
        if proceed_after:
            print(f"[gate] 承認: {operator} / 実行可能時刻 {proceed_after}",
                  flush=True)
        for w in items:
            r = requests.patch(f"{BASE}/crm/v3/objects/0-3/{w['deal_id']}",
                               headers=_headers(),
                               json={"properties": {DS.PROP_CODE: w["code"]}},
                               timeout=30)
            if r.status_code in (200, 201):
                ok += 1
                done.append({"deal_id": w["deal_id"],
                             "before": w.get("before", ""),
                             "after": w["code"]})
            else:
                ng += 1
                print(f"  [NG] {w['deal_id']}: {r.text[:120]}", flush=True)
            time.sleep(0.06)
        print(f"  更新OK {ok:,} / NG {ng:,}", flush=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log = LOG_DIR / (f"backfill_code_{'actual' if not dry_run else 'dry'}"
                     f"_{ts}.json")
    log.write_text(json.dumps({"plan": plan, "applied": done},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  log: {log}", flush=True)

    if plan["stale"] and do_slack:
        slack(stale_message(plan["stale"]))
    return 1 if ng else 0


def rollback(path: str) -> int:
    """--actual で書いた分を元に戻す (before が空なら空文字で消す)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    n = 0
    applied = data.get("applied", [])
    for a in applied:
        r = requests.patch(f"{BASE}/crm/v3/objects/0-3/{a['deal_id']}",
                           headers=_headers(),
                           json={"properties": {DS.PROP_CODE: a["before"]}},
                           timeout=30)
        n += r.status_code in (200, 201)
        time.sleep(0.06)
    print(f"ロールバック {n}/{len(applied)}件", flush=True)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--actual", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--slack", action="store_true")
    p.add_argument("--rollback")
    # ★承認ゲート。--actual を単独で撃てないようにする (fail closed)。
    p.add_argument("--proceed-after",
                   help="この時刻以降に実行してよい、と人が宣言した時刻"
                        "(ISO8601・タイムゾーン必須。例 2026-08-20T02:00:00+09:00)")
    p.add_argument("--rollout-operator",
                   help="実行を承認した人。ログに残す")
    return p.parse_args(argv)


def require_gate(plan: dict, proceed_after=None, operator: str = "",
                 now=None) -> str:
    """この書き込みに人の承認が要るか。要るのに無ければ理由を返す。

    ★なぜ「常に承認必須」にしないのか
      このスクリプトは**毎晩無人で走る**(deal_hygiene, JST 01:40)。常に
      --proceed-after を要求すると、routine が毎晩 exit 2 で落ちて
      「補完が動いていない」状態が続く。承認ゲートは一度きりの大量投入を
      守るためのもので、日次の差分補完を止めるためのものではない。

    ★では何を危険とみなすか
      (a) 件数が多い    … 初回の一括投入(実測1,615件)のような規模
      (b) 既存値の上書き … 空欄補完と違い、元の値が消える

      日次の差分はどちらにも当たらない(実測: 2026-08-19 の差分は2件・
      上書き0件)。当たったときだけ人の承認を求める。
    """
    n = len(plan.get("write") or [])
    over = [w for w in (plan.get("write") or []) if w.get("before")]
    reasons = []
    if n > UNATTENDED_MAX:
        reasons.append(f"書込 {n:,}件 (無人で許すのは {UNATTENDED_MAX}件まで)")
    if over:
        reasons.append(f"既存値の上書き {len(over)}件")
    if not reasons:
        return ""
    why = " / ".join(reasons)
    if not proceed_after or not operator:
        return (f"{why} → 人の承認が要る。"
                "--proceed-after と --rollout-operator を付けて実行すること"
                " (誰がいつ承認したかを残さずに本番を書き換えない)")
    try:
        pa = datetime.fromisoformat(proceed_after)
    except ValueError:
        return f"--proceed-after を解釈できない: {proceed_after!r}"
    ok, msg = check_time_gate(pa, now=now)
    return "" if ok else msg


if __name__ == "__main__":
    a = parse_args()
    if a.rollback:
        sys.exit(rollback(a.rollback))
    try:
        sys.exit(main(dry_run=not a.actual, limit=a.limit, do_slack=a.slack,
                      proceed_after=a.proceed_after,
                      operator=a.rollout_operator or ""))
    except Exception as e:  # noqa: BLE001
        # ★止まったことを人へ届ける。::warning:: はSlackに飛ばない。
        if a.slack:
            slack("取引先コードの補完が失敗しました\n"
                  f"　理由: {type(e).__name__}: {str(e)[:200]}\n"
                  "　▶ 放置すると: 契約更新をまたいで求人メモを引き継げません\n"
                  "　▶ 確認: Job Daily の deal_hygiene のログ "
                  "(backfill_deal_code_of_customer)")
        raise
