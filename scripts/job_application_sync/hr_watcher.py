"""HRハッカー求人 日次差分検知 → AW顧客ピンポイント連動エンジン.

設計準拠:
- v0.3 要件定義 W-1〜W-5
- v0.2 §11 HRハッカー取込ロジック
- 店舗ID基準 (Association経由ではない) で Deal特定

================================================================================
フロー (W-1〜W-5)
================================================================================
W-1: HR CSV取得 (hr_csv_fetcher) もしくは --csv で明示
W-2: 前回スナップショット (data/job_application_sync/hr_snapshots/<日付>.json.gz)
     と差分検出 (new / removed / changed)
W-3: 差分0件 → AW巡回スキップして終了
W-4: 差分あり →
     a. A-1 hrhacker_import.run() で HubSpot LISTING更新
     b. 差分求人の id_shop_hrhakkaa 集合を抽出
     c. Deal Search (pipeline=リクロジ_納品管理 AND hrhacker_shop_ids CONTAINS_TOKEN <sid>)
     d. Deal 経由で会社識別子 (login_id 候補) を集約
     e. account_loader.find_account_by_login_id で AW credentials lookup
W-5: aw_orchestrator(target_login_ids=[...]) でピンポイント実行
================================================================================

スナップショット形式 (gzip JSON):
    {
      "fetched_at": "2026-06-30T10:00:00",
      "source_csv": "hr_offers_all_20260630_100000.csv",
      "jobs": {
        "<media_job_id>": {
          "status": "公開" | "非公開" | "公開開始前" | "公開終了",
          "shop_id": "<store_id>",
          "job_name": "..."
        },
        ...
      }
    }

CLI:
    python -m scripts.job_application_sync.hr_watcher
        [--csv <path>]              # 省略時は hr_csv_fetcher 自動取得
        [--input-mapping-json PATH] # 外部CSVソース指定 (テスト/再実行用)
        [--dry-run | --actual]      # 既定 dry-run (A-1/AW実行スキップ)
        [--skip-aw]                 # HR反映のみ、AW連動スキップ
        [--max-customers N]         # AW顧客上限 (既定 50)
        [--snapshot-dir PATH]       # スナップショット保存先
"""
from __future__ import annotations

# self-bootstrap: タスクスケジューラ等から直接実行しても
# `from scripts.job_application_sync.*` が解決できるように repo root を sys.path に追加
import sys as _sys
import pathlib as _pathlib
_REPO_ROOT = str(_pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

import argparse
import asyncio
import gzip
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

# ----------------------------------------------------------------------------
# パス設定
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
PKG_DIR = HERE                              # scripts/job_application_sync
REPO = HERE.parent.parent                   # repository root
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
DEFAULT_SNAPSHOT_DIR = REPO / "data" / "job_application_sync" / "hr_snapshots"

# ----------------------------------------------------------------------------
# 定数 (HubSpot)
# ----------------------------------------------------------------------------
BASE = "https://api.hubapi.com"
# 納品管理_リクロジ pipeline (backfill_deal_shop_id.py 準拠)
PIPELINE_NOUHIN_RIKUROJI = "21596025"
PROP_DEAL_SHOP_IDS = "hrhacker_shop_ids"

# AW顧客上限 (1ジョブで巡回する最大社数 — 暴走防止)
DEFAULT_MAX_CUSTOMERS = 50


# ============================================================================
# 1. 差分検出 (純関数 — ユニットテスト主対象)
# ============================================================================
def detect_diff(prev: dict, curr: dict) -> dict:
    """前回スナップショット vs 今回スナップショットの差分検出.

    Args:
        prev: {job_id: {status, shop_id, ...}} 前回
        curr: {job_id: {status, shop_id, ...}} 今回

    Returns:
        {"new": [job_id, ...], "removed": [job_id, ...], "changed": [job_id, ...]}
        - new: 前回には無く今回ある求人 (新規掲載)
        - removed: 前回はあったが今回CSVに居ない求人 (掲載終了相当)
        - changed: 両方に居るが status または タイトル(job_name) が変化した求人
          (2026-07-10: 媒体SSOTでタイトル改定をHubSpotへ追従させるため、status
           だけでなく job_name の変化も差分に含める。これが無いと「タイトルだけ
           変更した日」に hi.run() が発火せず更新が遅延する)

    注意:
        v0.2 §24 第3条「HRはCSV未検出を公開終了と判断しない」を尊重する。
        本関数は「差分検出」までを行い、HubSpot側でのステータス変更は
        呼出側 (hrhacker_import) に委ねる。
    """
    prev_ids = set(prev.keys())
    curr_ids = set(curr.keys())
    new = sorted(curr_ids - prev_ids)
    removed = sorted(prev_ids - curr_ids)

    def _job_changed(jid: str) -> bool:
        p = prev.get(jid) or {}
        c = curr.get(jid) or {}
        return (p.get("status") != c.get("status")
                or p.get("job_name") != c.get("job_name"))

    changed = sorted(jid for jid in (prev_ids & curr_ids) if _job_changed(jid))
    return {"new": new, "removed": removed, "changed": changed}


def diff_is_empty(diff: dict) -> bool:
    """差分が完全に空か判定."""
    return not (diff.get("new") or diff.get("removed") or diff.get("changed"))


# ============================================================================
# 2. スナップショット I/O
# ============================================================================
def _snapshot_path(snapshot_dir: Path, date_str: Optional[str] = None) -> Path:
    """指定日 (既定: 今日) のスナップショットパスを返す."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return Path(snapshot_dir) / f"{date_str}.json.gz"


def load_latest_snapshot(snapshot_dir: Path) -> dict:
    """snapshot_dir 内で最も新しい *.json.gz を読み込んで返す.

    Returns:
        {"fetched_at": ..., "source_csv": ..., "jobs": {...}}
        ファイルが無ければ空辞書 {} (初回扱い)。
    """
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return {}
    files = sorted(snapshot_dir.glob("*.json.gz"))
    if not files:
        return {}
    latest = files[-1]
    with gzip.open(latest, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(snapshot_dir: Path, snapshot: dict,
                  date_str: Optional[str] = None) -> Path:
    """スナップショットを gzip JSON で保存."""
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(snapshot_dir, date_str)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return path


def csv_rows_to_snapshot(rows: list[dict], source_csv: str) -> dict:
    """hrhacker_import.load_hr_csv の戻り値をスナップショット辞書に変換.

    Args:
        rows: [{"media_job_id", "shop_id", "job_name", "original_status", ...}, ...]
        source_csv: 元CSVファイル名 (記録用)

    Returns:
        {"fetched_at", "source_csv", "jobs": {job_id: {status, shop_id, job_name}}}
    """
    jobs: dict[str, dict] = {}
    for r in rows:
        jid = (r.get("media_job_id") or "").strip()
        if not jid:
            continue
        jobs[jid] = {
            "status": (r.get("original_status") or "").strip(),
            "shop_id": (r.get("shop_id") or "").strip(),
            "job_name": (r.get("job_name") or "").strip(),
        }
    return {
        "fetched_at": datetime.now().isoformat(),
        "source_csv": source_csv,
        "jobs": jobs,
    }


# ============================================================================
# 3. 差分求人 → 影響を受ける店舗ID集合
# ============================================================================
def extract_affected_shop_ids(curr_snapshot: dict, prev_snapshot: dict,
                              diff: dict) -> set[str]:
    """差分求人 (new/removed/changed) の shop_id を集約.

    new/changed は curr_snapshot から、removed は prev_snapshot から取得。
    空文字列・None は除外。
    """
    shop_ids: set[str] = set()
    curr_jobs = curr_snapshot.get("jobs", {}) or {}
    prev_jobs = prev_snapshot.get("jobs", {}) or {}
    for jid in diff.get("new", []) + diff.get("changed", []):
        sid = (curr_jobs.get(jid) or {}).get("shop_id", "")
        if sid:
            shop_ids.add(sid.strip())
    for jid in diff.get("removed", []):
        sid = (prev_jobs.get(jid) or {}).get("shop_id", "")
        if sid:
            shop_ids.add(sid.strip())
    shop_ids.discard("")
    return shop_ids


# ============================================================================
# 4. 影響を受けた Deal の特定 (店舗ID → Deal Search)
# ============================================================================
def find_deals_by_shop_ids(shop_ids: Iterable[str],
                            headers_fn=None) -> list[dict]:
    """Deal (pipeline=リクロジ_納品管理 AND hrhacker_shop_ids CONTAINS_TOKEN sid).

    1 shop_id ごとに 1 Search 呼出 (CONTAINS_TOKEN は OR配列が使えない)。
    Returns:
        [{"id", "properties": {"dealname", "hrhacker_shop_ids", ...}}, ...]
        重複Dealは Deal.id でユニーク化。
    """
    import requests  # type: ignore  # 遅延 import (テスト時に依存しない)
    if headers_fn is None:
        token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
    else:
        headers = headers_fn()

    found: dict[str, dict] = {}
    for sid in shop_ids:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hrhacker_shop_ids",
                 "operator": "CONTAINS_TOKEN", "value": sid},
                {"propertyName": "pipeline",
                 "operator": "EQ", "value": PIPELINE_NOUHIN_RIKUROJI},
            ]}],
            "properties": ["dealname", "hrhacker_shop_ids", "pipeline",
                           "airwork_account_login_id"],
            "limit": 100,
        }
        r = requests.post(f"{BASE}/crm/v3/objects/0-3/search",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        for o in r.json().get("results", []):
            found[o["id"]] = o
        time.sleep(0.1)
    return list(found.values())


# ============================================================================
# 5. Deal → AW login_id 候補集合
# ============================================================================
def extract_login_id_candidates(deals: list[dict]) -> list[str]:
    """Deal リストから AW login_id (airwork_account_login_id) を抽出.

    現状: Deal の airwork_account_login_id プロパティに login_id が
    入っている前提 (将来は Company.airwork_login_id 等にも対応拡張可能)。
    重複除去・空除去。
    """
    seen: list[str] = []
    s: set[str] = set()
    for d in deals:
        props = d.get("properties") or {}
        lid = (props.get("airwork_account_login_id") or "").strip()
        if lid and lid not in s:
            s.add(lid)
            seen.append(lid)
    return seen


def resolve_accounts(login_ids: list[str],
                     account_finder=None) -> list[dict]:
    """login_id 集合 → AW credentials (account_loader.find_account_by_login_id).

    Args:
        login_ids: AW SSO email 候補
        account_finder: 注入用 (None なら本物の account_loader を呼ぶ)

    Returns:
        [{"company_name", "login_id", "password", "source"}, ...] (見つかったもののみ)
    """
    if account_finder is None:
        from scripts.job_application_sync.fetchers.account_loader import (  # noqa: E402
            find_account_by_login_id,
        )
        account_finder = find_account_by_login_id

    out: list[dict] = []
    for lid in login_ids:
        acc = account_finder(lid)
        if acc:
            out.append(acc)
    return out


# ============================================================================
# 6. メイン オーケストレーション
# ============================================================================
def run(csv_path: Optional[str] = None,
        *,
        dry_run: bool = True,
        skip_aw: bool = False,
        max_customers: int = DEFAULT_MAX_CUSTOMERS,
        snapshot_dir: Optional[Path] = None,
        input_mapping_json: Optional[str] = None,
        # --- 依存注入 (テスト容易性) ---
        csv_fetcher=None,           # 引数なし → CSVパス文字列を返す callable
        load_csv_fn=None,           # path → list[dict] (hrhacker_import.load_hr_csv 互換)
        hrhacker_run_fn=None,       # (csv_path, dry_run) → summary dict
        deal_finder=None,           # shop_ids → deals list (find_deals_by_shop_ids 互換)
        account_finder=None,        # login_id → account dict or None
        aw_orchestrate_fn=None,     # (target_login_ids, dry_run, ...) → result dict
        ) -> dict:
    """hr_watcher メイン.

    Returns:
        {
          "csv_path", "diff", "diff_count",
          "skipped_aw": bool, "affected_shop_ids": [...],
          "matched_deals": int, "target_login_ids": [...],
          "resolved_accounts": int, "aw_result": {...} | None,
          "snapshot_path": str, "log_path": str
        }
    """
    snapshot_dir = Path(snapshot_dir) if snapshot_dir else DEFAULT_SNAPSHOT_DIR
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "skip_aw": skip_aw,
        "started_at": datetime.now().isoformat(),
    }

    # ---- W-1: CSV 取得 ----
    if input_mapping_json:
        # 外部 mapping JSON 経由: {"csv_path": "...", "rows": [...]}
        # rows がある場合は CSV を読み直さず直接使う (テスト/再実行用)
        with open(input_mapping_json, encoding="utf-8") as f:
            mapping = json.load(f)
        csv_path = csv_path or mapping.get("csv_path") or input_mapping_json
        rows = mapping.get("rows")
    else:
        rows = None

    if csv_path is None and rows is None:
        if csv_fetcher is None:
            # 実取得: hr_csv_fetcher を呼ぶ
            from scripts.job_application_sync.fetchers.hr_csv_fetcher import (  # noqa: E402
                fetch_hr_csv,
            )
            fetched = asyncio.run(fetch_hr_csv(
                output_dir=REPO / "scratchpad" / "csv_fetched" / "hr",
                is_valid="",
            ))
            csv_path = str(fetched)
        else:
            csv_path = csv_fetcher()
    summary["csv_path"] = csv_path

    # ---- CSV読込 (rows 注入優先) ----
    if rows is None:
        if load_csv_fn is None:
            from scripts.job_application_sync import hrhacker_import as hi  # noqa: E402
            load_csv_fn = hi.load_hr_csv
        rows = load_csv_fn(csv_path)
    summary["csv_rows"] = len(rows)

    # ---- W-2: 前回スナップショット vs 今回 ----
    prev = load_latest_snapshot(snapshot_dir)
    curr = csv_rows_to_snapshot(rows, source_csv=os.path.basename(str(csv_path)))
    diff = detect_diff(prev.get("jobs", {}), curr.get("jobs", {}))
    summary["diff"] = {k: len(v) for k, v in diff.items()}
    summary["prev_snapshot_jobs"] = len(prev.get("jobs", {}))
    summary["curr_snapshot_jobs"] = len(curr.get("jobs", {}))

    # 今回スナップショット保存 (差分0でも保存して次回の基準にする)
    snap_path = save_snapshot(snapshot_dir, curr)
    summary["snapshot_path"] = str(snap_path)

    # ---- W-3: 差分0件 → 終了 ----
    if diff_is_empty(diff):
        summary["skipped_aw"] = True
        summary["skip_reason"] = "no_diff"
        log_path = _write_log(summary, "no_diff")
        summary["log_path"] = str(log_path)
        print(f"[hr_watcher] 差分0件 → AW巡回スキップ. snapshot={snap_path}")
        return summary

    print(f"[hr_watcher] 差分検出: new={len(diff['new'])} "
          f"removed={len(diff['removed'])} changed={len(diff['changed'])}")

    # ---- 安全ガード: snapshot欠損(初回)での大量newのactual暴発を防止 ----
    # snapshotがOneDrive revert/誤削除で消えると、全求人がnew扱いになり
    # A-1が28,000件超をactual反映+全AW顧客巡回する暴発リスク (レビュー2026-07-02 HIGH)
    MASS_WRITE_THRESHOLD = 500
    is_first_run = len(prev.get("jobs", {})) == 0
    if not dry_run and is_first_run and len(diff["new"]) > MASS_WRITE_THRESHOLD:
        summary["aborted"] = True
        summary["abort_reason"] = (
            f"first_run_mass_write_guard: snapshot欠損 かつ new={len(diff['new'])} "
            f"> 閾値{MASS_WRITE_THRESHOLD}。初回全件actualの暴発を防止して中断"
        )
        log_path = _write_log(summary, "aborted_mass_write_guard")
        summary["log_path"] = str(log_path)
        print(f"[hr_watcher] ★中断: snapshot欠損で new={len(diff['new'])}件。"
              f"actual暴発防止のため停止 (dry-runで内容確認を). log={log_path}")
        return summary

    # ---- W-4-a: A-1 LISTING 更新 ----
    if hrhacker_run_fn is None:
        from scripts.job_application_sync import hrhacker_import as hi  # noqa: E402
        hrhacker_run_fn = hi.run
    try:
        hr_summary = hrhacker_run_fn(csv_path, dry_run=dry_run)
        summary["hrhacker_summary"] = hr_summary
    except Exception as e:
        summary["hrhacker_error"] = str(e)
        print(f"[hr_watcher] ⚠️ A-1 失敗: {e}")

    # ---- W-4-b: 影響店舗ID集合 ----
    affected_shop_ids = extract_affected_shop_ids(curr, prev, diff)
    summary["affected_shop_ids"] = sorted(affected_shop_ids)
    print(f"[hr_watcher] 影響店舗ID: {len(affected_shop_ids)} 個")

    if skip_aw:
        summary["skipped_aw"] = True
        summary["skip_reason"] = "skip_aw_flag"
        log_path = _write_log(summary, "skipped_aw_flag")
        summary["log_path"] = str(log_path)
        return summary

    if not affected_shop_ids:
        summary["skipped_aw"] = True
        summary["skip_reason"] = "no_shop_id_in_diff"
        log_path = _write_log(summary, "no_shop_id")
        summary["log_path"] = str(log_path)
        return summary

    # ---- W-4-c: Deal Search ----
    # 注入 mock があれば常に使う。本物 (find_deals_by_shop_ids) は
    # dry_run + token 無し時はスキップする (HubSpot API 呼出回避)。
    deal_finder_is_mock = deal_finder is not None
    if deal_finder is None:
        deal_finder = find_deals_by_shop_ids
    if (not deal_finder_is_mock and dry_run
            and os.environ.get("HUBSPOT_ACCESS_TOKEN", "") == ""):
        deals: list[dict] = []
        summary["deal_search_skipped"] = "dry_run_no_token"
    else:
        try:
            deals = deal_finder(affected_shop_ids)
        except Exception as e:
            deals = []
            summary["deal_search_error"] = str(e)
            print(f"[hr_watcher] ⚠️ Deal Search 失敗: {e}")
    summary["matched_deals"] = len(deals)

    # ---- W-4-d/e: login_id 候補 → account 解決 ----
    candidates = extract_login_id_candidates(deals)
    summary["target_login_ids"] = candidates

    if not candidates:
        summary["skipped_aw"] = True
        summary["skip_reason"] = "no_login_id_candidate"
        log_path = _write_log(summary, "no_login_id")
        summary["log_path"] = str(log_path)
        return summary

    # max_customers で抑制
    if len(candidates) > max_customers:
        print(f"[hr_watcher] 候補 {len(candidates)} > max={max_customers} → 先頭のみ採用")
        candidates = candidates[:max_customers]
        summary["target_login_ids_capped"] = True

    try:
        accounts = resolve_accounts(candidates, account_finder=account_finder)
    except Exception as e:
        accounts = []
        summary["account_resolve_error"] = str(e)
    summary["resolved_accounts"] = len(accounts)

    if not accounts:
        summary["skipped_aw"] = True
        summary["skip_reason"] = "no_resolved_account"
        log_path = _write_log(summary, "no_resolved_account")
        summary["log_path"] = str(log_path)
        return summary

    # ---- W-5: aw_orchestrator ピンポイント実行 ----
    if aw_orchestrate_fn is None:
        from scripts.job_application_sync.fetchers.aw_orchestrator import (  # noqa: E402
            orchestrate,
        )

        def _run_orch(target_login_ids: list[str], dry_run: bool):
            # 既存 orchestrate は target_login_ids 引数を持たない。
            # ここでは accounts を Python 内で limit するため、
            # account_loader.iter_aw_accounts の戻りを login_id で一致フィルタする
            # ラッパとして呼ぶ。orchestrate(limit=N) を併用して max を制御。
            # (orchestrate 本体に --target-login-ids を追加する PR は別タスク)
            from scripts.job_application_sync.fetchers import account_loader as _al
            original_iter = _al.iter_aw_accounts

            def filtered_iter(active_only: bool = True, prefer: str = "A",
                              **kw):
                for acc in original_iter(active_only=active_only,
                                         prefer=prefer, **kw):
                    if acc.get("login_id") in set(target_login_ids):
                        yield acc
            _al.iter_aw_accounts = filtered_iter  # monkey patch
            try:
                return asyncio.run(orchestrate(
                    parallel=min(5, len(target_login_ids)),
                    limit=len(target_login_ids),
                    dry_run=dry_run,
                ))
            finally:
                _al.iter_aw_accounts = original_iter
        aw_orchestrate_fn = _run_orch

    try:
        aw_result = aw_orchestrate_fn(candidates, dry_run)
        summary["aw_result"] = aw_result
    except Exception as e:
        summary["aw_error"] = str(e)
        print(f"[hr_watcher] ⚠️ AW orchestrate 失敗: {e}")

    summary["finished_at"] = datetime.now().isoformat()
    log_path = _write_log(summary, "completed")
    summary["log_path"] = str(log_path)
    return summary


# ============================================================================
# 7. ログ
# ============================================================================
def _write_log(summary: dict, marker: str) -> Path:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = LOG_DIR / f"hr_watcher_{marker}_{ts}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


# ============================================================================
# CLI
# ============================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HRハッカー求人 日次差分検知 → AW顧客ピンポイント連動"
    )
    p.add_argument("--csv", default=None,
                   help="HR CSV パス (省略時は hr_csv_fetcher 自動取得)")
    p.add_argument("--input-mapping-json", default=None,
                   help="外部CSVソース指定 ({csv_path, rows: [...]})")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="(既定) 計画のみ. A-1/AW は dry_run で実行")
    g.add_argument("--actual", action="store_true",
                   help="本番実行 (HubSpot/AW に書込)")
    p.add_argument("--skip-aw", action="store_true",
                   help="HR反映のみ. AW orchestrate をスキップ")
    p.add_argument("--max-customers", type=int, default=DEFAULT_MAX_CUSTOMERS,
                   help=f"AW顧客上限 (既定 {DEFAULT_MAX_CUSTOMERS})")
    p.add_argument("--snapshot-dir", default=None,
                   help=f"スナップショット保存先 (既定 {DEFAULT_SNAPSHOT_DIR})")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    dry_run = not args.actual
    snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else None
    result = run(
        csv_path=args.csv,
        dry_run=dry_run,
        skip_aw=args.skip_aw,
        max_customers=args.max_customers,
        snapshot_dir=snapshot_dir,
        input_mapping_json=args.input_mapping_json,
    )
    print(f"\n[hr_watcher-done] {json.dumps({k: v for k, v in result.items() if k != 'hrhacker_summary'}, ensure_ascii=False, indent=2)[:600]}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
