"""Air Work 求人一覧 XLSX (ZIP) を 1顧客分自動取得.

Phase 0b 実測準拠フロー:
    1. https://ats.rct.airwork.net/airplf/login
       → AirRegi SSO (connect.airregi.jp/login) にリダイレクト
       → ID/PW 入力 → 送信
    2. ログイン完了で https://ats.rct.airwork.net/dashboards
    3. https://ats.rct.airwork.net/job_offers/bulk_download
    4. 「データを作成する」ボタン (text 完全一致) click → 非同期生成
    5. 「更新する」ボタンclick + 待機 を 30秒間隔で繰り返し
    6. 「ダウンロードする」ボタン出現で click → 内部 API GET で ZIP DL
       GET /api/templates/job_offers/bulk_download/api/download
           ?jobOffersBulkProgressManagementId={id}

注意:
    - AirRegi SSO のフィールドセレクタは name=accountId / name=password を想定。
      未確認なら headful 実行で実DOMを確認すること。
    - session 切れを避けるため 1顧客 1 context (storage_state 共有なし)。
    - ログに PW を出さない。失敗時 ScreenShot + DOM dump はPWマスクして保存。

Usage:
    python -m scripts.job_application_sync.fetchers.aw_csv_fetcher \
        --login-id rpo_xxxxx --output scratchpad/csv_fetched/aw [--headful]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# Phase 0b 実測 URL
URL_LOGIN = "https://ats.rct.airwork.net/airplf/login"
URL_DASHBOARDS = "https://ats.rct.airwork.net/dashboards"
URL_BULK_DOWNLOAD = "https://ats.rct.airwork.net/job_offers/bulk_download"

# 採用サイトURL(slug)抽出用セレクタ (2026-07-08 geo-3070 で実証)
#  ナビ→設定→採用ページ設定 と辿ると 採用サイトURL(https://arwrk.net/recruit/{slug})
#  が <a> として公開されている。CSSモジュールのハッシュクラスは壊れうるので
#  fallback(a[href*='arwrk.net/recruit'])も併用する。
AW_RECRUIT_NAV_SEL = "#__next > header > div > nav > ul > li:nth-child(6) > a"
AW_RECRUIT_ASIDE_SEL = ("#__next > div.styles_container__BxkYX > "
                        "aside.styles_module___F_Ud > div > div > a:nth-child(10)")
AW_RECRUIT_URL_SEL = ("#scroll-container > main > section.styles_module__Q5xzW > "
                      "div:nth-child(2) > div.styles_columnContent__u8kIn > a")

# ボタン text (完全一致 / Phase 0b 実測)
BTN_CREATE = "データを作成する"
BTN_REFRESH = "更新する"
BTN_DOWNLOAD = "ダウンロードする"

# 求人ゼロ時の画面メッセージ (2026-07-03 実測: 吉島タクシー等が該当)。
# この状態は「ダウンロードする」が永遠に出ないため、検知して即 empty 扱いにする。
MSG_NO_DATA = "データがありません"


class AWNoDataError(Exception):
    """AW一括DLで求人が0件 (「データがありません」)。エラーではなく空結果。"""


class AWNotReadyError(Exception):
    """mode=collect で生成物がまだ出来ていない (Phase1未完/生成中)。再キュー対象。"""

# AirRegi SSO フォーム (実測時に必要なら上書き)
SSO_ID_SELECTOR_CANDIDATES = (
    "input[name='accountId']",
    "input[name='loginId']",
    "input[type='text'][autocomplete='username']",
    "input[type='email']",
    "input[type='text']:visible",  # Phase 2 動作確認 2026-06-29 実測: 札幌三信運輸 SSOで命中
)
SSO_PW_SELECTOR_CANDIDATES = (
    "input[name='password']",
    "input[type='password']",
)
SSO_SUBMIT_SELECTOR_CANDIDATES = (
    "button[type='submit']",
    "input[type='submit']",
)


def _mask(s: str) -> str:
    """ログ用に PW などをマスク."""
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


async def _fill_first_match(page, selectors: tuple[str, ...], value: str) -> bool:
    """セレクタ候補を順に試して最初にヒットしたものへ value を入力."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_first_match(page, selectors: tuple[str, ...]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def _click_button_by_text(page, text: str) -> bool:
    """text 完全一致 button / a を JS 経由で click. 見つからなければ False."""
    clicked = await page.evaluate(
        """(label) => {
            const tagNames = ['button', 'a'];
            for (const tag of tagNames) {
                const els = Array.from(document.querySelectorAll(tag));
                const hit = els.find(el => (el.textContent || '').trim() === label);
                if (hit) {
                    hit.click();
                    return true;
                }
            }
            return false;
        }""",
        text,
    )
    return bool(clicked)


async def _button_visible(page, text: str) -> bool:
    return bool(
        await page.evaluate(
            """(label) => {
                const els = Array.from(document.querySelectorAll('button, a'));
                return !!els.find(el => (el.textContent || '').trim() === label);
            }""",
            text,
        )
    )


async def _dump_debug(page, output_dir: Path, tag: str) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png = output_dir / f"debug_{tag}_{ts}.png"
        html = output_dir / f"debug_{tag}_{ts}.html"
        await page.screenshot(path=str(png), full_page=True)
        body = await page.content()
        html.write_text(body, encoding="utf-8")
    except Exception:
        pass


CREATE_DONE = "__create_done__"  # mode="create" 完了マーカー (生成トリガーのみ)

URL_DASHBOARD = "https://ats.rct.airwork.net/dashboards"


async def establish_aw_session(
    browser,
    login_id: str,
    password: str,
    storage_state_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
):
    """AW セッション確立 (BAN対策: PWフルログインの反復を避ける).

    storage_state があれば再利用 → dashboards に直行できればセッション有効。
    切れて(ログイン画面に飛ぶ)いたら full login して新セッションを保存。
    (2026-07-07 実測検証済: 再利用OK / アカウント切替でも破綻せず)

    Returns:
        (context, page)  page は認証済 dashboards 上
    Raises:
        RuntimeError: ログイン失敗
    """
    sp = Path(storage_state_path) if storage_state_path else None

    # 1) 保存セッション再利用を試みる
    if sp and sp.exists():
        ctx = await browser.new_context(accept_downloads=True,
                                        storage_state=str(sp))
        page = await ctx.new_page()
        try:
            await page.goto(URL_DASHBOARD, wait_until="domcontentloaded",
                            timeout=45000)
            if "login" not in page.url and "airplf" not in page.url:
                return ctx, page          # 再利用成功 = ログイン画面を通らず
        except Exception:  # noqa: BLE001
            pass
        await ctx.close()                 # 無効 → 破棄して full login

    # 2) full login (初回 or セッション切れ)
    ctx = await browser.new_context(accept_downloads=True)
    page = await ctx.new_page()
    await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45000)
    ok_id = await _fill_first_match(page, SSO_ID_SELECTOR_CANDIDATES, login_id)
    ok_pw = await _fill_first_match(page, SSO_PW_SELECTOR_CANDIDATES, password)
    if not (ok_id and ok_pw):
        if output_dir:
            await _dump_debug(page, output_dir, "login_form_not_found")
        raise RuntimeError(
            f"AirRegi SSO のID/PWフィールド未検出 (login_id={login_id}, "
            f"pw={_mask(password)})"
        )
    if not await _click_first_match(page, SSO_SUBMIT_SELECTOR_CANDIDATES):
        if output_dir:
            await _dump_debug(page, output_dir, "login_submit_not_found")
        raise RuntimeError("AirRegi SSO の submit ボタン未検出")
    try:
        await page.wait_for_url("**/dashboards*", timeout=45000)
    except Exception as e:  # noqa: BLE001
        if output_dir:
            await _dump_debug(page, output_dir, "login_failed")
        raise RuntimeError(
            f"ログイン後 dashboards に遷移せず (login_id={login_id}, "
            f"last_url={page.url[:80]})"
        ) from e

    # 3) 新セッション保存 (次回から再利用)
    if sp:
        sp.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(sp))
    return ctx, page


async def extract_aw_recruit_site_url(page) -> Optional[str]:
    """AW採用サイトURL (https://arwrk.net/recruit/{slug}) を抽出.

    認証済 page から ナビ→採用ページ設定 と辿り、公開されている採用サイトURLを返す。
    セレクタ(ハッシュCSS)が壊れた場合は a[href*='arwrk.net/recruit'] で回収。
    取得できなければ None。求人URLは この戻り値 + '{求人ID}/' で組み立てる。
    """
    # 1) 実証済セレクタ経路
    try:
        await page.click(AW_RECRUIT_NAV_SEL, timeout=15000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.click(AW_RECRUIT_ASIDE_SEL, timeout=15000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        el = await page.wait_for_selector(AW_RECRUIT_URL_SEL, timeout=15000)
        href = await el.get_attribute("href")
        if href and "arwrk.net/recruit" in href:
            return href.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    # 2) fallback: ページ内の採用サイトリンクを拾う
    try:
        links = await page.eval_on_selector_all(
            "a[href*='arwrk.net/recruit']", "els=>els.map(e=>e.href)")
        for h in links:
            # 求人個別(/{id}/)でなく 採用サイトトップ(slugまで)を優先
            if h and "arwrk.net/recruit" in h:
                return h.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return None


def build_aw_job_url(recruit_site_url: str, media_job_id: str) -> str:
    """採用サイトURL + 求人ID → 個別求人URL (末尾スラッシュ付き)."""
    if not recruit_site_url or not media_job_id:
        return ""
    return f"{recruit_site_url.rstrip('/')}/{media_job_id}/"


# login_id → 採用サイトURL のキャッシュ (アカウント単位で1回抽出すれば十分)。
# 求人作成フローがこれを読んで url_airwork を補完する。
RECRUIT_URL_CACHE = "aw_recruit_urls.json"


def load_recruit_url_cache(state_dir: Path) -> dict:
    import json
    f = Path(state_dir) / RECRUIT_URL_CACHE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _cache_recruit_url_if_missing(page, login_id: str, state_dir: Path) -> None:
    """未キャッシュなら採用サイトURLを抽出して保存 (遅延・BAN対策)。

    本体の求人取得を絶対に妨げないよう、失敗は握りつぶす(bonus処理)。
    """
    import json
    try:
        cache = load_recruit_url_cache(state_dir)
        if cache.get(login_id):
            return                              # 既に取得済 → 何もしない(無駄なナビ回避)
        url = await extract_aw_recruit_site_url(page)
        if url:
            cache[login_id] = url
            f = Path(state_dir) / RECRUIT_URL_CACHE
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass                                    # 補完は best-effort、本体を止めない


async def fetch_aw_xlsx(
    login_id: str,
    password: str,
    output_dir: Path,
    headless: bool = True,
    timeout_min: int = 20,
    poll_interval_sec: int = 30,
    mode: str = "full",
    storage_state_path: Optional[Path] = None,
):
    """mode:
      - "full"    : login→生成→DL (従来動作, Path返却)
      - "create"  : login→「データを作成する」clickのみ (生成待ちしない)。
                    Phase1用。CREATE_DONE 文字列を返す (求人0件なら AWNoDataError)。
      - "collect" : login→生成済み前提で短時間ポーリング→DL。Phase2用 (待機なし)。
                    Path返却。まだ生成中なら短timeout後 AWNotReadyError。"""
    """AW 1顧客の 求人一覧 ZIP を取得して output_dir に保存.

    Args:
        login_id: AirWork ID
        password: AirワークPW (ログ出力厳禁)
        output_dir: 保存先 (存在しなければ作成)
        headless: True ならヘッドレス
        timeout_min: ZIP 生成完了待ちの最大分数
        poll_interval_sec: 「更新する」click の間隔秒

    Returns:
        保存した .zip のパス

    Raises:
        RuntimeError: ログイン失敗 / ボタン未検出 / タイムアウト
    """
    # 遅延 import (テストで playwright 未インストールでも account_loader 単体テストは通したい)
    from playwright.async_api import async_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        # セッション再利用でログイン (BAN対策)。storage_state 未指定なら毎回 full login。
        ctx, page = await establish_aw_session(
            browser, login_id, password,
            storage_state_path=storage_state_path, output_dir=output_dir,
        )
        try:
            # 1) ログインは establish_aw_session で完了済 (セッション再利用/再ログイン)
            # 1b) 採用サイトURL(slug)を未取得なら1回だけ抽出・キャッシュ (url_airwork補完用)
            await _cache_recruit_url_if_missing(page, login_id, output_dir)
            # 2) 一括DL画面
            await page.goto(URL_BULK_DOWNLOAD, wait_until="networkidle")

            # 3) 「データを作成する」 click (既存生成済みなら直接「ダウンロードする」が出ているケースもある)
            already_ready = await _button_visible(page, BTN_DOWNLOAD)

            # --- mode=create: 生成トリガーのみ (Phase1)。DLせず即返す ---
            if mode == "create":
                if already_ready:
                    return CREATE_DONE  # 既に生成済み → Phase2で回収
                # 求人0件チェック (作成ボタン押す前に「データがありません」なら空)
                try:
                    body0 = await page.inner_text("body")
                except Exception:
                    body0 = ""
                clicked = await _click_button_by_text(page, BTN_CREATE)
                if not clicked:
                    if MSG_NO_DATA in body0:
                        raise AWNoDataError(f"求人0件 (login_id={login_id})")
                    await _dump_debug(page, output_dir, "create_btn_missing")
                    raise RuntimeError(f"「{BTN_CREATE}」ボタン未検出 (login_id={login_id})")
                return CREATE_DONE  # 生成トリガー完了 (待たない)

            # --- mode=collect: 生成済み前提。create押さず短時間で回収 (Phase2) ---
            if mode == "collect":
                timeout_min = 3  # 生成済みのはず → 短timeout。未完なら AWNotReadyError
            elif not already_ready:
                clicked = await _click_button_by_text(page, BTN_CREATE)
                if not clicked:
                    await _dump_debug(page, output_dir, "create_btn_missing")
                    raise RuntimeError(
                        f"「{BTN_CREATE}」ボタン未検出 (login_id={login_id})"
                    )

            # 4) 完了ポーリング
            # ★ユーザー知見(2026-07-03): AWは生成完了しても画面が自動更新されず
            #   「ダウンロードする」が出ないことがある。in-page「更新する」ボタンでは
            #   反映されない場合があり、page.reload() のフルリロードで完了状態が現れる。
            #   → ポーリング毎にフルリロードして BTN_DOWNLOAD を再判定する (timeout大幅短縮)。
            deadline = time.time() + timeout_min * 60
            first_iter = True
            while time.time() < deadline:
                if not first_iter:
                    # フルリロードで生成完了状態を反映させる (BTN_REFRESHより確実)
                    try:
                        await page.goto(URL_BULK_DOWNLOAD,
                                        wait_until="domcontentloaded",
                                        timeout=30_000)
                    except Exception:
                        pass
                    # 保険で in-page「更新する」も押す (存在すれば)
                    await _click_button_by_text(page, BTN_REFRESH)
                first_iter = False

                if await _button_visible(page, BTN_DOWNLOAD):
                    # 5) DL
                    async with page.expect_download(timeout=60_000) as dl_info:
                        ok = await _click_button_by_text(page, BTN_DOWNLOAD)
                        if not ok:
                            raise RuntimeError(
                                f"「{BTN_DOWNLOAD}」 click 失敗 (login_id={login_id})"
                            )
                    dl = await dl_info.value
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_id = re.sub(r"[^0-9A-Za-z_.-]", "_", login_id)
                    out_path = output_dir / f"aw_{safe_id}_{ts}.zip"
                    await dl.save_as(str(out_path))
                    return out_path

                # 求人ゼロ検知: 「データがありません」が出ていれば即 empty で返す
                # (20分待たない)。DLボタンが無い かつ このメッセージがある場合のみ。
                try:
                    body_txt = await page.inner_text("body")
                except Exception:
                    body_txt = ""
                if MSG_NO_DATA in body_txt:
                    raise AWNoDataError(
                        f"求人0件 (login_id={login_id}): {MSG_NO_DATA}")

                # collect は待たない (生成済み前提)。短間隔で数回だけ確認
                await asyncio.sleep(5 if mode == "collect" else poll_interval_sec)

            if mode == "collect":
                # Phase2で生成物が未完 = Phase1未実行/生成中 → 再キュー対象
                raise AWNotReadyError(
                    f"生成物未完 (login_id={login_id})。Phase1未実行/生成中")
            await _dump_debug(page, output_dir, "timeout")
            raise RuntimeError(
                f"AW XLSX 生成 {timeout_min} 分以内に完了せず (login_id={login_id})"
            )
        finally:
            try:
                await ctx.close()
            finally:
                await browser.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AW 求人一覧 XLSX (ZIP) 自動取得")
    ap.add_argument("--login-id", required=True, help="AirWork ID")
    ap.add_argument(
        "--password",
        help="未指定なら Sheets「アカウント情報」から login-id で引く",
    )
    ap.add_argument(
        "--output",
        default="scratchpad/csv_fetched/aw",
        help="保存先ディレクトリ",
    )
    ap.add_argument(
        "--headful",
        action="store_true",
        help="ブラウザを表示して実行 (デバッグ用)",
    )
    ap.add_argument(
        "--timeout-min",
        type=int,
        default=20,
        help="ZIP 生成完了待ちの最大分数 (default: 20)",
    )
    return ap


def _resolve_password(login_id: str, password: Optional[str]) -> str:
    if password:
        return password
    # 遅延 import (gspread の認証エラーを CLI 引数指定で回避できるように)
    try:
        from job_application_sync.fetchers.account_loader import (
            find_account_by_login_id,
        )
    except ImportError:
        # スクリプト直接実行時の sys.path フォールバック
        sys.path.insert(
            0,
            str(Path(__file__).resolve().parent.parent.parent),
        )
        from job_application_sync.fetchers.account_loader import (  # type: ignore
            find_account_by_login_id,
        )
    acc = find_account_by_login_id(login_id)
    if not acc:
        raise SystemExit(
            f"AW PW 取得失敗: login_id={login_id} がシートに存在しない / "
            f"または該当列が空。--password 直接指定で実行可。"
        )
    return acc["password"]


def main() -> None:
    args = _build_arg_parser().parse_args()
    pw = _resolve_password(args.login_id, args.password)
    out_path = asyncio.run(
        fetch_aw_xlsx(
            login_id=args.login_id,
            password=pw,
            output_dir=Path(args.output),
            headless=not args.headful,
            timeout_min=args.timeout_min,
        )
    )
    # PW はログに出さない
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
