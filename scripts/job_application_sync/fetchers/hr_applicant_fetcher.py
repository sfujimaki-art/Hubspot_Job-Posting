"""HRハッカー 応募者CSV 自動取得 — Playwright Python async (同期fetch方式).

実測仕様 (2026-07-01 実ブラウザで確定):
- ページ: https://hr-hacker.com/admin/applicants (インビジョンSSO要)
- 求人CSVと異なり履歴ポーリング不要。ログイン済みセッションで
  GET /admin/applicants?...&csv-download=1 に日付範囲を付けると
  Content-Disposition: attachment; filename="applicants.csv" が即返る (同期fetch)
- 全件 (日付絞りなし) は 500 エラー → search_date_from / search_date_end 必須
- エンコーディング: Shift-JIS / 33列
- 主要列: col0=応募者id, col1=応募求人先(=HR求人ID → LISTING.id_hrhakkaa 突合キー),
  col2=選考ステータス, col4=名前, col9=電話番号, col10=メールアドレス,
  col29=応募日時, col31=店舗ID

SSO は hr_csv_fetcher の既存ヘルパ (_establish_session / classify_sso_url /
_wait_for_admin / storage_state 復元 / リトライ3回) を再利用する。重複実装しない。

CSV 取得は page.request.get (APIRequestContext, cookie 共有) で直接 GET →
bytes をそのまま保存 (Shift-JIS 維持)。保存後に先頭行を Shift-JIS デコードして
「応募者id」を含むことを検証 (500/HTML エラーページ検知)。

使い方:
    python -m scripts.job_application_sync.fetchers.hr_applicant_fetcher \
        --date-from 2026-06-30 --date-to 2026-07-03 --headful
"""
from __future__ import annotations

# self-bootstrap: タスクスケジューラ等から直接実行しても
# `from scripts.job_application_sync.*` が解決できるように repo root を sys.path に追加
import sys as _sys
import pathlib as _pathlib
_REPO_ROOT = str(_pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

import argparse
import asyncio
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

try:  # パッケージ経由 (job_application_sync.fetchers / scripts.job_application_sync.fetchers)
    from . import hr_csv_fetcher as _hrf
except ImportError:  # 直接実行 (python hr_applicant_fetcher.py)
    from scripts.job_application_sync.fetchers import hr_csv_fetcher as _hrf

# ----------------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------------
APPLICANTS_URL = "https://hr-hacker.com/admin/applicants"
CSV_ENCODING = "cp932"  # Shift-JIS (Windows拡張)
HEADER_REQUIRED_TOKEN = "応募者id"
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_OUTPUT_DIR = (
    _pathlib.Path(_REPO_ROOT) / "scratchpad" / "csv_fetched" / "hr_applicants"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------
# 純関数: URL 組立 / CSV 検証
# ----------------------------------------------------------------------------
def build_applicant_csv_url(date_from: str, date_to: str, limit: int = 20) -> str:
    """応募者CSV同期fetch用 URL を組み立てる純関数.

    ★日付絞りなしは 500 エラー (2026-07-01 実測) のため date_from/date_to 必須。
    """
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if not value or not _DATE_RE.match(value):
            raise ValueError(
                f"{label} は YYYY-MM-DD 形式必須 (got={value!r})。"
                "日付絞りなしはサーバ側 500 エラーになる"
            )
    params = [
        ("search_branches_id", ""),
        ("search_date_from", date_from),
        ("search_date_end", date_to),
        ("search_jobCategory_id", ""),
        ("search_selection_id", ""),
        ("search_freeword", ""),
        ("limit", str(limit)),
        ("csv-download", "1"),
    ]
    return f"{APPLICANTS_URL}?{urlencode(params)}"


def validate_applicant_csv(data: bytes) -> None:
    """取得 bytes が応募者CSVであることを検証する (だめなら RuntimeError).

    - 空 → RuntimeError
    - HTML (エラーページ/ログイン画面) → RuntimeError
    - 先頭行 (Shift-JIS デコード) に「応募者id」を含まない → RuntimeError
    """
    if not data or not data.strip():
        raise RuntimeError("応募者CSVが空です (0 bytes) — 取得失敗")

    head = data.lstrip()[:512]
    lowered = head.lower()
    if lowered.startswith(b"<!doctype") or lowered.startswith(b"<html") or b"<html" in lowered:
        raise RuntimeError(
            "応募者CSVの代わりに HTML が返りました (500エラー/セッション失効の可能性)"
        )

    first_line = data.split(b"\n", 1)[0]
    try:
        decoded = first_line.decode(CSV_ENCODING, errors="replace")
    except Exception as exc:  # pragma: no cover — replace指定で通常到達しない
        raise RuntimeError(f"応募者CSV先頭行の Shift-JIS デコード失敗: {exc}") from exc
    if HEADER_REQUIRED_TOKEN not in decoded:
        raise RuntimeError(
            f"応募者CSVヘッダに「{HEADER_REQUIRED_TOKEN}」が見つかりません "
            f"(先頭行={decoded[:120]!r})"
        )


def default_date_range(today: Optional[date] = None) -> tuple[str, str]:
    """デフォルト取得範囲 = 直近3日 (today-3 〜 today)."""
    today = today or date.today()
    return (
        (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat(),
        today.isoformat(),
    )


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
async def fetch_hr_applicants(
    output_dir: Path,
    date_from: str,
    date_to: str,
    headless: bool = True,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Path:
    """HRハッカー応募者CSVを同期fetchで取得し output_dir に保存、パスを返す.

    Args:
        output_dir: 保存先ディレクトリ (なければ作成)
        date_from:  応募日 開始 (YYYY-MM-DD, 必須 — 絞りなしは 500)
        date_to:    応募日 終了 (YYYY-MM-DD, 必須)
        headless:   True=非表示Chromium / False=デバッグ表示
        user, password: 明示指定がなければ環境変数 HRHACKER_USER/PASS から取得

    Returns:
        保存した CSV ファイルパス (Shift-JIS bytes そのまま保存)
    """
    url = build_applicant_csv_url(date_from, date_to)  # 先に検証 (fail fast)

    user = user or os.environ.get("HRHACKER_USER", "")
    password = password or os.environ.get("HRHACKER_PASS", "")
    if not user or not password:
        raise RuntimeError(
            "HRHACKER_USER / HRHACKER_PASS が未設定 (.env に追記して再実行してください)"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # playwright は遅延 import (mockテスト時の依存を避ける)
    from playwright.async_api import async_playwright  # noqa: WPS433

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = None
        try:
            # 1. セッション確立 (hr_csv_fetcher の既存ヘルパ再利用:
            #    storage_state 復元 → だめなら SSO 最大3回リトライ)
            ctx, page = await _hrf._establish_session(browser, user, password)

            # 2. 同期fetch: page.request.get (cookie共有 APIRequestContext)
            resp = await page.request.get(url, timeout=120_000)
            if resp.status != 200:
                raise RuntimeError(
                    f"応募者CSV取得失敗 status={resp.status} "
                    f"(date_from={date_from}, date_to={date_to})"
                )
            body = await resp.body()

            # 3. 検証 (HTML/空 → RuntimeError)
            validate_applicant_csv(body)

            # 4. 保存 (bytes そのまま = Shift-JIS 維持)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = output_dir / f"hr_applicants_{date_from}_{date_to}_{ts}.csv"
            out_path.write_bytes(body)
            print(f"[hr_applicants] 保存: {out_path} ({len(body):,} bytes)")
            return out_path
        finally:
            if ctx is not None:
                await ctx.close()
            await browser.close()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    df_default, dt_default = default_date_range()
    ap = argparse.ArgumentParser(description="HRハッカー 応募者CSV 自動取得 (同期fetch)")
    ap.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"保存先ディレクトリ (default: {DEFAULT_OUTPUT_DIR})",
    )
    ap.add_argument(
        "--date-from",
        default=df_default,
        help=f"応募日 開始 YYYY-MM-DD (default: 直近{DEFAULT_LOOKBACK_DAYS}日={df_default})",
    )
    ap.add_argument(
        "--date-to",
        default=dt_default,
        help=f"応募日 終了 YYYY-MM-DD (default: 本日={dt_default})",
    )
    ap.add_argument("--headless", dest="headless", action="store_true", default=True)
    ap.add_argument("--headful", dest="headless", action="store_false")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    path = asyncio.run(
        fetch_hr_applicants(
            output_dir=Path(args.output),
            date_from=args.date_from,
            date_to=args.date_to,
            headless=args.headless,
        )
    )
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
