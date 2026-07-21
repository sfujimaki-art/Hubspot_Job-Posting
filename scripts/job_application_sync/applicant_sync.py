"""応募連携 本線 runner: queue検知→集約→(セッション再利用)fetch→applicant_import→APPOINTMENT.

WBS 1.11.9 スコープ③。設計正本: docs/wbs_outputs/1.11.9_媒体CSV同期実装/
応募連携_トリガーとBAN対策_設計_2026-07-07.md

放置ゼロ原則:
  - 成功が検証できた応募だけ台帳を DONE にする
  - 失敗は最大3回リトライ(バックオフ)→駄目なら Slack 報告して手放す(黙って捨てない)
  - 未突合(アカウント特定不可)も Slack 報告

BAN対策:
  - アカウント集約(1社1ログイン/バッチ)
  - AWセッション再利用(storage_state)
  - 単一インスタンスロック(多重起動防止)

env:
  JAS_APPLICANT_QUEUE_SHEET_ID   : 集約シート(queue)
  JAS_SESSION_DIR                : storage_state 保存先 (既定 data/job_application_sync/aw_sessions)
  JAS_LEDGER_PATH                : 処理台帳 (既定 data/job_application_sync/applicant_ledger.json)
  SLACK_APPLICANT_ALERT_WEBHOOK  : 失敗通知先
  HUBSPOT_ACCESS_TOKEN           : HubSpot 書込
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_ENV = _REPO / ".env"
if _ENV.exists():
    load_dotenv(_ENV)

from scripts.job_application_sync import applicant_queue as aq  # noqa: E402
from scripts.job_application_sync import applicant_import as ai  # noqa: E402
from scripts.job_application_sync.fetchers import account_loader as al  # noqa: E402
from scripts.job_application_sync.fetchers import aw_applicant_fetcher as af  # noqa: E402

MAX_ATTEMPTS = 3
LOCK_STALE_SEC = 15 * 60
# BAN対策: 1run あたり新規ログインするAWアカウント数の上限。
# 5分バッチで少しずつ捌く(全106社なら ~9run/~45分に分散)。session再利用で2回目以降は再ログインなし。
MAX_AW_ACCOUNTS_PER_RUN = int(os.environ.get("JAS_MAX_AW_PER_RUN", "12"))
_DATA = _REPO / "data" / "job_application_sync"
SESSION_DIR = Path(os.environ.get("JAS_SESSION_DIR", _DATA / "aw_sessions"))
LEDGER_PATH = Path(os.environ.get("JAS_LEDGER_PATH", _DATA / "applicant_ledger.json"))
LOCK_PATH = _DATA / "applicant_sync.lock"


# ============================================================================
# 単一インスタンスロック (ステイル回収付き)
# ============================================================================
class Lock:
    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = path

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
                if age < LOCK_STALE_SEC:
                    return False       # 実行中 (ステイルでない)
            except OSError:
                pass
            # ステイル → 奪う
        self.path.write_text(str(int(time.time())), encoding="utf-8")
        return True

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# ============================================================================
# 処理台帳 (放置ゼロ: DONE は成功時のみ / FAILED は試行回数を記録)
# ============================================================================
class Ledger:
    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def status(self, row_id: str) -> str:
        return (self.data.get(row_id) or {}).get("status", "NEW")

    def attempts(self, row_id: str) -> int:
        return (self.data.get(row_id) or {}).get("attempts", 0)

    def mark(self, row_id: str, status: str, error: str = "") -> None:
        e = self.data.setdefault(row_id, {"attempts": 0})
        e["status"] = status
        e["updated"] = datetime.now().isoformat()
        if error:
            e["last_error"] = error[:300]

    def bump(self, row_id: str, error: str) -> int:
        e = self.data.setdefault(row_id, {"attempts": 0})
        e["attempts"] = e.get("attempts", 0) + 1
        e["status"] = "FAILED"
        e["last_error"] = error[:300]
        e["updated"] = datetime.now().isoformat()
        return e["attempts"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================================
# Slack 通知 (放置ゼロ: 失敗を必ず報告)
# ============================================================================
def slack_notify(message: str, dry_run: bool = False) -> bool:
    # dry-run 時は実送信せず表示のみ (テストのSlackノイズ防止)
    if dry_run:
        print(f"[slack(dry-run,未送信)] {message}", flush=True)
        return False
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        print(f"[slack未設定] {message}", flush=True)
        return False
    try:
        r = requests.post(url, json={"text": message}, timeout=15)
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"[slack送信失敗] {e}: {message}", flush=True)
        return False


# ============================================================================
# AW 1アカウント処理 (セッション再利用fetch → applicant_import)
# ============================================================================
def _sanitize(s: str) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z_.-]", "_", s)[:60]


def _session_path(login_id: str) -> Path:
    return SESSION_DIR / f"{_sanitize(login_id)}.json"


async def _fetch_aw_csv(login_id: str, password: str, out_dir: Path) -> Path:
    return await af.fetch_aw_applicants(
        login_id, password, out_dir, headless=True,
        storage_state_path=_session_path(login_id))


def _missing_aw_listings(job_ids: list[str], token: str) -> list[str]:
    """id_airwork の LISTING が存在しない求人IDを返す (100件チャンク search)."""
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    exist: set = set()
    ids = [j for j in job_ids if j]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        body = {"filterGroups": [{"filters": [
            {"propertyName": "id_airwork", "operator": "IN", "values": chunk}]}],
            "properties": ["id_airwork"], "limit": 200}
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/0-420/search",
            headers=H, json=body, timeout=30)
        for o in r.json().get("results", []):
            v = (o.get("properties") or {}).get("id_airwork")
            if v:
                exist.add(v)
        time.sleep(0.1)
    return [j for j in ids if j not in exist]


def _ensure_aw_jobs(bid: str, b_pw: str, missing: list[str], out_dir: Path) -> int:
    """案2b: 応募の求人LISTINGが欠落 → その社の求人をfetch(session再利用)して upsert.
    生成待ちあり(数分)。欠落時のみ発火。Returns: upsert試行した求人数(近似)."""
    from scripts.job_application_sync.fetchers import aw_csv_fetcher as awf
    from scripts.job_application_sync import airwork_import as air
    zip_path = asyncio.run(awf.fetch_aw_xlsx(
        bid, b_pw, out_dir, headless=True, mode="full",
        storage_state_path=_session_path(bid)))
    # login_id=bid で LISTING を作成 (応募linkと同じキーに揃える)。
    # client_code はXLSX側と異なるため strict_client_code=False。
    air.run_xlsx(str(zip_path), login_id=bid, dry_run=False,
                 strict_client_code=False)
    return len(missing)


def process_aw_account(
    company: str, b_ids: list[str], b_pw: str,
    out_dir: Path, dry_run: bool = True,
) -> dict:
    """AW 1アカウントの応募を取得→(案2b求人先行)→登録。複数B系IDは順に試行。
    Returns: {ok, linked, unlinked, dup, jobs_fetched, error, login_id}"""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    result = {"ok": False, "linked": 0, "unlinked": 0, "dup": 0,
              "jobs_fetched": 0, "error": "", "login_id": ""}
    last_err = ""
    for bid in b_ids or []:
        if not bid or bid in ("ー", "-"):
            continue
        try:
            csv_path = asyncio.run(_fetch_aw_csv(bid, b_pw, out_dir))
        except Exception as e:  # noqa: BLE001
            last_err = f"{bid}: {type(e).__name__}: {str(e)[:100]}"
            continue
        rows = ai.load_applicants_csv(str(csv_path))
        if dry_run:
            result.update(ok=True, login_id=bid, linked=0, unlinked=len(rows),
                          csv=csv_path.name, total=len(rows))
            return result
        # 案2b (inline求人fetch) は既定OFF。理由(2026-07-08 本番実測):
        #   fetch_aw_xlsx の求人生成待ち(最大20分/社)がバッチを破綻させる
        #   (12社×20分=数時間 → 5分バッチ/30分timeoutに収まらず)。
        # → 求人LISTINGの作成は独立の求人デイリー(hr_watcher/aw巡回)に委ね、
        #   応募syncは既存LISTINGへ紐付けるだけにする(高速)。求人未作成の応募は
        #   対象外で登録される(求人が先=ユーザー設計に沿う)。
        # JAS_ENABLE_INLINE_JOB_FETCH=1 で従来の同期fetchを有効化可(生成待ち許容時)。
        job_ids = list({r.media_job_id for r in rows if r.media_job_id})
        jobs_fetched = 0
        if os.environ.get("JAS_ENABLE_INLINE_JOB_FETCH", "0") == "1":
            try:
                missing = _missing_aw_listings(job_ids, token)
                if missing:
                    jobs_fetched = _ensure_aw_jobs(bid, b_pw, missing, out_dir)
                    time.sleep(15)  # index反映待ち
            except Exception as e:  # noqa: BLE001
                print(f"  [案2b] 求人fetch失敗(応募は続行): "
                      f"{type(e).__name__}: {str(e)[:80]}", flush=True)
        # 応募import (求人が揃った状態で紐付け)
        cli = ai.RealHubSpotClient(token)
        results = ai.run_import(rows, cli, default_login_id=bid)
        from collections import Counter
        st = Counter(r.status for r in results)
        result.update(ok=True, login_id=bid, jobs_fetched=jobs_fetched,
                      linked=st.get("linked", 0),
                      unlinked=st.get("unlinked", 0),
                      dup=st.get("skip_duplicate", 0), total=len(results))
        return result
    result["error"] = last_err or "有効なB系IDなし"
    return result


# ============================================================================
# HR 1マスター処理 (日付範囲で全顧客一括fetch → applicant_import)
# ============================================================================
def _q_date_to_iso(s: str) -> Optional[str]:
    """queue の日付 '2026/5/12' → '2026-05-12'."""
    import re
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", (s or "").strip())
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


async def _fetch_hr_csv(out_dir: Path, date_from: str, date_to: str) -> Path:
    from scripts.job_application_sync.fetchers import hr_applicant_fetcher as hf
    return await hf.fetch_hr_applicants(out_dir, date_from, date_to, headless=True)


def process_hr_batch(items, out_dir: Path, dry_run: bool = True,
                     date_from: str = "", date_to: str = "") -> dict:
    """HR応募を日付範囲で1マスター取得→登録。
    date_from/to 未指定なら queue項目の日付範囲(最大60日にcap)から算出。"""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    result = {"ok": False, "linked": 0, "unlinked": 0, "dup": 0, "error": "",
              "date_from": "", "date_to": ""}
    # 日付範囲決定
    if not (date_from and date_to):
        ds = sorted(d for d in (_q_date_to_iso(it.columns.get("D", ""))
                                for it in items) if d)
        if not ds:
            result["error"] = "HR項目に有効な日付なし"
            return result
        date_to = date_to or ds[-1]
        # 60日capで開始日を決定
        from datetime import date as _date, timedelta
        y, m, d = map(int, date_to.split("-"))
        cap = (_date(y, m, d) - timedelta(days=60)).isoformat()
        date_from = date_from or max(ds[0], cap)
    result["date_from"], result["date_to"] = date_from, date_to
    try:
        csv_path = asyncio.run(_fetch_hr_csv(out_dir, date_from, date_to))
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return result
    rows = ai.load_applicants_csv(str(csv_path))
    if dry_run:
        result.update(ok=True, unlinked=len(rows), total=len(rows),
                      csv=csv_path.name)
        return result
    cli = ai.RealHubSpotClient(token)
    results = ai.run_import(rows, cli)
    from collections import Counter
    st = Counter(r.status for r in results)
    result.update(ok=True, linked=st.get("linked", 0),
                  unlinked=st.get("unlinked", 0),
                  dup=st.get("skip_duplicate", 0), total=len(results))
    return result


# ============================================================================
# メイン runner
# ============================================================================
def run(dry_run: bool = True, limit_accounts: Optional[int] = None,
        media_filter: str = "BOTH",
        hr_date_from: str = "", hr_date_to: str = "",
        source: str = "queue", hr_cutoff_iso: str = "") -> dict:
    lock = Lock()
    if not lock.acquire():
        print("[applicant_sync] 別インスタンス実行中 → skip", flush=True)
        return {"skipped": True}
    ledger = Ledger()
    out_dir = _REPO / "scratchpad" / "applicant_sync_csv"
    summary = {"accounts": 0, "done": 0, "failed": 0, "reported": 0,
               "unresolved": 0, "linked": 0}
    try:
        if source == "sheet1":
            # GAS queue座礁の迂回: シート1を直読み(F列マーカーで未処理判定)。
            # cutoff未指定なら直近14日(処理を軽く保つ。古いbacklogは別途catch-up)。
            if not hr_cutoff_iso:
                from datetime import date as _date, timedelta as _td
                hr_cutoff_iso = (_date.today() - _td(days=14)).isoformat()
            print(f"[applicant_sync] シート1直読み "
                  f"(cutoff={hr_cutoff_iso}, dry_run={dry_run})", flush=True)
            items = aq.read_new_items_from_sheet1(
                cutoff_iso=hr_cutoff_iso,
                media_filter=("HR" if media_filter == "BOTH" else media_filter))
        else:
            print(f"[applicant_sync] queue検知... (dry_run={dry_run})", flush=True)
            items = aq.read_new_items()
        # 台帳で既にDONEの項目は除外
        items = [it for it in items if ledger.status(it.row_id) != "DONE"]
        resolver = aq.AccountResolver().build()
        grouped, unresolved = aq.aggregate_by_account(items, resolver)

        # 未突合 → 報告(放置ゼロ)
        if unresolved:
            summary["unresolved"] = len(unresolved)
            samp = ", ".join(f"{u.company[:14]}({u.login_id[:20]})"
                             for u in unresolved[:5])
            slack_notify(dry_run=dry_run, message=
                f"⚠️ 応募連携: アカウント特定不可 {len(unresolved)}件 "
                f"(要手動確認) 例: {samp}")

        # ---- HR: 1マスターで日付範囲一括 (リクロジアドレスで100%突合) ----
        hr_items = grouped.get("HR::master", [])
        if hr_items and media_filter in ("HR", "BOTH"):
            print(f"[applicant_sync] HR: {len(hr_items)}件を1マスターで処理",
                  flush=True)
            res = process_hr_batch(hr_items, out_dir, dry_run,
                                   date_from=hr_date_from, date_to=hr_date_to)
            if res["ok"]:
                # 放置ゼロ: 取得した日付範囲に入る項目だけ DONE。
                # 範囲外(古い/未来)は未登録なので NEW のまま残す(次の広い範囲で処理)。
                df, dt = res["date_from"], res["date_to"]
                covered = uncovered = 0
                covered_rows: list = []
                for it in hr_items:
                    d = _q_date_to_iso(it.columns.get("D", ""))
                    if d and df <= d <= dt:
                        ledger.mark(it.row_id, "DONE")
                        covered += 1
                        if it.sheet_row:
                            covered_rows.append(it.sheet_row)
                    else:
                        uncovered += 1  # NEW のまま
                # シート1直読み時: 重複防止の本命は台帳(ledger, row_id=内容ハッシュ,
                # 行削除に強い)。F列マーカーは"あれば尚可"の best-effort(書込権限が
                # 無ければ台帳のみで機能する。403等でrun全体を落とさない)。
                if source == "sheet1" and not dry_run and covered_rows:
                    try:
                        m = aq.mark_sheet1_rows(covered_rows)
                        print(f"  シート1 F列マーク: {m}行", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"  シート1 F列マーク skip(書込権限無し等/台帳で重複防止): "
                              f"{type(e).__name__}", flush=True)
                summary["done"] += covered
                summary["linked"] += res.get("linked", 0)
                print(f"  ✅ HR: 応募取込 linked={res.get('linked')} "
                      f"unlinked={res.get('unlinked')} dup={res.get('dup')} "
                      f"範囲={df}〜{dt} / queue項目 DONE={covered} "
                      f"範囲外残={uncovered}", flush=True)
            else:
                atts = 0
                for it in hr_items:
                    atts = ledger.bump(it.row_id, res["error"])
                summary["failed"] += len(hr_items)
                if atts >= MAX_ATTEMPTS:
                    summary["reported"] += 1
                    slack_notify(dry_run=dry_run, message=
                        f"⚠️ HR応募登録失敗: 理由={res['error']} / {atts}回 / 要確認")
                print(f"  {'❌' if atts >= MAX_ATTEMPTS else '⏳'} HR: "
                      f"{res['error']} ({atts}/{MAX_ATTEMPTS}回)", flush=True)

        if media_filter not in ("AW", "BOTH"):
            if not dry_run:
                ledger.save()  # dry-runは台帳を汚さない(実登録してないのにDONEにしない)
            print(f"[applicant_sync-done] {summary}", flush=True)
            return summary   # lock は finally で解放

        aw_groups = {k: v for k, v in grouped.items() if k.startswith("AW::")}
        # BAN対策: 1runのAWアカウント数を上限でcap(一斉ログイン回避)。
        cap = limit_accounts if limit_accounts else MAX_AW_ACCOUNTS_PER_RUN
        keys = list(aw_groups)[:cap]
        if len(aw_groups) > len(keys):
            print(f"[applicant_sync] AW {len(aw_groups)}社中 今回{len(keys)}社処理 "
                  f"(残{len(aw_groups)-len(keys)}社は次サイクル / BAN対策の分散)",
                  flush=True)
        for key in keys:
            group = aw_groups[key]
            company = key[len("AW::"):]
            summary["accounts"] += 1
            # そのアカウントのB系認証を resolver から (代表itemで再解決)
            acc = resolver.resolve(group[0])
            if acc is None or acc.closed:
                for it in group:
                    ledger.mark(it.row_id, "SKIP", "closed/unresolved")
                continue
            res = process_aw_account(company, acc.b_ids, acc.b_pw, out_dir, dry_run)
            if res["ok"]:
                for it in group:
                    ledger.mark(it.row_id, "DONE")
                summary["done"] += len(group)
                summary["linked"] += res.get("linked", 0)
                print(f"  ✅ {company[:18]}: 応募{len(group)}件 "
                      f"linked={res.get('linked')} unlinked={res.get('unlinked')} "
                      f"dup={res.get('dup')} 求人fetch={res.get('jobs_fetched')} (login={res.get('login_id')})", flush=True)
            else:
                # 失敗 → リトライ回数を数え、上限超過で Slack 報告
                atts = 0
                for it in group:
                    atts = ledger.bump(it.row_id, res["error"])
                summary["failed"] += len(group)
                if atts >= MAX_ATTEMPTS:
                    summary["reported"] += 1
                    slack_notify(dry_run=dry_run, message=
                        f"⚠️ 応募登録失敗: {company} / 理由={res['error']} / "
                        f"{atts}回試行済 / 要手動確認")
                    print(f"  ❌ {company[:18]}: {res['error']} "
                          f"({atts}回→Slack報告)", flush=True)
                else:
                    print(f"  ⏳ {company[:18]}: {res['error']} "
                          f"({atts}/{MAX_ATTEMPTS}回, 次サイクル再試行)", flush=True)
        if not dry_run:
            ledger.save()  # dry-runは台帳を汚さない
    finally:
        lock.release()
    print(f"[applicant_sync-done] {summary}", flush=True)
    return summary


def relink(dry_run: bool = False, limit: int = 1000) -> dict:
    """再紐付けスイープ (順序問題の恒久解決).

    「求人が登録される前に来た応募」は対象外で記録される(dedupで残る)。
    その後 求人巡回が LISTING を作れば、本スイープが oubokyuujinmemo(媒体求人ID)で
    LISTINGを引いて Association(typeId=5) を張り、対象外→未転記 に解除する。
    → 求人と応募の到着順序に依存せず、必ず後から紐付く。求人巡回の後に定期実行。
    """
    import re as _re
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    cli = ai.RealHubSpotClient(token)

    # 1) 対象外APPOINTMENTを全走査して (appt_id, jid, is_hr) を収集 (検索はまだしない)
    targets = []  # [(appt_id, jid, is_hr)]
    checked = 0
    after = None
    while checked < limit:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "kokyakushiitotenkijoukyou",
             "operator": "EQ", "value": "対象外"}]}],
            "properties": ["oubokyuujinmemo", "oubobaitaimei",
                           "bikou_hiaringu"], "limit": 100}
        if after:
            body["after"] = after
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/0-421/search",
            headers=H, json=body, timeout=30).json()
        for o in r.get("results", []):
            checked += 1
            p = o.get("properties") or {}
            jid = (p.get("oubokyuujinmemo") or "").strip()
            if not jid:  # 旧backlogは bikou_hiaringu から抽出
                m = _re.search(r"媒体求人ID=(\S+)", p.get("bikou_hiaringu") or "")
                jid = m.group(1) if m else ""
            if not jid:
                continue
            media = p.get("oubobaitaimei") or ""
            targets.append((o["id"], jid, ("HR" in media or "ハッカー" in media)))
        after = r.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    # 2) 求人IDを媒体別に集約し、LISTINGをバッチ IN検索 (100件/req) して map化
    #    値=(listing_id, 一次対応フラグ)。曖昧(複数一致)は None。
    def _listing_map(job_ids, prop):
        m = {}
        ids = sorted(set(job_ids))
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            r = requests.post(
                "https://api.hubapi.com/crm/v3/objects/0-420/search",
                headers=H, json={"filterGroups": [{"filters": [
                    {"propertyName": prop, "operator": "IN", "values": chunk}]}],
                    "properties": [prop, "ichijitaiounoumu_deforuto"],
                    "limit": 200}, timeout=30).json()
            for o in r.get("results", []):
                p = o.get("properties") or {}
                v = p.get(prop)
                if not v:
                    continue
                m[v] = None if v in m else (
                    o["id"], p.get("ichijitaiounoumu_deforuto"))  # 2件目=曖昧
            time.sleep(0.1)
        return m
    hr_map = _listing_map([j for _, j, ish in targets if ish], "id_hrhakkaa")
    aw_map = _listing_map([j for _, j, ish in targets if not ish], "id_airwork")

    # 3) 突合できた対象外だけ Association + 対象外解除 + 一次対応引き継ぎ
    relinked = still = ambiguous = 0
    note_copied = 0
    for appt_id, jid, is_hr in targets:
        hit = (hr_map if is_hr else aw_map).get(jid, "__miss__")
        if hit == "__miss__":
            still += 1
            continue
        if hit is None:            # 曖昧(複数LISTING)は§24で紐付けない
            ambiguous += 1
            continue
        lid, ichijitaiou = hit
        if not dry_run:
            cli.associate_appointment_to_listing(appt_id, lid)
            props = {"kokyakushiitotenkijoukyou": "未転記"}
            if ichijitaiou in ("必要", "不要"):
                props["ichijitaiounoumu"] = ichijitaiou   # 求人→応募 引き継ぎ
            # 応募先求人情報11項目も引き継ぐ (AWのlogin_idは対象外appt側に無いため
            # LISTING直読み+HR関連付けのみ。best-effort)
            media = "HRハッカー" if is_hr else "AirWork"
            try:
                props.update(cli.get_oubosaki_props(lid, media, "", jid))
            except Exception:  # noqa: BLE001
                pass
            requests.patch(
                f"https://api.hubapi.com/crm/v3/objects/0-421/{appt_id}",
                headers=H, json={"properties": props}, timeout=20)
            # ②求人のピン留め暗黙知Noteを応募へ複製 (一次対応コーラー用, best-effort)
            if cli.copy_listing_note(lid, appt_id):
                note_copied += 1
        relinked += 1
    summary = {"checked": checked, "relinked": relinked,
               "still_unlinked": still, "ambiguous": ambiguous,
               "note_copied": note_copied}
    if relinked and not dry_run:
        slack_notify(f"🔗 再紐付けスイープ: {relinked}件の対象外応募を求人に紐付け "
                     f"(確認{checked}件/未紐付け{still}/曖昧{ambiguous}/"
                     f"暗黙知Note複製{note_copied})")
    print(f"[relink] {summary}", flush=True)
    return summary


def reconcile(dry_run: bool = False) -> dict:
    """照合スイープ (放置ゼロの最後の砦, 日次想定).

    台帳とqueueを突合し「詰まった項目」を検出してSlack報告する:
      - FAILED で試行上限に達した項目 (自動回復せず放置になりかけ)
      - 長期 NEW のまま処理されていない項目 (queueにあるが未着手)
    黙って宙に浮く項目をゼロにするための人手トリガー。
    """
    ledger = Ledger()
    stuck_failed = [rid for rid, e in ledger.data.items()
                    if e.get("status") == "FAILED"
                    and e.get("attempts", 0) >= MAX_ATTEMPTS]
    try:
        items = aq.read_new_items()
    except Exception as e:  # noqa: BLE001
        slack_notify(f"⚠️ 照合スイープ: queue読取失敗 {e}", dry_run)
        return {"error": str(e)}
    # queueにあるが台帳で未DONE/未FAILEDのまま溜まっている件数
    pending = [it for it in items if ledger.status(it.row_id) in ("NEW",)]
    report = {"stuck_failed": len(stuck_failed), "pending_new": len(pending)}
    if stuck_failed or len(pending) > 200:
        slack_notify(
            f"🔎 照合スイープ: 要確認 — リトライ上限失敗={len(stuck_failed)}件 / "
            f"未処理NEW滞留={len(pending)}件 (放置ゼロ点検)", dry_run)
    print(f"[reconcile] {report}", flush=True)
    return report


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="応募連携 本線 runner")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    g.add_argument("--actual", dest="dry_run", action="store_false")
    p.add_argument("--limit-accounts", type=int, default=None)
    p.add_argument("--media", choices=["AW", "HR", "BOTH"], default="BOTH")
    p.add_argument("--hr-date-from", default="")
    p.add_argument("--hr-date-to", default="")
    p.add_argument("--reconcile", action="store_true",
                   help="照合スイープ(日次): 詰まった項目を検出しSlack報告")
    p.add_argument("--relink", action="store_true",
                   help="再紐付けスイープ: 対象外応募を後から出来た求人に紐付け")
    p.add_argument("--source", choices=["queue", "sheet1"], default="queue",
                   help="応募の入力元。sheet1=GAS queue座礁を迂回しシート1を直読み")
    p.add_argument("--hr-cutoff", default="",
                   help="シート1直読み時、この日付(YYYY-MM-DD)以降の応募のみ処理")
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _parse_args()
    if a.relink:
        relink(dry_run=a.dry_run)
        sys.exit(0)
    if a.reconcile:
        reconcile(dry_run=a.dry_run)
        sys.exit(0)
    run(dry_run=a.dry_run, limit_accounts=a.limit_accounts,
        media_filter=a.media, hr_date_from=a.hr_date_from,
        hr_date_to=a.hr_date_to, source=a.source, hr_cutoff_iso=a.hr_cutoff)
