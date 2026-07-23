"""AW 顧客並列 orchestrator (Phase 2c).

目的:
    Active 約 496 社の Air Work アカウントに対して、
        account_loader.iter_aw_accounts  (ID/PW 列挙)
        → aw_csv_fetcher.fetch_aw_xlsx   (顧客別 XLSX を Playwright で取得)
        → airwork_import.run_xlsx        (HubSpot LISTING upsert / A-2)
    を 5〜10 社並列で実行する。

設計:
    - asyncio + Semaphore で並列度を制御 (parallel パラメータ既定 5)
    - 各社処理は独立。例外は捕捉して results に詰め、全体は止めない。
    - PW など機微情報はログに保存しない (sanitize 済 dict のみ persist)。
    - 失敗顧客は status="error" + error/trace で記録 (再実行できるよう login_id 残置)。
    - A-2 (airwork_import.run_xlsx) は同期 API のため
      loop.run_in_executor(None, ...) で別スレッド実行 (HubSpot batch を I/O 並列化)。
    - 進捗: 1 社完了ごとに stderr へ [done N/total] を出す。

CLI:
    python -m scripts.job_application_sync.fetchers.aw_orchestrator \\
        [--parallel 5] [--limit 3] [--actual] [--headful]

DoD:
    - 既定は dry_run。--actual 指定時のみ HubSpot に書込が走る。
    - --limit N で先頭 N 社のみ実行 (動作確認用)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# ---- 自己解決: scripts.* で import できるよう sys.path にリポルートを通す ----
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]  # .../<repo>
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# 並列ジョブの依存 (別タスクで実装される / orchestrator 自体は import に依存しない)
from scripts.job_application_sync.fetchers.account_loader import (  # type: ignore  # noqa: E402
    iter_aw_accounts,
    resolve_accounts_for_mails,
)
from scripts.job_application_sync.fetchers import aw_csv_fetcher as _awf  # type: ignore  # noqa: E402
from scripts.job_application_sync.fetchers.aw_csv_fetcher import (  # type: ignore  # noqa: E402
    fetch_aw_xlsx,
    AWNoDataError,
    AWNotReadyError,
    CREATE_DONE,
)
from scripts.job_application_sync.fetchers.account_loader import (  # type: ignore  # noqa: E402
    find_account_by_login_id,
)

# A-2 を関数呼出で再利用
from scripts.job_application_sync import airwork_import as awi  # noqa: E402


def _extract_xlsx_from_zip(zip_or_xlsx: Path) -> Path:
    """ZIP なら中の .xlsx を temp に展開して返す。既に xlsx ならそのまま返す。

    fetch_aw_xlsx は ZIP を返すが A-2 の run_xlsx は xlsx 直接受取のため、
    orchestrator 側で中継変換する (Phase 2 動作確認 2026-06-29)。
    """
    import zipfile
    import tempfile
    p = Path(str(zip_or_xlsx))
    if p.suffix.lower() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return p
    if p.suffix.lower() == ".zip":
        tmpdir = Path(tempfile.mkdtemp(prefix="aw_orch_"))
        with zipfile.ZipFile(str(p)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xlsx"):
                    extracted = z.extract(name, tmpdir)
                    return Path(extracted)
        raise RuntimeError(f"No xlsx in zip: {p}")
    raise RuntimeError(f"Unsupported file format: {p}")


def _extract_client_code(xlsx_path: Path) -> str:
    """XLSX col3 (client_code) を軽量抽出。A-2 の login_id 引数に渡す。

    Phase 2 動作確認 2026-06-29: account_loader の login_id は SSO email、
    A-2 run_xlsx の login_id 引数は client_code (HubSpot airwork_account_login_id)。
    型不一致を埋めるため orchestrator 側で XLSX row2/col4(1-indexed)=client_code を取得して渡す。
    """
    import openpyxl  # type: ignore
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"]
        val = ws.cell(row=2, column=4).value
        return str(val) if val else ""
    finally:
        wb.close()


# ============================================================================
# 1 顧客処理
# ============================================================================
# run跨ぎで永続させる状態ディレクトリ (actions/cache が保持するパス)。
# out_dir(scratchpad/csv_fetched/aw)はcache対象外でcursorが消えるため分離する
# (2026-07-09 検証で run跨ぎ cursor非永続を実測→ここへ修正)。
STATE_DIR = Path("data/job_application_sync")


def _rotate_slice(accounts: list, limit: int, out_dir: Path, key: str) -> list:
    """全社を N社ずつローテーション。カーソルを cache対象パスに保存し続きから返す。

    「パッチで順番に繰り返す」オンデマンド政策(2026-07-09)。全社一斉を避け、
    1runでN社、次runは続きのN社を処理。末尾まで行ったら先頭へ折返す。
    """
    n = len(accounts)
    if n <= limit:
        return accounts
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cur_path = STATE_DIR / "aw_rotation_cursor.json"
    cursor = 0
    if cur_path.exists():
        try:
            cursor = int(json.loads(cur_path.read_text(encoding="utf-8")).get(key, 0))
        except Exception:  # noqa: BLE001
            cursor = 0
    cursor %= n
    end = cursor + limit
    if end <= n:
        sliced = accounts[cursor:end]
    else:                                   # 折返し
        sliced = accounts[cursor:] + accounts[: end - n]
    new_cursor = end % n
    try:
        data = {}
        if cur_path.exists():
            data = json.loads(cur_path.read_text(encoding="utf-8"))
        data[key] = new_cursor
        cur_path.parent.mkdir(parents=True, exist_ok=True)
        cur_path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"[orchestrate] incremental: {n}社中 cursor {cursor}→{new_cursor} "
          f"の{len(sliced)}社を処理", flush=True)
    return sliced


def _classify_error(err: str) -> str:
    """NG理由を住み分け用に分類 (login失敗/生成/その他)."""
    e = (err or "").lower()
    if any(k in e for k in ["login", "sso", "id/pw", "パスワード",
                            "資格", "dashboards に遷移", "認証"]):
        return "login_fail"
    if any(k in e for k in ["429", "quota", "sheets"]):
        return "sheets_429"
    if any(k in e for k in ["not_ready", "生成", "timeout", "タイムアウト"]):
        return "generation"
    return "other"


def _slack_notify(message: str) -> None:
    """AW巡回結果サマリをSlackへ (webhook未設定なら何もしない)."""
    url = os.environ.get("SLACK_APPLICANT_ALERT_WEBHOOK", "")
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"text": message}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _write_account_results(results: list, out_dir: Path, phase: str) -> dict:
    """per-account結果を状態JSONに保存し、住み分けサマリを返す (C: ログ行き先B)."""
    from collections import Counter
    cat = Counter()
    detail = []
    for r in results:
        st = r.get("status")
        if st in ("ok", "queued"):
            cat["login_success"] += 1
            outcome = "login_success"
        elif st == "not_ready":
            cat["generation"] += 1
            outcome = "generation_not_ready"
        else:
            c = _classify_error(r.get("error", ""))
            cat[c] += 1
            outcome = c
        detail.append({"login_id": r.get("login_id"),
                       "company": r.get("company"),
                       "outcome": outcome, "error": r.get("error", "")[:120]})
    summary = dict(cat)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = STATE_DIR / f"aw_account_results_{phase}.json"  # cache対象パスに永続
        p.write_text(json.dumps({"summary": summary, "detail": detail},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return summary


async def process_one(sem: asyncio.Semaphore, acc: dict, out_dir: Path,
                      dry_run: bool, headless: bool,
                      phase: str = "full") -> dict:
    """1 顧客処理。常に dict 返却 (exception も captured)。

    phase:
      - "full"   : login→生成→DL→A-2取込 (従来)
      - "create" : login→生成トリガーのみ (Phase1)。status="queued"
      - "collect": キューから login→(待機なし)DL→A-2取込 (Phase2)。
                   生成物未完は status="not_ready" (再キュー)
    """
    async with sem:
        login_id = acc.get("login_id", "")
        company = acc.get("company_name", "")
        try:
            t0 = datetime.now()

            # --- Phase1: 生成トリガーのみ ---
            if phase == "create":
                await fetch_aw_xlsx(login_id, acc.get("password", ""), out_dir,
                                    headless=headless, mode="create")
                return {"login_id": login_id, "company": company,
                        "status": "queued",
                        "duration_sec": (datetime.now() - t0).total_seconds()}

            # --- Phase2(collect) / full: DL→A-2 ---
            mode = "collect" if phase == "collect" else "full"
            zip_or_xlsx = await fetch_aw_xlsx(
                login_id, acc.get("password", ""), out_dir,
                headless=headless, mode=mode,
            )
            xlsx_path = _extract_xlsx_from_zip(zip_or_xlsx)
            client_code = _extract_client_code(xlsx_path)
            # fetch_aw_xlsx がセッション中に抽出・キャッシュした採用サイトURLを取得。
            # url_airwork 空欄の求人を slug+求人IDで補完する。
            # (テストで aw_csv_fetcher がスタブの場合は関数が無いので防御的に取得)
            recruit_url = ""
            _loader = getattr(_awf, "load_recruit_url_cache", None)
            if _loader:
                recruit_url = _loader(out_dir).get(login_id, "")
            # 採用サイトURLがある時だけ渡す (空なら従来シグネチャで呼ぶ)
            xkw = {"recruit_site_url": recruit_url} if recruit_url else {}
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: awi.run_xlsx(xlsx_path, client_code, dry_run=dry_run,
                                     **xkw),
            )
            t1 = datetime.now()
            return {
                "login_id": login_id, "company": company, "status": "ok",
                "xlsx_path": str(xlsx_path), "result": result,
                "duration_sec": (t1 - t0).total_seconds(),
            }
        except AWNotReadyError as e:
            # Phase2で生成物未完 → 再キュー
            return {"login_id": login_id, "company": company,
                    "status": "not_ready", "error": str(e)}
        except AWNoDataError as e:
            return {
                "login_id": login_id, "company": company, "status": "ok",
                "empty": True,
                "result": {"input": 0, "updates": 0, "creates": 0,
                           "skipped_01": 0, "note": "求人0件(データがありません)"},
                "duration_sec": (datetime.now() - t0).total_seconds(),
            }
        except Exception as e:
            return {
                "login_id": login_id, "company": company, "status": "error",
                "error": str(e),
                "trace": traceback.format_exc().splitlines()[-5:],
            }


# ============================================================================
# orchestrator 本体
# ============================================================================
def _sanitize(r: dict) -> dict:
    """ログ書出前のフィールド除去 (PW 等の機微情報を念のため落とす)。

    process_one は元々 PW を持たない result を返すが、
    将来 acc dict 全体を埋め込んだ場合に備えて防御的に password/secret を除去。
    """
    bad = {"password", "passwd", "secret", "token", "api_key"}
    d: dict = {}
    for k, v in r.items():
        if k in bad:
            continue
        if isinstance(v, dict):
            d[k] = _sanitize(v)
        else:
            d[k] = v
    return d


PIPELINE_NOUHIN_RIKUROJI = "21596025"  # 納品管理_リクロジ = 現役顧客
# Phase1→Phase2 引き継ぎキュー (生成トリガー済み顧客)
QUEUE_PATH = _REPO / "data" / "job_application_sync" / "aw_pending_queue.json"


def _load_queue() -> dict:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_queue(q: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(q, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def _fetch_active_deal_mails(pipeline: str = PIPELINE_NOUHIN_RIKUROJI) -> list[str]:
    """アクティブDeal(指定pipeline)の kanri_mail_address を全取得 (セミコロン分解)。

    Deal起点の巡回対象抽出用。HubSpot Search をページングで全件走査。
    """
    load_dotenv(_REPO / ".env")
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    mails, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "kanri_mail_address", "operator": "HAS_PROPERTY"},
                {"propertyName": "pipeline", "operator": "EQ", "value": pipeline},
            ]}],
            "properties": ["kanri_mail_address"], "limit": 100,
        }
        if after:
            body["after"] = after
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/0-3/search",
            headers=hdr, json=body, timeout=30).json()
        for o in r.get("results", []):
            v = o.get("properties", {}).get("kanri_mail_address") or ""
            for m in v.replace(",", ";").split(";"):
                if m.strip():
                    mails.append(m.strip())
        after = r.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return mails


async def orchestrate(parallel: int = 5,
                      limit: Optional[int] = None,
                      dry_run: bool = True,
                      headless: bool = True,
                      out_dir: Optional[Path] = None,
                      log_dir: Optional[Path] = None,
                      target_login_ids: Optional[list] = None,
                      from_active_deals: bool = False,
                      from_applications: bool = False,
                      applications_cutoff: str = "",
                      phase: str = "full") -> dict:
    """AW アカウントを並列処理。

    Args:
        parallel:  Semaphore 上限 (既定 5)
        limit:     先頭 N 社のみ (テスト用 / None=全件)
        dry_run:   True なら HubSpot 書込なし
        headless:  Playwright headless モード
        out_dir:   XLSX 保存先 (既定 scratchpad/csv_fetched/aw)
        log_dir:   ログ保存先 (既定 scripts/job_application_sync/logs)
        target_login_ids: 指定された login_id のみ実行 (hr_watcher 連携用)。
                           None なら active 全件。

    Returns:
        {ok, ng, total, log}
    """
    out_dir = Path(out_dir) if out_dir else Path("scratchpad/csv_fetched/aw")
    out_dir.mkdir(parents=True, exist_ok=True)

    log_dir = Path(log_dir) if log_dir else (
        _HERE.parent / "logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(parallel)

    # --- Phase2(collect): キューから対象を構築 (PWはlogin_idで再解決) ---
    if phase == "collect":
        queue = _load_queue()
        # login_id でソート=毎runで安定した順序。_save_queue が順序を変えても
        # ローテーションカーソルが決定的に前進する(全件を漏れなく巡回)。
        q_items = sorted(queue.items())
        # ★重大バグ修正(2026-07-22): 旧実装はキュー全件(259件)を1回の gather で処理し、
        #   job timeout(30分)に収まらず gather途中でcancel→_save_queue不達→キュー更新も
        #   作成済みLISTINGも破棄→永久停滞(LISTING 0)。
        # → ローテーションスライスで毎run N件だけ処理し、必ず完了→キュー永続+LISTING前進。
        #   未処理(スライス外)は保持、処理済のok/empty/errorは除去、not_readyは残す。
        slice_n = limit if limit else int(os.environ.get("AW_COLLECT_SLICE", "20"))
        q_slice = _rotate_slice(q_items, slice_n, out_dir, key="collect")
        accounts, processed_lids = [], set()
        for lid, meta in q_slice:
            processed_lids.add(lid)
            acc = find_account_by_login_id(lid)
            if acc:
                accounts.append(acc)
            else:
                accounts.append({"login_id": lid,
                                 "company_name": meta.get("company", ""),
                                 "password": ""})
        print(f"[orchestrate] phase=collect: キュー {len(queue)}件 → 今回 {len(accounts)}社処理"
              f"(残{len(queue)-len(accounts)}社は次サイクル)", flush=True)
        total = len(accounts)
        if total == 0:
            print("[orchestrate] キュー空。Phase1(create)を先に実行してください。", flush=True)
            return {"ok": 0, "ng": 0, "total": 0, "log": None}
        done_count = {"n": 0}

        async def _wrapped_c(acc):
            r = await process_one(sem, acc, out_dir, dry_run, headless, phase="collect")
            done_count["n"] += 1
            print(f"[collect {done_count['n']}/{total}] {r['status']} "
                  f"login_id={r.get('login_id')} ({r.get('company','')[:18]})",
                  flush=True)
            return r
        results = await asyncio.gather(*[_wrapped_c(a) for a in accounts])
        # キュー更新: 未処理(スライス外)は保持 / 処理済は not_ready のみ残す
        # (ok/empty/error は除去)。全件一括でないため未処理を消さないのが要点。
        new_q = {lid: meta for lid, meta in queue.items()
                 if lid not in processed_lids}
        for r in results:
            if r["status"] == "not_ready":
                new_q[r["login_id"]] = queue.get(r["login_id"], {})
        _save_queue(new_q)
        ok = sum(1 for r in results if r["status"] == "ok")
        ng = sum(1 for r in results if r["status"] == "error")
        nr = sum(1 for r in results if r["status"] == "not_ready")
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = log_dir / f"aw_collect_{'dry' if dry_run else 'actual'}_{ts}.json"
        log_path.write_text(json.dumps(
            {"ok": ok, "ng": ng, "not_ready": nr,
             "results": [_sanitize(r) for r in results]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[collect-done] ok={ok} ng={ng} not_ready={nr}(再キュー) "
              f"queue残={len(new_q)} log={log_path}", flush=True)
        return {"ok": ok, "ng": ng, "not_ready": nr, "total": total,
                "log": str(log_path)}

    if from_applications:
        # 応募起点(ブートストラップ, 2026-07-23): AW求人がHubSpotに未存在の導入期に、
        # 「応募が来た社」の求人を一括習得するための入口。日常の差分検知は別(温存)。
        # 既存の read_new_items_from_sheet1 + AccountResolver を流用(車輪の再発明なし)。
        from scripts.job_application_sync import applicant_queue as _aq
        items = _aq.read_new_items_from_sheet1(
            cutoff_iso=applications_cutoff, media_filter="AW", limit=None)
        resolver = _aq.AccountResolver().build()
        seen: dict = {}
        for it in items:
            acc = resolver.resolve(it)
            if not acc or acc.closed:
                continue
            for bid in acc.b_ids:
                if bid and bid not in ("ー", "-") and bid not in seen:
                    seen[bid] = {"login_id": bid, "password": acc.b_pw,
                                 "company_name": acc.company}
        accounts = list(seen.values())
        print(f"[orchestrate] --from-applications: 応募{len(items)}件 → "
              f"AW解決アカウント {len(accounts)}社(cutoff={applications_cutoff})",
              flush=True)
    elif from_active_deals:
        # Deal起点: アクティブDeal(納品管理pipeline)のkanri_mail → AWアカウント解決。
        # シート順(古い会社混入)を排除し「現役顧客」のみを巡回対象にする。
        deal_mails = _fetch_active_deal_mails()
        accounts, unresolved = resolve_accounts_for_mails(
            deal_mails, active_only=True, prefer="A")
        print(
            f"[orchestrate] --from-active-deals: Deal管理メール "
            f"{len(set(m.lower() for m in deal_mails))}ユニーク → "
            f"AW解決 {len(accounts)} / 未解決 {len(unresolved)}",
            flush=True,
        )
        # 未解決(シートID/PW整備で埋まる分)をログ出力
        if unresolved:
            ur_path = (log_dir /
                       f"aw_unresolved_deal_mails_"
                       f"{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
            ur_path.write_text(json.dumps(
                {"unresolved_count": len(unresolved), "mails": unresolved},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[orchestrate] 未解決メール一覧: {ur_path}", flush=True)
    else:
        accounts = list(iter_aw_accounts(active_only=True))
    if target_login_ids:
        target_set = {str(e).strip() for e in target_login_ids if str(e).strip()}
        accounts = [a for a in accounts if a.get("login_id") in target_set]
        print(
            f"[orchestrate] --target-login-ids filter: "
            f"{len(accounts)}/{len(target_set)} matched",
            flush=True,
        )
    # incremental: 全社一斉を避け「N社ずつローテーション」で巡回 (2026-07-09)。
    # from_active_deals/from_applications + limit のときカーソルを進めて続きから処理。
    if limit and (from_active_deals or from_applications) and not target_login_ids:
        accounts = _rotate_slice(accounts, limit, out_dir, key=phase)
    elif limit:
        accounts = accounts[:limit]
    total = len(accounts)
    print(
        f"[orchestrate] target={total} accounts, "
        f"parallel={parallel}, dry_run={dry_run}, headless={headless}",
        flush=True,
    )

    if total == 0:
        print("[orchestrate] no accounts; nothing to do.", flush=True)
        return {"ok": 0, "ng": 0, "total": 0, "log": None}

    # 進捗カウンタを wrap
    done_count = {"n": 0}

    async def _wrapped(acc: dict) -> dict:
        r = await process_one(sem, acc, out_dir, dry_run, headless, phase=phase)
        done_count["n"] += 1
        marker = {"ok": "OK ", "queued": "QUEUED ",
                  "error": "NG "}.get(r["status"], r["status"] + " ")
        print(
            f"[done {done_count['n']}/{total}] {marker}"
            f"login_id={r.get('login_id')} "
            f"({r.get('company','')[:20]})",
            flush=True,
        )
        return r

    tasks = [_wrapped(acc) for acc in accounts]
    results: list[dict] = await asyncio.gather(*tasks)

    # C: per-account結果を状態JSONに保存 + Slackサマリ (住み分け・ログ行き先A+B)
    acct_summary = _write_account_results(results, out_dir, phase)
    print(f"[orchestrate] 住み分け({phase}): {acct_summary}", flush=True)
    _slack_notify(
        f"🔄 AW巡回({phase}) {total}社処理: "
        + " / ".join(f"{k}={v}" for k, v in acct_summary.items()))

    # --- Phase1(create): 生成トリガー成功社をキューに保存 (Phase2引き継ぎ) ---
    if phase == "create":
        q = _load_queue()
        now = datetime.now().isoformat()
        for r in results:
            if r["status"] == "queued":
                q[r["login_id"]] = {"company": r.get("company", ""),
                                    "triggered_at": now}
        _save_queue(q)
        queued = sum(1 for r in results if r["status"] == "queued")
        empty = sum(1 for r in results if r.get("empty"))
        ng = sum(1 for r in results if r["status"] == "error")
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = log_dir / f"aw_create_{ts}.json"
        log_path.write_text(json.dumps(
            {"queued": queued, "empty": empty, "ng": ng,
             "results": [_sanitize(r) for r in results]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[create-done] queued={queued} empty={empty} ng={ng} "
              f"queue総数={len(q)} log={log_path}", flush=True)
        return {"queued": queued, "empty": empty, "ng": ng, "total": total,
                "log": str(log_path)}

    ok = sum(1 for r in results if r.get("status") == "ok")
    ng = sum(1 for r in results if r.get("status") == "error")

    # ログ保存 (PW マスク)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    sanitized: list[dict] = [_sanitize(r) for r in results]
    log_path = log_dir / f"aw_orchestrate_{'dry' if dry_run else 'actual'}_{ts}.json"
    log_payload: dict[str, Any] = {
        "executed_at": datetime.now().isoformat(),
        "parallel": parallel,
        "dry_run": dry_run,
        "headless": headless,
        "total": total,
        "ok": ok,
        "ng": ng,
        "results": sanitized,
    }
    log_path.write_text(
        json.dumps(log_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 失敗顧客だけの別ファイル (再実行用)
    failures = [r for r in sanitized if r.get("status") == "error"]
    if failures:
        fail_path = log_dir / f"aw_orchestrate_failures_{ts}.json"
        fail_path.write_text(
            json.dumps(failures, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[orchestrate] failures saved: {fail_path}", flush=True)

    # 顧客別 実行結果サマリ (エラー時も必ず結果を可視化する — 2026-07-07 ユーザー要望)
    # 「上手くいったのか失敗したのか」をログを開かずに判別できるようにする。
    print("[orchestrate-summary] 顧客別 実行結果:", flush=True)
    for r in sanitized:
        lid = r.get("login_id", "?")
        cname = r.get("company_name", "")
        if r.get("status") == "ok":
            res = r.get("result") or {}
            print(f"  ✅ OK   {lid} {cname} "
                  f"新規={res.get('creates_planned', res.get('creates', '?'))} "
                  f"更新={res.get('updates_planned', res.get('updates', '?'))}",
                  flush=True)
        else:
            print(f"  ❌ NG   {lid} {cname} 理由={r.get('error', '不明')[:120]}",
                  flush=True)

    print(
        f"[orchestrate-done] ok={ok} ng={ng} total={total} log={log_path}",
        flush=True,
    )
    return {
        "ok": ok, "ng": ng, "total": total, "log": str(log_path),
    }


# ============================================================================
# CLI
# ============================================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="AW 顧客並列 orchestrator (Phase 2c)"
    )
    p.add_argument("--parallel", type=int, default=5,
                   help="並列度 (既定 5)")
    p.add_argument("--limit", type=int, default=None,
                   help="先頭 N 社のみ (テスト用)")
    p.add_argument("--actual", action="store_true",
                   help="本番実行 (HubSpot に書込)。未指定なら dry-run")
    p.add_argument("--headful", action="store_true",
                   help="Playwright headful (動作確認用)。既定 headless")
    p.add_argument("--out-dir", default=None,
                   help="XLSX 保存先 (既定 scratchpad/csv_fetched/aw)")
    p.add_argument("--target-login-ids", default=None,
                   help="カンマ区切りの login_id 一覧。"
                        "指定時は active 全件ではなく該当アカウントのみ実行 "
                        "(hr_watcher 連携用)")
    p.add_argument("--from-active-deals", action="store_true",
                   help="アクティブDeal(納品管理pipeline)のkanri_mail から巡回対象を"
                        "抽出 (現役顧客のみ)。シート順の古い会社混入を排除。"
                        "未解決メールは aw_unresolved_deal_mails_*.json に出力")
    p.add_argument("--from-applications", action="store_true",
                   help="応募起点(導入期ブートストラップ): 応募が来た社の求人を一括習得。"
                        "シート1のAW応募→AWアカウント解決を対象にする")
    p.add_argument("--applications-cutoff", default="",
                   help="--from-applications の応募日カットオフ(ISO YYYY-MM-DD, 空=全件)")
    p.add_argument("--phase", choices=["full", "create", "collect"],
                   default="full",
                   help="full=生成+DL+取込(従来) / create=生成トリガーのみ(Phase1,"
                        "対象をキュー保存) / collect=キューから待機なしDL+取込(Phase2)。"
                        "クラウド課金の生成待ち時間を分離する")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    target_ids = None
    if args.target_login_ids:
        target_ids = [s.strip() for s in args.target_login_ids.split(",")
                      if s.strip()]
    asyncio.run(orchestrate(
        parallel=args.parallel,
        limit=args.limit,
        dry_run=not args.actual,
        headless=not args.headful,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        target_login_ids=target_ids,
        from_active_deals=args.from_active_deals,
        from_applications=args.from_applications,
        applications_cutoff=args.applications_cutoff,
        phase=args.phase,
    ))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
