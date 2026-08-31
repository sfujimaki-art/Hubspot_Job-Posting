# -*- coding: utf-8 -*-
"""旧取引の暗黙知メモを、同じ契約の新しい取引へ引き継ぐ (2026-08-21)。

## なぜ要るか

暗黙知メモの正を取引に置いた。理由は求人がクローズ→出し直しで作り替えられ、
旧新を結ぶIDが無いため、求人に置くと出し直すたびに失われるから。

ところが**取引も契約更新のたびに作り替えられ、IDが変わる**。実測(2026-08-18)で
納品管理3,478件のうち跡地(継続済/満了済)は1,449件あった。つまり求人で起きて
いた喪失を、規模を小さくして取引に移しただけの状態だった。

    RL00000030  株式会社みどり産業
       ├─ 取引 60177607584（継続済＝跡地）    ← メモはここにある
       └─ 取引 62939569706（継続_定期実施前） ← 生きている。メモが無い ★

実測(2026-08-21): **125グループ**で、同じ契約内にメモがあるのに生きている取引
へ届いていなかった。集約(rollup_memo_to_deal)は求人の関連先(to[0])へ貼るだけで、
契約グループを見ていないため、旧取引に溜まったメモは旧取引に取り残される。

## 何を鍵にするか

**取引先コード(code_of_customer)**。計上PLが契約単位で発番しており、法人が
同じでも契約が違えば別コードになる。契約更新で取引が作り替えられてもコードは
変わらないので、これが唯一の安定キー。既存の他のキーはどれも足りなかった:

    取引↔取引の関連 16% / 管理用メール 40% / 会社レコード 36% / 媒体アカウント 11%

## 対象

実測(2026-08-21、契約グループ1,301):

    対象外(メモ無し or 生存無し)      959
    既に生存側にある                  182
    継承する                          160   ← 生存が複数のグループも含む

**生存が複数のグループ(本体＋オプション等)も貼る**。求人はどの取引にもぶら下がり
うるので、片方だけだとその求人経由の応募にメモが届かない。同じ本文を生きている
取引すべてへ貼る (ユーザー指示 2026-08-21「継承して欲しい」)。

## やること

グループ内のメモを **rollup_merge.merge_group でまとめて**、生きている取引
すべてへ同じ本文を貼る。単純コピーにしないのは、生存側にも独自のメモが
ありうるため。

★**値が食い違っても省略せず両方を残す** (ユーザー指示 2026-08-21)。
  同じ契約に生きている取引が複数あるとき、どちらが新しいかは決まらない。
  **片方だけ更新されて、もう片方はいじられていない**ことが普通に起きるので、
  片方を履歴へ落とすと現場が見る本文からその条件が消える。両方を残し、
  どの取引に由来するかを添えて、人が見て判断できる形にする。

跡地側のメモは**消さない**。跡地は誰も見ないので害が小さく、消す方は
引き継ぎに失敗していたときに情報が消える。

使い方:
  python scripts/job_application_sync/inherit_rollup_by_contract.py            # 確認のみ
  python scripts/job_application_sync/inherit_rollup_by_contract.py --actual   # 引き継ぐ
  python scripts/job_application_sync/inherit_rollup_by_contract.py --rollback <json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")

from scripts.job_application_sync import deal_stages as DS       # noqa: E402
from scripts.job_application_sync.hs_paging import post_retry    # noqa: E402
from scripts.job_application_sync.rollup_merge import (          # noqa: E402
    merge_group)
from scripts.job_application_sync.rollup_memo_to_deal import (   # noqa: E402
    existing_rollup_state, _patch_note)

BASE = "https://api.hubapi.com"
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
# 取引とNoteの関連付け種別 (Deal ↔ Note)
ASSOC_NOTE_TO_DEAL = 214


def _headers() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def load_nouhin_deals() -> dict:
    """納品管理の取引を全件。取得漏れは total と突合して検知する."""
    fg = [{"filters": [{"propertyName": "pipeline", "operator": "EQ",
                        "value": DS.PIPELINE_NOUHIN}]}]
    total = post_retry(f"{BASE}/crm/v3/objects/0-3/search",
                       {"filterGroups": fg, "limit": 1}).get("total", 0)
    out: dict = {}
    after = None
    while True:
        body = {"filterGroups": fg, "limit": 100,
                "properties": ["dealstage", "dealname", DS.PROP_CODE],
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
            f"取引の取得漏れ: {len(out)}/{total}件。"
            "不完全なデータで引き継ぐと、あるはずのメモを見落とすため中止する")
    return out


def plan_inherit(deals: dict, state: dict, today: str = "") -> dict:
    """引き継ぎ計画を作る純関数 (テスト対象)。API は呼ばない。

    deals: {deal_id: {dealstage, dealname, code_of_customer}}
    state: {deal_id: {"note_id", "hash", "body"}}   既存の集約メモ

    戻り: {"write": [...], "deferred": [...], "nochange": int, "groups": int}
    """
    today = today or f"{datetime.now(timezone.utc):%Y-%m-%d}"
    groups = defaultdict(list)
    for did, p in deals.items():
        code = (p.get(DS.PROP_CODE) or "").strip()
        if code:
            groups[code].append(did)

    write, deferred = [], []
    nochange = 0
    for code, ids in sorted(groups.items()):
        srcs = [i for i in ids if (state.get(i) or {}).get("body")]
        tgts = [i for i in ids if DS.is_writable(deals[i].get("dealstage"))]
        if not srcs or not tgts:
            continue
        donors = [i for i in srcs if i not in tgts]
        lacking = [i for i in tgts if i not in srcs]
        # ★跡地が無くても、メモを持たない生きている取引があれば配る (2026-08-31 是正)。
        #   旧: `if not donors: continue` — 引き継ぐ元を跡地に限っていたため、
        #   本体＋オプションのように生存が複数でメモが片方にしか無いと、
        #   もう片方へ永久に届かなかった。実測37契約が全てこれに該当した
        #   (跡地メモ0件・生存の一部だけメモあり)。契約更新で旧取引がまだ
        #   閉じていない間(ヨミ段階)も同じ形になる。
        #   全ての生存が既に持っていて跡地も無いときだけ、何もしない。
        #   (生存同士で本文が食い違う場合はここでは触らない。別論点)
        if not donors and not lacking:
            continue
        # ★契約グループ内の全メモを1本にまとめ、生きている取引すべてへ貼る。
        #   生存が複数(本体＋オプション等)でも貼り先を選ばない。求人はどの取引に
        #   もぶら下がりうるので、片方だけだとその求人経由の応募にメモが届かない。
        #   値が食い違っても**省略せず両方を残す** (2026-08-21 ユーザー指示。
        #   片方だけ更新されもう片方は未更新、という状態が普通に起きるため)。
        merged = merge_group(
            [{"name": deals[i].get("dealname", "") or i,
              "body": state[i]["body"]} for i in srcs], today)
        if not merged.strip():
            continue
        for tgt in tgts:
            cur = (state.get(tgt) or {}).get("body") or ""
            if merged.strip() == cur.strip():
                nochange += 1
                continue
            write.append({
                "取引先コード": code,
                "deal_id": tgt,
                "取引名": deals[tgt].get("dealname", ""),
                "ステージ": DS.label(deals[tgt].get("dealstage")),
                # 跡地が無い経路(生存→生存)では donors が空になる。ログで
                # 「どこから来たか」が消えないよう、その場合はメモ元の生存を残す。
                "引継元": donors or [i for i in srcs if i != tgt],
                "同時に貼る生存取引": tgts,
                "note_id": (state.get(tgt) or {}).get("note_id", ""),
                "before_len": len(cur),
                # ★実行前の本文をそのまま残す。長さだけではロールバック
                #   できず、戻せない変更を本番へ入れることになる。
                "before_body": cur,
                "body": merged,
            })
    return {"write": write, "deferred": deferred, "nochange": nochange,
            "groups": len(groups)}


def create_note(deal_id: str, body: str) -> str:
    """取引へ集約Noteを新規作成して関連付ける。

    ★関連付けに失敗したら作ったNoteを消す。孤児Noteを残すと、次回の
      existing_rollup_state が拾えず同じ取引へ何度も作り続ける。
    """
    r = requests.post(f"{BASE}/crm/v3/objects/notes", headers=_headers(),
                      json={"properties": {
                          "hs_note_body": body.replace("\n", "<br>"),
                          "hs_timestamp": datetime.now(timezone.utc).strftime(
                              "%Y-%m-%dT%H:%M:%SZ")}}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Note作成に失敗 HTTP {r.status_code}: {r.text[:150]}")
    nid = r.json()["id"]
    a = requests.put(
        f"{BASE}/crm/v4/objects/notes/{nid}/associations/default/0-3/{deal_id}",
        headers=_headers(), timeout=30)
    if a.status_code not in (200, 201, 204):
        requests.delete(f"{BASE}/crm/v3/objects/notes/{nid}",
                        headers=_headers(), timeout=30)
        raise RuntimeError(f"関連付けに失敗 HTTP {a.status_code}: {a.text[:150]}")
    return nid


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


def deferred_message(deferred: list) -> str:
    """人へ回す通知。何を・どこで・放置するとどうなるかまで書く."""
    head = (f"契約が更新されたのに暗黙知メモを引き継げない取引が "
            f"{len(deferred)}件 あります\n"
            "　▶ やること: 前の取引の暗黙知メモを、今の取引へコピーしてください\n"
            "　▶ なぜ機械ができないか: 同じ契約に生きている取引が複数あり"
            "(本体＋オプション等)、どれに貼るべきか決められません\n"
            "　▶ 放置すると: 一次対応の担当者が応募者画面で足切り基準・"
            "ヒアリング項目を見られません\n"
            "　▶ 対象:\n")
    lines = []
    for d in deferred[:12]:
        src = d["メモがある取引"][0]
        lines.append(f"　　・{d['取引先コード']} {src['取引名'][:26]}\n"
                     f"　　　 メモ元 取引 {src['id']} → 候補 "
                     + " / ".join(f"{t['id']}({t['ステージ'][:10]})"
                                  for t in d["生きている取引"][:3]))
    if len(deferred) > 12:
        lines.append(f"　　…ほか {len(deferred) - 12}件")
    return head + "\n".join(lines)


def main(dry_run: bool, limit, do_slack: bool) -> int:
    print(f"=== inherit_rollup_by_contract (dry_run={dry_run}) ===", flush=True)
    deals = load_nouhin_deals()
    print(f"納品管理PL: {len(deals):,}件", flush=True)
    state = existing_rollup_state()
    print(f"集約メモを持つ取引: {len(state):,}件", flush=True)
    plan = plan_inherit(deals, state)
    print(f"\n契約グループ {plan['groups']:,} / 引き継ぐ {len(plan['write']):,}件"
          f" / 変化なし {plan['nochange']:,} / 人へ回す {len(plan['deferred']):,}件",
          flush=True)
    for w in plan["write"][:6]:
        print(f"    {w['取引先コード']} {w['取引名'][:26]:<28} "
              f"取引{w['deal_id']} {w['before_len']:,}字 → {len(w['body']):,}字")

    items = plan["write"][:limit] if limit else plan["write"]
    ok = ng = 0
    done = []
    if not dry_run:
        for w in items:
            try:
                if w["note_id"]:
                    _patch_note(w["note_id"], w["body"])
                    done.append({"deal_id": w["deal_id"],
                                 "note_id": w["note_id"], "op": "update"})
                else:
                    nid = create_note(w["deal_id"], w["body"])
                    done.append({"deal_id": w["deal_id"], "note_id": nid,
                                 "op": "create"})
                ok += 1
            except Exception as e:  # noqa: BLE001
                ng += 1
                print(f"  [NG] 取引 {w['deal_id']}: {e}", flush=True)
            time.sleep(0.08)
        print(f"  引き継ぎOK {ok:,} / NG {ng:,}", flush=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log = LOG_DIR / (f"inherit_rollup_{'actual' if not dry_run else 'dry'}"
                     f"_{ts}.json")
    log.write_text(json.dumps({"plan": plan, "applied": done},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  log: {log}", flush=True)
    if plan["deferred"] and do_slack:
        slack(deferred_message(plan["deferred"]))
    return 1 if ng else 0


def rollback(path: str) -> int:
    """--actual の結果を戻す。create は削除、update は元本文へ戻す."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    before = {w["deal_id"]: w for w in data["plan"]["write"]}
    n = 0
    for a in data.get("applied", []):
        if a["op"] == "create":
            r = requests.delete(f"{BASE}/crm/v3/objects/notes/{a['note_id']}",
                                headers=_headers(), timeout=30)
            n += r.status_code in (200, 204)
        else:
            w = before.get(a["deal_id"])
            if not w:
                continue
            _patch_note(a["note_id"], w["before_body"])
            n += 1
        time.sleep(0.06)
    print(f"ロールバック {n}件 (作成は削除 / 更新は元本文へ)", flush=True)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--actual", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--slack", action="store_true")
    p.add_argument("--rollback")
    return p.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    if a.rollback:
        sys.exit(rollback(a.rollback))
    try:
        sys.exit(main(dry_run=not a.actual, limit=a.limit, do_slack=a.slack))
    except Exception as e:  # noqa: BLE001
        if a.slack:
            slack("暗黙知メモの引き継ぎが失敗しました\n"
                  f"　理由: {type(e).__name__}: {str(e)[:200]}\n"
                  "　▶ 放置すると: 契約更新後の取引にメモが無いまま、"
                  "一次対応の担当者が足切り基準を見られません\n"
                  "　▶ 確認: Job Daily の deal_hygiene のログ "
                  "(inherit_rollup_by_contract)")
        raise
