"""AW 応募者一覧 CSV を 1顧客分自動取得.

実測仕様 (2026-07-01 実ブラウザで確定):
    1. ログイン: AirRegi SSO (ats.rct.airwork.net/airplf/login 経由)
       — aw_csv_fetcher.py と同一フロー (実装を再利用、重複実装しない)
    2. https://ats.rct.airwork.net/entries (応募者一覧) へ goto
    3. ボタン「応募者一覧をダウンロードする（上限1000件）」を click
       → 内部API GET /api/templates/entries/api/entry_archives?entryTab=all
       → ブラウザ download イベントで 応募一覧_{YYYYMMDD}_{n}.csv が落ちる
       → Playwright は expect_download で捕捉し save_as
    4. CSV 仕様: UTF-8 BOM / 59列 / 上限1000件
       col0=応募ID, col1=応募者名, col7=電話番号, col8=メールアドレス,
       col27=応募日時, col28=応募求人ID(=AW求人ID → LISTING.id_airwork突合キー),
       col36=職種名

セキュリティ:
    - PW・credentials をログ / 例外メッセージに出さない (_mask 使用)。

Usage:
    python -m job_application_sync.fetchers.aw_applicant_fetcher \
        --login-id rpo_xxxxx [--password ***] [--output DIR] [--headful]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# --- self-bootstrap: 直接実行でも scripts/ を sys.path に載せる -------------
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# SSO ログイン部品は aw_csv_fetcher の既存実装を再利用 (重複実装しない)
from job_application_sync.fetchers import aw_csv_fetcher as awf  # noqa: E402


# 実測 URL / ボタン文言 (2026-07-01)
URL_ENTRIES = "https://ats.rct.airwork.net/entries"
BTN_DOWNLOAD_PARTIAL = "応募者一覧をダウンロード"  # 部分一致 (「…する（上限1000件）」)

# CSV 検証: 1行目に含まれるべきヘッダ列名
CSV_HEADER_TOKEN = "応募ID"

# 既定保存先 (repo_root/scratchpad/csv_fetched/aw_applicants)
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "scratchpad" / "csv_fetched" / "aw_applicants"


def sanitize_login_id(login_id: str) -> str:
    """login_id をファイル名に安全な形へ (@ や . 等を _ に)."""
    return re.sub(r"[^0-9A-Za-z_-]", "_", login_id)


def validate_applicant_csv(path: Path) -> None:
    """保存済み CSV の妥当性検証.

    UTF-8(BOM) で読めて 1行目に「応募ID」を含むこと。
    含まなければ (HTML エラーページ等) RuntimeError。
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            first_line = f.readline()
    except OSError as e:
        raise RuntimeError(f"応募CSV 読み込み失敗: {path.name} ({e})") from e
    if CSV_HEADER_TOKEN not in first_line:
        raise RuntimeError(
            f"応募CSV 検証失敗: 1行目に「{CSV_HEADER_TOKEN}」が無い "
            f"(HTMLエラーページ等の可能性): {path.name}"
        )


async def _click_button_by_partial_text(page, text: str) -> bool:
    """text 部分一致の button / a を click. locator → JS click fallback."""
    # 1) Playwright locator (has-text は部分一致)
    try:
        loc = page.locator(f"button:has-text('{text}')").first
        if await loc.count() and await loc.is_visible():
            await loc.click()
            return True
    except Exception:
        pass
    # 2) JS click fallback
    try:
        clicked = await page.evaluate(
            """(label) => {
                const els = Array.from(document.querySelectorAll('button, a'));
                const hit = els.find(el => (el.textContent || '').includes(label));
                if (hit) { hit.click(); return true; }
                return false;
            }""",
            text,
        )
        return bool(clicked)
    except Exception:
        return False


async def fetch_aw_applicants(
    login_id: str,
    password: str,
    output_dir: Path,
    headless: bool = True,
    storage_state_path: Optional[Path] = None,
) -> Path:
    """AW 1顧客の応募者一覧 CSV を取得して output_dir に保存.

    Args:
        login_id: AirWork ID
        password: AirワークPW (ログ出力厳禁)
        output_dir: 保存先 (存在しなければ作成)
        headless: True ならヘッドレス
        storage_state_path: セッション再利用先 (BAN対策)。指定時は保存Cookieを
            再利用しPWフルログインの反復を避ける (切れたら再ログイン→保存)。

    Returns:
        保存した .csv のパス

    Raises:
        RuntimeError: ログイン失敗 / ボタン未検出 / DL失敗 / CSV検証失敗
    """
    from playwright.async_api import async_playwright  # 遅延 import

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        # セッション再利用でログイン (BAN対策)。storage_state 未指定なら毎回 full login。
        ctx, page = await awf.establish_aw_session(
            browser, login_id, password,
            storage_state_path=storage_state_path, output_dir=output_dir,
        )
        try:
            # 1) ログインは establish_aw_session で完了済 (セッション再利用/再ログイン)
            # 2) 応募者一覧
            await page.goto(URL_ENTRIES, wait_until="networkidle")

            # 3) DLボタン click → download イベント捕捉
            try:
                async with page.expect_download(timeout=60_000) as dl_info:
                    ok = await _click_button_by_partial_text(
                        page, BTN_DOWNLOAD_PARTIAL
                    )
                    if not ok:
                        raise RuntimeError(
                            f"「{BTN_DOWNLOAD_PARTIAL}」ボタン未検出 "
                            f"(login_id={login_id})"
                        )
                dl = await dl_info.value
            except RuntimeError:
                await awf._dump_debug(page, output_dir, "entries_btn_missing")
                raise
            except Exception:
                await awf._dump_debug(page, output_dir, "download_timeout")
                raise RuntimeError(
                    f"応募CSV download イベント 60秒以内に発生せず "
                    f"(login_id={login_id})"
                )

            # 4) 保存 + 検証
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = (
                output_dir
                / f"aw_applicants_{sanitize_login_id(login_id)}_{ts}.csv"
            )
            await dl.save_as(str(out_path))
            validate_applicant_csv(out_path)
            return out_path
        finally:
            try:
                await ctx.close()
            finally:
                await browser.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AW 応募者一覧 CSV 自動取得")
    ap.add_argument("--login-id", required=True, help="AirWork ID")
    ap.add_argument(
        "--password",
        help="未指定なら Sheets「アカウント情報」から login-id で引く",
    )
    ap.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"保存先ディレクトリ (default: {DEFAULT_OUTPUT_DIR})",
    )
    ap.add_argument(
        "--headful",
        action="store_true",
        help="ブラウザを表示して実行 (デバッグ用)",
    )
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()
    # PW 解決は aw_csv_fetcher の既存実装を再利用
    pw = awf._resolve_password(args.login_id, args.password)
    out_path = asyncio.run(
        fetch_aw_applicants(
            login_id=args.login_id,
            password=pw,
            output_dir=Path(args.output),
            headless=not args.headful,
        )
    )
    # PW はログに出さない
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
