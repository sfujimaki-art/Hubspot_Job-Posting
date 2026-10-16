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
import re
import sys
import time
import traceback
from datetime import date, datetime
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
from scripts.job_application_sync import private_log as plog  # noqa: E402
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
from scripts.job_application_sync.fetchers import aw_pace as _aw_pace  # noqa: E402
from scripts.job_application_sync.fetchers import aw_urgent_queue as _urgent  # noqa: E402



def _mask(v) -> str:
    """ログインIDを公開のActionsログに出さない (2026-10-05)。先頭2文字だけ残す。"""
    v = str(v or "")
    return (v[:2] + "…") if len(v) > 2 else "…"


def _drop_pw(text, password) -> str:
    """例外文からパスワードを消す。先頭2文字も残さない (2026-10-13)。"""
    t = str(text or "")
    pw = str(password or "")
    return t.replace(pw, "***") if len(pw) >= 3 else t


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _scrub(text, secrets=()) -> str:
    """例外文に埋め込まれたログインID等を伏せる (2026-10-08)。

    ★aw_csv_fetcher の例外文は `login_id=...` を生のまま含む。公開リポジトリの
      Actions ログに出る直前で、既知の値とメール形式の文字列をまとめて伏せる。
    """
    t = str(text or "")
    for v in sorted({str(x) for x in secrets if x and len(str(x)) >= 3},
                    key=len, reverse=True):
        t = t.replace(v, _mask(v))
    return _EMAIL.sub(lambda m: _mask(m.group(0)), t)


def _summary_lines(results: list) -> list:
    """顧客別 実行結果サマリの行。ログインID・会社名・理由は伏せる。

    ★2026-10-09: 会社名も伏せる (公開ログ)。全文は非公開ログ (private_log)。
    """
    lines = []
    for r in results:
        raw = r.get("login_id") or ""
        lid = _mask(raw) if raw else "?"
        cname = plog.mask_name(r.get("company_name", ""))
        if r.get("status") == "ok":
            res = r.get("result") or {}
            lines.append(f"  ✅ OK   {lid} {cname} "
                         f"新規={res.get('creates_planned', res.get('creates', '?'))} "
                         f"更新={res.get('updates_planned', res.get('updates', '?'))}")
        else:
            why = _scrub(r.get("error") or "不明", [raw])[:120]
            lines.append(f"  ❌ NG   {lid} {cname} 理由={why}")
    return lines

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


LAST_OK_PATH = STATE_DIR / "aw_crawl_last_ok.json"


def _load_last_ok(path: Path = None) -> dict:
    """login_id → 最後に求人を取れた時刻 (epoch 秒)。無ければ空。"""
    p = path or LAST_OK_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _record_last_ok(login_id: str, when: float = None, path: Path = None) -> None:
    """取れた社の時刻を記録する。社ごとに書くので、途中で止まっても続きから回れる。

    巡回と「すぐ取りに行く」処理の両方が書くので、毎回読み直してから1社分だけ足す。
    """
    p = path or LAST_OK_PATH
    try:
        data = _load_last_ok(p)
        data[login_id] = float(when if when is not None else time.time())
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def _order_stale_first(accounts: list, last_ok: dict) -> list:
    """最後に取れたのが古い社から並べる (一度も取れていない社が先頭)。同じなら元の順。

    ★2026-10-16 (ユーザー承認 A): 以前は「何社目まで進んだか」の番号で回していた。
      対象の社の一覧は回ごとに変わる (新しい応募・受注で増減し、順番も詰め直される) ので、
      番号がずれて同じ社を何度も取っていた (10/14〜15 の6回・のべ60社で、別々の社は31社)。
      最後に取れた時刻で並べれば、一覧が変わっても取れていない社から順に回る。
    """
    return sorted(accounts, key=lambda a: last_ok.get(a.get("login_id", ""), 0.0))


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
        # ★AirWork の CloudFront が 403 を返した後は触らない。触る前は、応募の取り込みと
        #   共通の「社と社の間の最低間隔」を守る (2026-10-13、fetchers/aw_pace.py)。
        if _aw_pace.is_blocked():
            return {"login_id": login_id, "company": company,
                    "status": "not_ready" if phase == "collect" else "error",
                    "error": "AirWork が403を返したため、アクセスを止めています"}
        await asyncio.to_thread(_aw_pace.wait_company_turn)
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
            # LISTINGのlogin表現は bid(顧客管理シートのログインID)に統一(2026-07-27)。
            # 旧: XLSX内の数値client_codeを渡していたため deal-assoc の
            # login→管理メール→Deal突合が全滅していた(識別子ズレ)。upsert突合は
            # id_airwork単独(find_hubspot_jobs)のため表現切替でも重複は生じない。
            _extract_client_code(xlsx_path)  # 妥当性確認のみ(XLSX形式チェック)
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
                lambda: awi.run_xlsx(xlsx_path, login_id, dry_run=dry_run,
                                     strict_client_code=False, **xkw),
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
                    "status": "not_ready",
                    "error": _drop_pw(str(e), acc.get("password"))}
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
                "error": _drop_pw(str(e), acc.get("password")),
                "trace": [_drop_pw(x, acc.get("password"))
                          for x in traceback.format_exc().splitlines()[-5:]],
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
                      phase: str = "full",
                      from_urgent_queue: bool = False) -> dict:
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
                  f"login_id={_mask(r.get('login_id'))} "
                  f"({plog.mask_name(r.get('company', ''))})",
                  flush=True)
            return r
        results = await asyncio.gather(*[_wrapped_c(a) for a in accounts])
        # キュー更新: 未処理(スライス外)は保持 / 処理済は not_ready のみ残す
        # (ok/empty/error は除去)。全件一括でないため未処理を消さないのが要点。
        # dry-run は保存しない(LISTING未作成のままokをキューから消す事故防止 2026-07-25)。
        new_q = {lid: meta for lid, meta in queue.items()
                 if lid not in processed_lids}
        for r in results:
            if r["status"] == "not_ready":
                new_q[r["login_id"]] = queue.get(r["login_id"], {})
        if dry_run:
            print("[collect] dry-run のためキュー更新はスキップ(保持)", flush=True)
        else:
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
        for r in results:
            if r.get("status") != "ok":
                plog.detail("aw_collect_result", login_id=r.get("login_id"),
                            company=r.get("company_name") or r.get("company"),
                            status=r.get("status"), error=r.get("error", ""))
        return {"ok": ok, "ng": ng, "not_ready": nr, "total": total,
                "log": str(log_path)}

    accounts = []
    if from_urgent_queue:
        # ★2026-10-16: 応募の求人が HubSpot に無かった社だけを取る (fetchers/aw_urgent_queue.py)。
        pend = _urgent.pending()
        if not pend:
            print("[orchestrate] すぐ取りに行く待ち行列は空です", flush=True)
            return {"ok": 0, "ng": 0, "total": 0, "log": None}
        index: dict = {}
        for prefer in ("A", "B"):
            for a in iter_aw_accounts(active_only=False, prefer=prefer):
                index.setdefault(a.get("login_id"), a)
        for lid in pend:
            if lid in index:
                accounts.append(index[lid])
            else:   # シートから消えた社は取りに行けない
                _urgent.finish(lid, ok=False)
        print(f"[orchestrate] すぐ取りに行く待ち行列: {len(pend)}社 → "
              f"今回 {min(len(accounts), limit or len(accounts))}社", flush=True)
    if from_applications:
        # 応募起点(ブートストラップ, 2026-07-23): AW求人がHubSpotに未存在の導入期に、
        # 「応募が来た社」の求人を一括習得するための入口。日常の差分検知は別(温存)。
        # 既存の read_new_items_from_sheet1 + AccountResolver を流用(車輪の再発明なし)。
        from scripts.job_application_sync import applicant_queue as _aq
        items = _aq.read_new_items_from_sheet1(
            cutoff_iso=applications_cutoff, media_filter="AW", limit=None)
        resolver = _aq.AccountResolver().build()
        seen: dict = {}
        # 最新の応募から遡る(シート1は下=最新のため逆順)。求人が生きている可能性が
        # 高い新しい応募のアカウントから習得し、紐付き歩留まりを最大化する(2026-07-23)。
        for it in reversed(items):
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
    if from_active_deals:
        # Deal起点: アクティブDeal(納品管理pipeline)のkanri_mail → AWアカウント解決。
        # シート順(古い会社混入)を排除し「現役顧客」のみを巡回対象にする。
        # ★2026-10-16 (ユーザー承認 B): --from-applications と一緒に指定したら両方を足す
        #   (受注中の全社 + 受注の一覧に無いが最近応募が来た社)。同じ login_id は1つにする。
        deal_mails = _fetch_active_deal_mails()
        deal_accounts, unresolved = resolve_accounts_for_mails(
            deal_mails, active_only=True, prefer="A")
        have = {a.get("login_id") for a in accounts}
        for a in deal_accounts:
            if a.get("login_id") not in have:
                have.add(a.get("login_id"))
                accounts.append(a)
        print(
            f"[orchestrate] --from-active-deals: Deal管理メール "
            f"{len(set(m.lower() for m in deal_mails))}ユニーク → "
            f"AW解決 {len(deal_accounts)} (応募起点と合わせて {len(accounts)}社) / "
            f"未解決 {len(unresolved)}",
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
    if not (from_applications or from_active_deals or from_urgent_queue):
        # 顧客管理シートは下=最新。最新顧客から検証するため逆順(下から)で処理する。
        accounts = list(iter_aw_accounts(active_only=True))[::-1]
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
    if (from_active_deals or from_applications) and not target_login_ids:
        accounts = _order_stale_first(accounts, _load_last_ok())
        if limit:
            print(f"[orchestrate] 最後に取れたのが古い順に {min(limit, len(accounts))}/"
                  f"{len(accounts)}社を処理", flush=True)
    if limit:
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
        if r["status"] == "ok" and phase == "full" and not dry_run:
            _record_last_ok(r.get("login_id", ""))
        if from_urgent_queue and not dry_run:
            _urgent.finish(r.get("login_id", ""), ok=r["status"] == "ok")
        marker = {"ok": "OK ", "queued": "QUEUED ",
                  "error": "NG "}.get(r["status"], r["status"] + " ")
        print(
            f"[done {done_count['n']}/{total}] {marker}"
            f"login_id={_mask(r.get('login_id'))} "
            f"({plog.mask_name(r.get('company', ''))})",
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
    for line in _summary_lines(sanitized):
        print(line, flush=True)
    for r in sanitized:
        plog.detail("aw_account_result", phase=phase, login_id=r.get("login_id"),
                    company=r.get("company_name") or r.get("company"),
                    status=r.get("status"), error=r.get("error", ""),
                    result=r.get("result"))

    print(
        f"[orchestrate-done] ok={ok} ng={ng} total={total} log={log_path}",
        flush=True,
    )
    return {
        "ok": ok, "ng": ng, "total": total, "log": str(log_path),
    }


# ============================================================================
# 有料広告実績 (billing) フェーズ
#   billing-create : 各社の先頭の未取得の週を作成するだけ (待たない)。キューに保存
#   billing-collect: キューから待機短めで ZIP を保存 (ローテーションスライス)
#   billing-full   : 1 週ずつ create → 完成待ち → 保存を週の数だけ直列 (バックフィル向け)
# HubSpot への書込はここでは行わない (airwork_billing_import.py が別途実行)。
# ============================================================================
BILLING_PHASES = ("billing-create", "billing-collect", "billing-full")
BILLING_QUEUE_PATH = _REPO / "data" / "job_application_sync" / "aw_billing_queue.json"
BILLING_EMPTY_PATH = _REPO / "data" / "job_application_sync" / "aw_billing_empty.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _billing_status(week_results: list, mode: str) -> str:
    """週ごとの結果から 1 アカウントの status を決める。"""
    st = {s for _, s, _ in week_results}
    if "error" in st:
        return "error"
    if "not_ready" in st:
        return "not_ready"
    if mode == "create" and "queued" in st:
        return "queued"
    return "ok"


async def process_billing_one(sem: asyncio.Semaphore, acc: dict, weeks: list,
                              out_dir: Path, headless: bool, mode: str) -> dict:
    """1 顧客の有料広告実績を処理。常に dict 返却 (例外も捕捉)。"""
    from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
    async with sem:
        login_id = acc.get("login_id", "")
        company = acc.get("company_name", "")
        pw = acc.get("password", "")
        t0 = datetime.now()
        try:
            res = await bf.fetch_aw_billing_weeks(
                login_id, pw, weeks, out_dir, headless=headless, mode=mode)
            counts: dict = {}
            for _, s, _ in res:
                counts[s] = counts.get(s, 0) + 1
            first_err = next((d for _, s, d in res if s in ("error", "not_ready") and d), "")
            return {"login_id": login_id, "company": company,
                    "status": _billing_status(res, mode),
                    "weeks": counts,
                    "week_status": {w.isoformat(): s for w, s, _ in res},
                    "error": _scrub(_drop_pw(first_err, pw), [login_id]),
                    "duration_sec": (datetime.now() - t0).total_seconds()}
        except Exception as e:  # noqa: BLE001
            return {"login_id": login_id, "company": company, "status": "error",
                    "error": _scrub(_drop_pw(str(e), pw), [login_id]),
                    "trace": [_scrub(_drop_pw(x, pw), [login_id])
                              for x in traceback.format_exc().splitlines()[-5:]]}


def _select_billing_accounts(from_active_deals: bool, target_login_ids) -> list:
    if from_active_deals:
        mails = _fetch_active_deal_mails()
        accounts, unresolved = resolve_accounts_for_mails(
            mails, active_only=True, prefer="A")
        print(f"[orchestrate] --from-active-deals: AW解決 {len(accounts)} / "
              f"未解決 {len(unresolved)}", flush=True)
    else:
        accounts = list(iter_aw_accounts(active_only=True))[::-1]
    if target_login_ids:
        tset = {str(e).strip() for e in target_login_ids if str(e).strip()}
        accounts = [a for a in accounts if a.get("login_id") in tset]
        print(f"[orchestrate] --target-login-ids filter: {len(accounts)}/{len(tset)} matched",
              flush=True)
    return accounts


async def orchestrate_billing(phase: str, weeks: list, parallel: int = 5,
                              limit: Optional[int] = None, headless: bool = True,
                              out_dir: Optional[Path] = None,
                              log_dir: Optional[Path] = None,
                              target_login_ids: Optional[list] = None,
                              from_active_deals: bool = False,
                              dry_run: bool = False) -> dict:
    """有料広告実績の取得。dry_run=True なら対象の列挙だけ (ログインしない)。"""
    from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
    mode = phase.split("-", 1)[1]
    out_dir = Path(out_dir) if out_dir else bf.DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(log_dir) if log_dir else (_HERE.parent / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(parallel)
    empty = _load_json(BILLING_EMPTY_PATH)   # {login_id: [週の月曜 ISO,...]} データ無し確定の週

    def pending_weeks(lid: str) -> list:
        skip = set(empty.get(lid, []))
        return [w for w in weeks if w.isoformat() not in skip
                and not bf.has_saved(out_dir, lid, w)]

    if phase == "billing-collect":
        queue = _load_json(BILLING_QUEUE_PATH)
        q_items = sorted(queue.items())
        slice_n = limit if limit else int(os.environ.get("AW_COLLECT_SLICE", "20"))
        q_slice = _rotate_slice(q_items, slice_n, out_dir, key="billing-collect")
        jobs = []
        for lid, meta in q_slice:
            try:
                wk = date.fromisoformat(meta.get("week_start", ""))
            except Exception:  # noqa: BLE001
                continue
            acc = find_account_by_login_id(lid) or {
                "login_id": lid, "company_name": meta.get("company", ""), "password": ""}
            jobs.append((acc, [wk]))
        print(f"[orchestrate] phase={phase}: キュー {len(queue)}件 → 今回 {len(jobs)}社",
              flush=True)
    else:
        accounts = _select_billing_accounts(from_active_deals, target_login_ids)
        if limit and (from_active_deals) and not target_login_ids:
            accounts = _rotate_slice(accounts, limit, out_dir, key=phase)
        elif limit:
            accounts = accounts[:limit]
        jobs = [(a, pending_weeks(a.get("login_id", ""))) for a in accounts]
        jobs = [(a, w) for a, w in jobs if w]
        print(f"[orchestrate] phase={phase}: 対象 {len(jobs)}社 / 週 "
              f"{weeks[0].isoformat() if weeks else '-'}〜{weeks[-1].isoformat() if weeks else '-'}"
              f" ({len(weeks)}週), parallel={parallel}", flush=True)

    total = len(jobs)
    if total == 0:
        print("[orchestrate] 対象なし。", flush=True)
        return {"ok": 0, "ng": 0, "total": 0, "log": None}
    if dry_run:
        print(f"[orchestrate] dry-run: {total}社を処理予定 (ログインしない)", flush=True)
        return {"ok": 0, "ng": 0, "total": total, "log": None, "dry_run": True}

    done = {"n": 0}

    async def _wrapped(acc, wk):
        r = await process_billing_one(sem, acc, wk, out_dir, headless, mode)
        done["n"] += 1
        print(f"[done {done['n']}/{total}] {r['status']} "
              f"login_id={_mask(r.get('login_id'))} "
              f"({plog.mask_name(r.get('company', ''))}) weeks={r.get('weeks', {})}",
              flush=True)
        return r

    results = await asyncio.gather(*[_wrapped(a, w) for a, w in jobs])

    # --- 状態の更新 (キュー / データ無しの週) ---
    queue = _load_json(BILLING_QUEUE_PATH)
    for r in results:
        lid = r["login_id"]
        for wk, st in (r.get("week_status") or {}).items():
            if st == "empty":
                lst = empty.setdefault(lid, [])
                if wk not in lst:
                    lst.append(wk)
        if phase == "billing-create" and r["status"] == "queued":
            first = next((w for w, s in (r.get("week_status") or {}).items()
                          if s == "queued"), "")
            queue[lid] = {"company": r.get("company", ""), "week_start": first,
                          "triggered_at": datetime.now().isoformat()}
        elif phase == "billing-collect":
            if r["status"] == "ok":
                queue.pop(lid, None)
        elif phase == "billing-full":
            queue.pop(lid, None) if r["status"] == "ok" else None
    _save_json(BILLING_EMPTY_PATH, empty)
    _save_json(BILLING_QUEUE_PATH, queue)

    summary = _write_account_results(results, out_dir, phase)
    print(f"[orchestrate] 住み分け({phase}): {summary}", flush=True)
    _slack_notify(f"🔄 AW有料広告実績({phase}) {total}社処理: "
                  + " / ".join(f"{k}={v}" for k, v in summary.items()))
    ok = sum(1 for r in results if r["status"] in ("ok", "queued"))
    ng = sum(1 for r in results if r["status"] == "error")
    nr = sum(1 for r in results if r["status"] == "not_ready")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"aw_{phase.replace('-', '_')}_{ts}.json"
    log_path.write_text(json.dumps(
        {"ok": ok, "ng": ng, "not_ready": nr,
         "results": [_sanitize(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        if r.get("status") not in ("ok", "queued"):
            plog.detail("aw_billing_result", phase=phase, login_id=r.get("login_id"),
                        company=r.get("company"), status=r.get("status"),
                        error=r.get("error", ""))
    print(f"[{phase}-done] ok={ok} ng={ng} not_ready={nr} queue残={len(queue)} "
          f"log={log_path}", flush=True)
    return {"ok": ok, "ng": ng, "not_ready": nr, "total": total, "log": str(log_path)}


def resolve_billing_weeks(week_start: str = "", frm: str = "", to: str = "",
                          today: Optional[date] = None) -> list:
    """CLI 引数 → 対象週 (月曜 date のリスト)。

    --week-start : その 1 週 (月曜でなければエラー)
    --from [--to]: その期間の完了週すべて (バックフィル)
    どちらも無し : 直近の完了週 1 週
    """
    from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
    if week_start and (frm or to):
        raise SystemExit("--week-start と --from/--to は同時に指定できません")
    if week_start:
        d = date.fromisoformat(week_start)
        if d.weekday() != 0:
            raise SystemExit("--week-start は月曜日を指定してください")
        return [d]
    if frm:
        return bf.weeks_between(date.fromisoformat(frm),
                                date.fromisoformat(to) if to else None, today)
    if to:
        raise SystemExit("--to は --from と一緒に指定してください")
    return [bf.last_full_week(today)]


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
    p.add_argument("--from-urgent-queue", action="store_true",
                   help="応募の求人が HubSpot に無かった社だけを取る (aw_urgent_queue)")
    p.add_argument("--applications-cutoff", default="",
                   help="--from-applications の応募日カットオフ(ISO YYYY-MM-DD, 空=全件)")
    p.add_argument("--phase",
                   choices=["full", "create", "collect", *BILLING_PHASES],
                   default="full",
                   help="full=生成+DL+取込(従来) / create=生成トリガーのみ(Phase1,"
                        "対象をキュー保存) / collect=キューから待機なしDL+取込(Phase2)。"
                        "クラウド課金の生成待ち時間を分離する。"
                        "billing-create/billing-collect/billing-full=有料広告実績(求人単位/週)の"
                        "取得 (HubSpot 書込なし。取込は airwork_billing_import.py)")
    p.add_argument("--week-start", default="",
                   help="[billing-*] 対象週の月曜 YYYY-MM-DD (既定: 直近の完了週)")
    p.add_argument("--from", dest="billing_from", default="",
                   help="[billing-*] バックフィル開始日 YYYY-MM-DD (その日を含む週から)")
    p.add_argument("--to", dest="billing_to", default="",
                   help="[billing-*] バックフィル終了日 (既定: 直近の完了週)")
    p.add_argument("--dry-run", action="store_true",
                   help="[billing-*] 対象の列挙だけ行い、ログインしない "
                        "(billing は HubSpot に書かないので --actual は不要)")
    p.add_argument("--billing-dir", default=None,
                   help="[billing-*] ZIP 保存先 (既定 ~/airwork-exports/billing)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    target_ids = None
    if args.target_login_ids:
        target_ids = [s.strip() for s in args.target_login_ids.split(",")
                      if s.strip()]
    if args.phase in BILLING_PHASES:
        asyncio.run(orchestrate_billing(
            args.phase,
            resolve_billing_weeks(args.week_start, args.billing_from, args.billing_to),
            parallel=args.parallel, limit=args.limit, headless=not args.headful,
            out_dir=Path(args.billing_dir) if args.billing_dir else None,
            target_login_ids=target_ids, from_active_deals=args.from_active_deals,
            dry_run=args.dry_run))
        return
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
        from_urgent_queue=args.from_urgent_queue,
    ))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    finally:
        plog.flush()
