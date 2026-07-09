"""HRハッカー (hr-hacker.com) CSV 自動取得 — Playwright Python async.

設計準拠:
- Phase 0b 実測 (docs/wbs_outputs/1.11.9_媒体CSV同期実装/Phase0b_HR編_完了報告_2026-06-26.md)
- Phase 0b Chrome実操作手順 (同ディレクトリ Phase0b_Chrome実操作手順.md)

動線:
    1. https://hr-hacker.com/admin → インビジョンSSO (accounts-invision.com) にリダイレクト
    2. SSO で user / password 入力 → submit → /admin/dashboards に遷移
    3. /admin/job-offers?is_valid=<filter> で一覧表示
    4. #js-search-form の [name=csv-download] を "1" にして submit → CSV生成キック
    5. /admin/job-offers/csv-export-list を 15秒ごとにポーリング (最大 timeout_min 分)
    6. 「処理終了日時」が埋まり download-csv リンクが現れたらクリックして DL

認証情報 (環境変数必須、.env に追加):
    HRHACKER_USER : 例 rpo.medica@gmail.com
    HRHACKER_PASS : ログインパスワード

使い方:
    python -m job_application_sync.fetchers.hr_csv_fetcher --output scratchpad/csv_fetched/hr --headful

注意:
- インビジョンSSOのフィールド名は実測未確定なので、placeholder/type/name の複数候補で柔軟特定する
- Onboarding モーダル (.g-modal-pos, .g-shape) が出たら remove で除去
- storage_state 再利用対応 (2026-07-03):
    data/job_application_sync/hr_storage_state.json にログイン成功時のセッションを保存し、
    次回起動時は SSO をスキップしてダッシュボードへ直接 goto する。
    セッション失効時 (ログイン画面へリダイレクト) は通常SSOへフォールバック。
- SSO 到達確認は 'load' イベントを要求しない (2026-07-03 根治):
    重いダッシュボード (analytics 等) で 'load' が 45s 内に完了せず TimeoutError になるため、
    domcontentloaded + page.url ポーリングで /admin/ 配下到達のみ判定する。
- SSO 一連 (goto→入力→submit→到達確認) は最大3回リトライ (失敗時30s待機、context作り直し)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 未導入時のフォールバック
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

# ----------------------------------------------------------------------------
# 環境設定
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
_ENV_PATH = REPO / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

LOGIN_URL = "https://hr-hacker.com/admin"
DASHBOARD_URL = "https://hr-hacker.com/admin/dashboards/"
JOB_OFFERS_URL = "https://hr-hacker.com/admin/job-offers"
EXPORT_LIST_URL = "https://hr-hacker.com/admin/job-offers/csv-export-list"

# storage_state 保存先 (認証Cookie含む — .gitignore 済み)
STORAGE_STATE_PATH = REPO / "data" / "job_application_sync" / "hr_storage_state.json"

# SSO リトライ設定
SSO_MAX_ATTEMPTS = 3
SSO_RETRY_WAIT_S = 30.0
# /admin/ 配下到達ポーリング (SSO submit 後)
ADMIN_REACH_TIMEOUT_S = 60.0
ADMIN_REACH_POLL_S = 2.0

# SSO ログインフィールド候補 (placeholder/type/name で複数候補をチェック)
USER_FIELD_SELECTORS = [
    "input[type='email']",
    "input[name='user']",
    "input[name='email']",
    "input[name='login_id']",
    "input[name='loginId']",
    "input[name='userId']",
    "input[name='username']",
    "input[id*='email' i]",
    "input[id*='user' i]",
    "input[id*='login' i]",
    "input[placeholder*='メール']",
    "input[placeholder*='ID']",
    "input[placeholder*='ユーザー']",
]
PASS_FIELD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[name='pass']",
    "input[id*='password' i]",
    "input[id*='pass' i]",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('ログイン')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
]


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
async def fetch_hr_csv(
    output_dir: Path,
    is_valid: str = "",
    headless: bool = True,
    timeout_min: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Path:
    """HR ハッカー CSV を自動取得して output_dir に保存し、ファイルパスを返す.

    Args:
        output_dir: 保存先ディレクトリ (なければ作成)
        is_valid:   "" すべて / "1" 公開 / "0" 非公開 / "2" 公開開始前 / "3" 公開終了
        headless:   True=非表示Chromium / False=デバッグ表示
        timeout_min: CSV生成完了ポーリングの最大待ち時間 (分)
        user, password: 明示指定がなければ環境変数 HRHACKER_USER/PASS から取得

    Returns:
        保存したCSVファイルパス
    """
    user = user or os.environ.get("HRHACKER_USER", "")
    password = password or os.environ.get("HRHACKER_PASS", "")
    if not user or not password:
        raise RuntimeError(
            "HRHACKER_USER / HRHACKER_PASS が未設定 (.env に追記して再実行してください)"
        )
    # 生成待ち上限: データ量増でCSV生成が長引くため既定を長め+env設定可能に。
    # (2026-07-09 ユーザー指摘: 今のラグではデータ量増加時に足りなくなる)
    if timeout_min is None:
        timeout_min = int(os.environ.get("HR_CSV_TIMEOUT_MIN", "30"))

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # playwright は遅延 import (mockテスト時の依存を避ける)
    from playwright.async_api import async_playwright  # noqa: WPS433

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = None
        try:
            # 1. セッション確立 (storage_state 復元 → だめなら SSO 最大3回)
            ctx, page = await _establish_session(browser, user, password)

            # 2. onboarding 除去
            await _dismiss_onboarding(page)

            # 3. CSV出力指示
            kick_iso = await _kick_csv_export(page, is_valid=is_valid)

            # 4. 完了ポーリング → DL URL 特定
            dl_url = await _poll_export_list(page, kick_iso=kick_iso, timeout_min=timeout_min)

            # 5. DL 保存
            out_path = await _download_csv(page, dl_url, output_dir, is_valid)
            return out_path
        finally:
            if ctx is not None:
                await ctx.close()
            await browser.close()


# ----------------------------------------------------------------------------
# Step 1: セッション確立 (storage_state 復元 / SSO ログイン + リトライ)
# ----------------------------------------------------------------------------
def classify_sso_url(url: str) -> str:
    """URL を 'admin' / 'login' / 'other' に分類する純関数 (到達判定).

    - 'admin':  hr-hacker.com の /admin 配下 (= ログイン済みで管理画面到達)
    - 'login':  インビジョンSSO (accounts-invision) またはログイン/SSO パス
    - 'other':  上記以外 (about:blank、無関係サイト等)
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url or "")
    except ValueError:
        return "other"
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "accounts-invision" in host or "/login" in path or "/sso" in path:
        return "login"
    if (host == "hr-hacker.com" or host.endswith(".hr-hacker.com")) and (
        path == "/admin" or path.startswith("/admin/")
    ):
        return "admin"
    return "other"


async def _wait_for_admin(page, timeout_s: float = ADMIN_REACH_TIMEOUT_S,
                          poll_s: float = ADMIN_REACH_POLL_S) -> bool:
    """page.url をポーリングして /admin/ 配下到達を判定 ('load' 完了は要求しない)."""
    deadline = time.monotonic() + timeout_s
    while True:
        if classify_sso_url(page.url) == "admin":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


async def _retry_async(fn, max_attempts: int = SSO_MAX_ATTEMPTS,
                       wait_s: float = SSO_RETRY_WAIT_S):
    """async 関数 fn(attempt) を最大 max_attempts 回試行。失敗間は wait_s 秒待機."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(attempt)
        except Exception as exc:  # noqa: BLE001 — 呼び出し側で最終raise
            last_exc = exc
            print(f"[hr_sso] 試行 {attempt}/{max_attempts} 失敗: "
                  f"{type(exc).__name__}: {exc}")
            if attempt < max_attempts:
                print(f"[hr_sso] {wait_s:.0f}s 待機して再試行します")
                await asyncio.sleep(wait_s)
    assert last_exc is not None
    raise last_exc


async def _establish_session(
    browser,
    user: str,
    password: str,
    max_attempts: int = SSO_MAX_ATTEMPTS,
    retry_wait_s: float = SSO_RETRY_WAIT_S,
    storage_state_path: Path = STORAGE_STATE_PATH,
):
    """(context, page) を返す。storage_state 復元を先に試し、だめなら SSO リトライ.

    - storage_state があれば new_context(storage_state=...) → dashboards へ直接 goto。
      /admin/ 配下ならSSOスキップ。ログイン画面等へ飛ばされたら通常SSOへフォールバック。
    - SSO 一連 (goto→入力→submit→到達確認) は最大 max_attempts 回。
      2回目以降は context を作り直す。成功時に storage_state を保存。
    """
    # 1) storage_state 再利用 (レート制限対策: 無駄なSSOを打たない)
    if storage_state_path.exists():
        ctx = await browser.new_context(
            accept_downloads=True, storage_state=str(storage_state_path)
        )
        page = await ctx.new_page()
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"[hr_sso] storage_state 復元後の goto 失敗: {type(exc).__name__}: {exc}")
        if await _wait_for_admin(page, timeout_s=20.0, poll_s=2.0):
            print("[hr_sso] storage_state 再利用でセッション復元 (SSOスキップ)")
            return ctx, page
        print(f"[hr_sso] storage_state 失効 (url={page.url}) — 通常SSOへフォールバック")
        await ctx.close()

    # 2) SSO 一連をリトライ (2回目以降は context 作り直し)
    state: dict = {}

    async def _attempt(attempt: int):
        prev = state.pop("ctx", None)
        if prev is not None:
            try:
                await prev.close()
            except Exception:
                pass
        ctx = await browser.new_context(accept_downloads=True)
        state["ctx"] = ctx
        page = await ctx.new_page()
        print(f"[hr_sso] SSO ログイン試行 {attempt}/{max_attempts}")
        await _login_sso(page, user, password)
        return ctx, page

    ctx, page = await _retry_async(_attempt, max_attempts=max_attempts, wait_s=retry_wait_s)

    # 3) 成功時に storage_state 保存 (失敗しても本処理は継続)
    try:
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(storage_state_path))
        print(f"[hr_sso] storage_state 保存: {storage_state_path}")
    except Exception as exc:
        print(f"[hr_sso] storage_state 保存失敗 (継続): {type(exc).__name__}: {exc}")
    return ctx, page


async def _login_sso(page, user: str, password: str) -> None:
    """インビジョンSSOにログインして /admin/ 配下到達を確認する.

    到達確認は wait_for_url('load') を使わない (重いページで 'load' が45s内に
    完了せず TimeoutError になる実績 2026-07-03)。domcontentloaded まで待ったのち
    page.url ポーリングで /admin/ 配下到達のみ判定する。
    """
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # SSO への遷移を少し待つ
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    user_loc = await _find_first_visible(page, USER_FIELD_SELECTORS)
    pass_loc = await _find_first_visible(page, PASS_FIELD_SELECTORS)
    if user_loc is None or pass_loc is None:
        raise RuntimeError(
            f"SSO ログインフォーム検出失敗 (url={page.url}). "
            "USER_FIELD_SELECTORS / PASS_FIELD_SELECTORS を見直してください"
        )
    await user_loc.fill(user)
    await pass_loc.fill(password)

    submit_loc = await _find_first_visible(page, SUBMIT_SELECTORS)
    if submit_loc is None:
        # フォールバック: Enter キーで submit
        await pass_loc.press("Enter")
    else:
        await submit_loc.click()

    # 到達確認: domcontentloaded で待つ + page.url ポーリング ('load' は要求しない)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    if not await _wait_for_admin(page):
        raise RuntimeError(
            f"SSO 後に /admin/ 配下へ到達できませんでした (url={page.url})"
        )


async def _find_first_visible(page, selectors: list[str]):
    """候補セレクタを順に試して最初に visible な要素を返す (なければ None)."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------------
# Step 2: Onboarding モーダル除去
# ----------------------------------------------------------------------------
async def _dismiss_onboarding(page) -> None:
    """Onboarding/ツアー用モーダルを DOM から強制除去 (visibilityブロック回避)."""
    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll('.g-modal-pos, .g-shape, .g-modal-overlay').forEach(el => el.remove());
            }"""
        )
    except Exception:
        # SPAでまだ無いケースは無視
        pass


# ----------------------------------------------------------------------------
# Step 3: CSV 生成キック
# ----------------------------------------------------------------------------
async def _kick_csv_export(page, is_valid: str) -> str:
    """求人一覧に遷移して #js-search-form を csv-download=1 で submit.

    Returns:
        キック時刻 (ISO文字列、ポーリング時の参照用)
    """
    url = f"{JOB_OFFERS_URL}?is_valid={is_valid}"
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await _dismiss_onboarding(page)

    kick_iso = datetime.now().isoformat()
    await page.evaluate(
        """() => {
            const form = document.getElementById('js-search-form');
            if (!form) { throw new Error('js-search-form not found'); }
            const dl = form.querySelector('[name=csv-download]');
            if (dl) {
                dl.value = '1';
            } else {
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = 'csv-download';
                hidden.value = '1';
                form.appendChild(hidden);
            }
            form.submit();
        }"""
    )
    # submit 後の遷移を待つ (csv-export-list に飛ぶ実装が多いが、戻りパターン不明なので緩め)
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    return kick_iso


# ----------------------------------------------------------------------------
# Step 4: 完了ポーリング
# ----------------------------------------------------------------------------
async def _poll_export_list(page, kick_iso: str, timeout_min: int) -> str:
    """CSV履歴ページを 15秒ごとにリロードし、完了行の DL URL を取得.

    完了行の定義:
        - cells[2] (処理終了日時) が空でない
        - a[href*=download-csv] が存在

    Returns:
        DL URL (string)
    """
    # kick(_kick_csv_export)の form.submit() が起こしたナビゲーションが settling 中に
    # 即 goto すると net::ERR_ABORTED でナビゲーション競合する (2026-07-09 実証)。
    # settle 待ち + リトライで回避 (直接 goto は成功することを確認済)。
    last_err = None
    for attempt in range(4):
        try:
            await page.goto(EXPORT_LIST_URL, wait_until="domcontentloaded",
                            timeout=30_000)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if "ERR_ABORTED" in str(e) or "aborted" in str(e).lower():
                await asyncio.sleep(3)  # 直前ナビゲーションの settle を待って再試行
                continue
            raise
    if last_err is not None:
        raise last_err
    # 明確な生成ラグ: kick直後はまだ生成が始まっていないので、ポーリング前に
    # 明示的に待つ (env HR_CSV_KICK_WAIT_SEC, 既定30秒)。データ量増でも
    # 「作成ボタン→ラグ→再取得」の設計を明確化 (2026-07-09 ユーザー指摘)。
    kick_wait = int(os.environ.get("HR_CSV_KICK_WAIT_SEC", "30"))
    print(f"[hr_csv] CSV生成待ち: 初期ラグ {kick_wait}s → "
          f"以後15秒ごとにポーリング(最大{timeout_min}分)", flush=True)
    await asyncio.sleep(kick_wait)
    deadline = time.time() + timeout_min * 60
    start = time.time()
    while time.time() < deadline:
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await _dismiss_onboarding(page)
        rows = await page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('table tbody tr')).map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim());
                    const dlA = tr.querySelector('a[href*="download-csv"]');
                    return {
                        kishin: cells[0] || '',
                        shuryou: cells[2] || '',
                        info: cells[3] || '',
                        dl: dlA ? dlA.href : null,
                    };
                });
            }"""
        )
        for r in rows:
            if r.get("dl") and r.get("shuryou"):
                elapsed = int(time.time() - start) + kick_wait
                print(f"[hr_csv] CSV生成完了を検出 (待ち{elapsed}s)", flush=True)
                return r["dl"]
        waited = int(time.time() - start) + kick_wait
        print(f"[hr_csv] 生成中... 経過{waited}s / 上限{timeout_min * 60}s",
              flush=True)
        await asyncio.sleep(15)
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            await page.goto(EXPORT_LIST_URL, wait_until="domcontentloaded")

    raise RuntimeError(
        f"CSV 生成が {timeout_min} 分以内に完了しませんでした "
        f"(kick={kick_iso}, url={EXPORT_LIST_URL})。"
        f"データ量が多い場合は環境変数 HR_CSV_TIMEOUT_MIN を増やす。"
    )


# ----------------------------------------------------------------------------
# Step 5: DL 保存
# ----------------------------------------------------------------------------
async def _download_csv(page, dl_url: str, output_dir: Path, is_valid: str) -> Path:
    """expect_download コンテキストで CSV を取得し、タイムスタンプ付きで保存."""
    async with page.expect_download(timeout=120_000) as dl_info:
        await page.evaluate(f"location.href = {dl_url!r}")
    dl = await dl_info.value
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = is_valid or "all"
    out_path = output_dir / f"hr_offers_{label}_{ts}.csv"
    await dl.save_as(str(out_path))
    return out_path


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="HRハッカー CSV 自動取得")
    ap.add_argument(
        "--output",
        default="scratchpad/csv_fetched/hr",
        help="保存先ディレクトリ (default: scratchpad/csv_fetched/hr)",
    )
    ap.add_argument(
        "--is-valid",
        default="",
        choices=["", "0", "1", "2", "3"],
        help='""=すべて / "1"=公開 / "0"=非公開 / "2"=公開開始前 / "3"=公開終了',
    )
    ap.add_argument("--headless", dest="headless", action="store_true", default=False)
    ap.add_argument("--headful", dest="headless", action="store_false")
    ap.add_argument("--timeout-min", type=int, default=20)
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    path = asyncio.run(
        fetch_hr_csv(
            output_dir=Path(args.output),
            is_valid=args.is_valid,
            headless=args.headless,
            timeout_min=args.timeout_min,
        )
    )
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
