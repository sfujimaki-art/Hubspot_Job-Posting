"""Air Work 有料広告実績データ (求人単位 / 1週間) を 1顧客分自動取得.

画面 (2026-10-14 実アカウントで読み取り確認):
    https://ats.rct.airwork.net/advertising/analysis  (有料広告 →「効果を分析する」)
    1. データの種類: 「有料広告実績データ」
    2. 期間 (日付範囲ピッカー。入力欄は `2025/5/2 - 2025/5/31` 形式) と
       集計単位 (配信日 / 有料広告 / 求人 → 「求人」)、「データを作成する」
       生成は非同期 (数分〜15分)。単位ごとに 1 ファイルずつ。作成から 24 時間で自動削除。
    3. 「データをダウンロードする」: 単位ごとの行。「更新する」で再読込、完成で
       「ダウンロード」リンク。データ無しは
       「データがありません。指定期間を変更して再度作成ください。」
    ZIP 名: Job_<アカウント番号>_<YYYYMMDD>-<YYYYMMDD>_<作成日時>.zip
            中身: all_*.csv / sponsor_*.csv / organic_*.csv  (有料分は sponsor_)

フェーズ (aw_csv_fetcher と同じ create → collect の二段):
    create : 期間と単位を選んで「データを作成する」を押すだけ (待たない)
    collect: 更新して、できていれば ZIP を保存 (待ちは短時間)
    full   : 1 つの週を create → 完成まで待つ → 保存 (週ごとに繰り返せる)

重要な制約:
    - 単位ごとに同時に作れるのは 1 ファイル。複数週 (バックフィル) は
      同じアカウントの中で 1 週ずつ create → collect → 次の週、と直列に進める。
    - 保存ファイル名にログインIDを入れない (login_file_tag のハッシュ)。
      ハッシュ → ログインIDの対応は保存先直下の manifest に書く (ローカルのみ)。

★未検証: 期間ピッカーと「求人」ラジオ・ダウンロード行の DOM は読み取り調査の
  結果に基づく推定。ローカル PC で --headful で実測して必要なら
  セレクタ候補 (下の *_CANDIDATES) を直すこと。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except Exception:  # noqa: BLE001
    _JST = None

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync.fetchers.aw_csv_fetcher import (  # noqa: E402
    AWNoDataError,
    AWNotReadyError,
    establish_aw_session,
    login_file_tag,
    _button_visible,
    _click_button_by_text,
    _dump_debug,
)

URL_ANALYSIS = "https://ats.rct.airwork.net/advertising/analysis"

RADIO_KIND_PAID = "有料広告実績データ"
RADIO_UNIT_JOB = "求人"
BTN_CREATE = "データを作成する"
BTN_REFRESH = "更新する"
BTN_DOWNLOAD = "ダウンロード"
MSG_NO_DATA = "データがありません"

DEFAULT_OUT_DIR = Path.home() / "airwork-exports" / "billing"
MANIFEST_NAME = "billing_manifest.json"

# 期間入力欄: 値が `2025/5/2 - 2025/5/31` の形
PERIOD_VALUE_RE = re.compile(
    r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*-\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*$")
PERIOD_INPUT_CANDIDATES = (
    "input[placeholder*='/']",
    "input[readonly]",
    "input[type='text']:visible",
)
CAL_PREV_CANDIDATES = (
    "button[aria-label*='前']", "button[aria-label*='prev' i]",
    "[class*='prev' i]", "button:has-text('<')", "button:has-text('‹')",
)
CAL_NEXT_CANDIDATES = (
    "button[aria-label*='次']", "button[aria-label*='next' i]",
    "[class*='next' i]", "button:has-text('>')", "button:has-text('›')",
)
ZIP_NAME_RE = re.compile(r"^Job_.+?_(\d{8})-(\d{8})_.*\.zip$", re.IGNORECASE)


# ----------------------------------------------------------------------------
# 純粋関数 (ブラウザ不要・テスト対象)
# ----------------------------------------------------------------------------
def today_jst() -> date:
    return datetime.now(_JST).date() if _JST else datetime.now().date()


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def last_full_week(today: Optional[date] = None) -> date:
    """直近の「完了した」月〜日の週の月曜日 (JST)。"""
    t = today or today_jst()
    return monday_of(t) - timedelta(days=7)


def week_end(start: date) -> date:
    return start + timedelta(days=6)


def weeks_between(frm: date, to: Optional[date] = None,
                  today: Optional[date] = None) -> list:
    """frm を含む週から、to (または直近の完了週) までの月曜日の昇順リスト。

    未完了の週 (日曜が今日以降) と、to が週の途中の場合のその週は含めない。
    """
    limit = last_full_week(today)
    if to is not None:
        last_in_range = monday_of(to)
        if to < week_end(last_in_range):
            last_in_range -= timedelta(days=7)
        limit = min(limit, last_in_range)
    out, cur = [], monday_of(frm)
    while cur <= limit:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def format_period_input(start: date, end: date) -> str:
    """画面の期間入力欄の表記 (ゼロ詰めなし)。"""
    def f(d: date) -> str:
        return f"{d.year}/{d.month}/{d.day}"
    return f"{f(start)} - {f(end)}"


def parse_period_input(text: str) -> Optional[tuple]:
    m = PERIOD_VALUE_RE.match(text or "")
    if not m:
        return None
    try:
        a = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        b = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    return a, b


def parse_zip_period(filename: str) -> Optional[tuple]:
    """`Job_<番号>_<YYYYMMDD>-<YYYYMMDD>_<作成>.zip` → (開始, 終了)。"""
    m = ZIP_NAME_RE.match(os.path.basename(filename or ""))
    if not m:
        return None
    try:
        a = datetime.strptime(m.group(1), "%Y%m%d").date()
        b = datetime.strptime(m.group(2), "%Y%m%d").date()
    except ValueError:
        return None
    return (a, b) if a <= b else None


def week_dir(out_dir: Path, start: date) -> Path:
    return Path(out_dir) / start.isoformat()


def saved_zip_path(out_dir: Path, login_id: str, start: date) -> Path:
    return (week_dir(out_dir, start)
            / f"billing_{login_file_tag(login_id)}_{start.isoformat()}.zip")


def has_saved(out_dir: Path, login_id: str, start: date) -> bool:
    return saved_zip_path(out_dir, login_id, start).exists()


def record_manifest(out_dir: Path, login_id: str) -> None:
    """ハッシュ → ログインID の対応をローカルに記録 (importer が口座を知る用)。"""
    p = Path(out_dir) / MANIFEST_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    tag = login_file_tag(login_id)
    if data.get(tag) == login_id:
        return
    data[tag] = login_id
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest(out_dir: Path) -> dict:
    p = Path(out_dir) / MANIFEST_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


# ----------------------------------------------------------------------------
# 画面操作 (未検証部分あり。docstring 冒頭の注意を参照)
# ----------------------------------------------------------------------------
async def _click_text(page, text: str, *, exact: bool = True) -> bool:
    """ラジオ等のラベル文字を押す。role=radio → label → テキストの順に試す。"""
    tries = (
        lambda: page.get_by_role("radio", name=text, exact=exact).first,
        lambda: page.locator(f"label:text-is('{text}')").first,
        lambda: page.get_by_text(text, exact=exact).first,
    )
    for t in tries:
        try:
            loc = t()
            if await loc.count() and await loc.is_visible():
                await loc.click()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# 集計単位のラジオ (2026-10-16 実画面で確認: name=advertisingAnalysisFileType、
#   値 01=配信日 / 02=有料広告 / 03=求人)。どのラジオも読み上げ名は共通の「出力ファイル種類」で、
#   「求人」の文字で探すと画面上部の「求人」タブを押してしまい、求人一覧へ移動していた。
UNIT_RADIO_NAME = "advertisingAnalysisFileType"
UNIT_RADIO_JOB = "03"


async def _check_unit_job(page) -> bool:
    """集計単位「求人」を選ぶ。選べて、分析の画面のままのときだけ True。"""
    sel = f'input[name="{UNIT_RADIO_NAME}"][value="{UNIT_RADIO_JOB}"]'
    try:
        radio = page.locator(sel).first
        if not await radio.count():
            return False
        label = page.locator(f"label:has({sel})").first
        await (label if await label.count() else radio).click()
        await asyncio.sleep(0.2)
        return bool(await radio.is_checked()) and "/advertising/analysis" in page.url
    except Exception:  # noqa: BLE001
        return False


async def _find_period_input(page):
    for sel in PERIOD_INPUT_CANDIDATES:
        try:
            locs = page.locator(sel)
            n = await locs.count()
            for i in range(min(n, 8)):
                loc = locs.nth(i)
                if not await loc.is_visible():
                    continue
                if PERIOD_VALUE_RE.match(await loc.input_value() or ""):
                    return loc
        except Exception:  # noqa: BLE001
            continue
    # 値がまだ無い (初期状態) 場合は最初に見える候補を返す
    for sel in PERIOD_INPUT_CANDIDATES:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


async def _visible_months(page) -> list:
    """開いているカレンダーのヘッダ (例 `2025年5月`) から (年, 月) を読む。"""
    texts = await page.evaluate(
        """() => Array.from(document.querySelectorAll('*'))
              .filter(e => e.children.length === 0 &&
                  /^\\s*\\d{4}\\s*(年|\\/|-)\\s*\\d{1,2}\\s*月?\\s*$/.test(e.textContent || ''))
              .map(e => e.textContent.trim())""")
    out = []
    for t in texts:
        m = re.match(r"(\d{4})\D+(\d{1,2})", t)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


async def _go_to_month(page, year: int, month: int) -> None:
    target = year * 12 + month
    for _ in range(60):
        months = await _visible_months(page)
        if any(y * 12 + m == target for y, m in months):
            return
        if not months:
            break
        cur = min(y * 12 + m for y, m in months)
        cands = CAL_PREV_CANDIDATES if target < cur else CAL_NEXT_CANDIDATES
        clicked = False
        for sel in cands:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            break
        await asyncio.sleep(0.2)
    raise RuntimeError(f"カレンダーを {year}年{month}月へ移動できず")


async def _click_day(page, year: int, month: int, day: int) -> None:
    """表示中の `year年month月` パネル内の日付セルを押す (他月のはみ出し日を避ける)。"""
    ok = await page.evaluate(
        """([y, m, d]) => {
            const hdr = Array.from(document.querySelectorAll('*')).find(e =>
                e.children.length === 0 &&
                new RegExp('^\\\\s*' + y + '\\\\s*(年|/|-)\\\\s*' + m + '\\\\s*月?\\\\s*$')
                  .test(e.textContent || ''));
            if (!hdr) return false;
            let panel = hdr;
            for (let i = 0; i < 6 && panel.parentElement; i++) {
                panel = panel.parentElement;
                const cells = Array.from(panel.querySelectorAll('button, td, div, span'))
                    .filter(e => e.children.length === 0 && (e.textContent || '').trim() === String(d));
                if (cells.length) {
                    const el = cells.find(e => !/disabled|outside|other/i.test(e.className || '')) || cells[0];
                    el.click();
                    return true;
                }
            }
            return false;
        }""",
        [year, month, day],
    )
    if not ok:
        raise RuntimeError(f"日付セル {year}/{month}/{day} を押せず")


async def _set_period(page, start: date, end: date) -> None:
    inp = await _find_period_input(page)
    if inp is None:
        raise RuntimeError("期間の入力欄が見つからない")
    await inp.click()
    await asyncio.sleep(0.3)
    await _go_to_month(page, start.year, start.month)
    await _click_day(page, start.year, start.month, start.day)
    await asyncio.sleep(0.2)
    await _go_to_month(page, end.year, end.month)
    await _click_day(page, end.year, end.month, end.day)
    await asyncio.sleep(0.3)
    got = parse_period_input(await inp.input_value())
    if got != (start, end):
        raise RuntimeError("期間の設定結果が意図と異なる (入力欄の表示を確認)")


async def _open_analysis(page) -> None:
    await page.goto(URL_ANALYSIS, wait_until="networkidle", timeout=60_000)


async def _body_text(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


async def _pick_download_link(page) -> bool:
    """「ダウンロード」リンクを 1 つ押す。複数あれば集計単位=求人の行を優先。

    行の data-la-file-type=03 (求人) を見る (2026-10-27 実画面で確認)。属性が無い古い画面だけ、
    リンクが 1 つならそれを押す。
    """
    return bool(await page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a, button'))
                .filter(e => (e.textContent || '').trim() === 'ダウンロード');
            if (!links.length) return false;
            // ★2026-10-27: 作成データの行は data-la-file-type (01=配信日 / 02=有料広告 / 03=求人) を持つ。
            //   以前の「親をたどって行の文字を見る」は、リンクのすぐ外の div で止まり「ダウンロード」しか
            //   読めず、人が配信日・有料広告の単位でも作っていた社 (3 行並ぶ) で毎回押せなかった。
            let pick = links.find(l => {
                const row = l.closest('[data-la-file-type]');
                return row && row.getAttribute('data-la-file-type') === '03';
            }) || null;
            if (!pick) {
                pick = links.find(l => {
                    const row = l.closest('[data-la-file-type]');
                    const t = row ? (row.textContent || '') : '';
                    return t.includes('求人単位') || t.includes('Job_');
                }) || null;
            }
            // 行に種類が付いている画面で求人単位が無ければ、別の単位のファイルは押さない
            if (!pick && links.some(l => l.closest('[data-la-file-type]'))) return false;
            if (!pick && links.length === 1) pick = links[0];
            if (!pick) {
                for (const l of links) {
                    let row = l;
                    for (let i = 0; i < 6 && row.parentElement; i++) {
                        row = row.parentElement;
                        const dl = Array.from(row.querySelectorAll('a, button'))
                            .filter(e => (e.textContent || '').trim() === 'ダウンロード').length;
                        if (dl === 1 && row.querySelectorAll('a, button').length <= 4) break;
                    }
                    const t = row.textContent || '';
                    if (t.includes('求人') && !t.includes('配信日')) { pick = l; break; }
                }
            }
            if (!pick) return false;
            pick.click();
            return true;
        }"""))


async def _job_unit_ready(page) -> bool:
    """求人単位 (03) の完成した行があれば True。行に種類が無い古い画面も True (従来どおり押してみる)。"""
    return bool(await page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('[data-la-file-type]'));
            if (!rows.length) return true;
            return rows.some(r => r.getAttribute('data-la-file-type') === '03'
                                  && r.getAttribute('data-la-file-status') === '01');
        }"""))


async def _download_ready(page, out_dir: Path, login_id: str):
    """完成済みファイルを保存。返り値 (保存パス, 期間開始)。"""
    async with page.expect_download(timeout=60_000) as info:
        if not await _pick_download_link(page):
            # ★2026-10-27: 人が別の単位 (配信日・有料広告) で作ったファイルだけが並び、
            #   求人単位はまだ作成中・未作成のときは、失敗ではなく完成待ちとして扱う。
            if not await _job_unit_ready(page):
                raise AWNotReadyError("求人単位のファイルがまだ無い (別の単位のファイルのみ)")
            # 1 社 (岡安***) で 04:28 から毎回この失敗だった。画面が分からないので残す
            await _dump_debug(page, out_dir, "billing_download_pick")
            raise RuntimeError("ダウンロードリンクの押下に失敗")
    dl = await info.value
    period = parse_zip_period(dl.suggested_filename)
    if period is None:
        raise RuntimeError("ZIP 名から期間を読めない")
    start = period[0]
    dest = saved_zip_path(out_dir, login_id, start)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await dl.save_as(str(dest))
    record_manifest(out_dir, login_id)
    return dest, start


async def _create_week(page, out_dir: Path, login_id: str, start: date) -> None:
    """期間・単位を選んで作成ボタンを押す。直前に完成済みがあれば先に保存する。"""
    await _open_analysis(page)
    # 前の週の完成ファイルが残っていたら、消える前に保存して枠を空ける
    if await _button_visible(page, BTN_DOWNLOAD):
        try:
            _, got = await _download_ready(page, out_dir, login_id)
            if got == start:
                return  # すでに目的の週が完成していた
        except Exception:  # noqa: BLE001
            pass
        await _open_analysis(page)
    if not await _click_text(page, RADIO_KIND_PAID):
        await _dump_debug(page, out_dir, "billing_kind_missing")
        raise RuntimeError(f"「{RADIO_KIND_PAID}」を選べない")
    await asyncio.sleep(0.3)
    await _set_period(page, start, week_end(start))
    if not await _check_unit_job(page):
        await _dump_debug(page, out_dir, "billing_unit_missing")
        raise RuntimeError(f"集計単位「{RADIO_UNIT_JOB}」を選べない")
    await asyncio.sleep(0.2)
    if not await _click_button_by_text(page, BTN_CREATE):
        await _dump_debug(page, out_dir, "billing_create_missing")
        raise RuntimeError(f"「{BTN_CREATE}」ボタン未検出")
    await asyncio.sleep(2)


async def _collect_week(page, out_dir: Path, login_id: str, start: date,
                        timeout_min: float, poll_interval_sec: int):
    """更新しながら完成を待つ。保存パスを返す。

    - データ無し → AWNoDataError
    - 時間内に完成せず → AWNotReadyError
    - 別の週のファイルが出ていたら、その週として保存し、目的の週は未完扱い
    """
    deadline = time.time() + timeout_min * 60
    first = True
    while True:
        if not first:
            try:
                await page.goto(URL_ANALYSIS, wait_until="domcontentloaded",
                                timeout=30_000)
            except Exception:  # noqa: BLE001
                pass
            await _click_button_by_text(page, BTN_REFRESH)
        first = False
        await asyncio.sleep(1.5)
        if await _button_visible(page, BTN_DOWNLOAD) and await _job_unit_ready(page):
            path, got = await _download_ready(page, out_dir, login_id)
            if got == start:
                return path
            raise AWNotReadyError(
                f"別の週のファイルが出ていた (週 {got.isoformat()} を保存、"
                f"目的の週 {start.isoformat()} は未完)")
        if MSG_NO_DATA in await _body_text(page):
            raise AWNoDataError(f"有料広告実績なし (週 {start.isoformat()})")
        if time.time() >= deadline:
            raise AWNotReadyError(f"生成物未完 (週 {start.isoformat()})")
        await asyncio.sleep(poll_interval_sec)


# ----------------------------------------------------------------------------
# 公開 API
# ----------------------------------------------------------------------------
async def fetch_aw_billing_weeks(
    login_id: str,
    password: str,
    weeks: list,
    out_dir: Path,
    headless: bool = True,
    mode: str = "full",
    timeout_min: float = 20,
    poll_interval_sec: int = 30,
    storage_state_path: Optional[Path] = None,
) -> list:
    """1 顧客 (1 ログイン) で複数の週を直列に処理する。

    mode:
      create  : 先頭の未取得の週だけ作成 (待たない)
      collect : 先頭の未取得の週を短時間だけ待って保存
      full    : 未取得の週を 1 週ずつ create → 完成まで待つ → 保存
    返り値: [(週の月曜 date, status, detail)]。status は
      saved / skipped / empty / queued / not_ready / error。
    1 週でも not_ready / error になったら、同じ単位の枠が埋まっている可能性が
    あるため以降の週は処理せず打ち切る。
    """
    from playwright.async_api import async_playwright

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [w for w in weeks if not has_saved(out_dir, login_id, w)]
    results: list = [(w, "skipped", None) for w in weeks if w not in todo]
    if not todo:
        return results
    if mode in ("create", "collect"):
        todo = todo[:1]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx, page = await establish_aw_session(
            browser, login_id, password,
            storage_state_path=storage_state_path, output_dir=out_dir)
        try:
            for w in todo:
                try:
                    if mode in ("create", "full"):
                        await _create_week(page, out_dir, login_id, w)
                        if has_saved(out_dir, login_id, w):
                            results.append((w, "saved", None))
                            continue
                    if mode == "create":
                        results.append((w, "queued", None))
                        break
                    tmo = timeout_min
                    if mode == "collect":
                        tmo = float(os.environ.get("AW_BILLING_COLLECT_TIMEOUT_MIN", "2"))
                    await _collect_week(page, out_dir, login_id, w, tmo,
                                        poll_interval_sec)
                    results.append((w, "saved", None))
                except AWNoDataError:
                    results.append((w, "empty", None))
                except AWNotReadyError as e:
                    results.append((w, "not_ready", str(e)))
                    break
                except Exception as e:  # noqa: BLE001
                    results.append((w, "error", str(e)))
                    break
        finally:
            try:
                await ctx.close()
            finally:
                await browser.close()
    return results


def _build_arg_parser():
    import argparse
    ap = argparse.ArgumentParser(description="AW 有料広告実績 (求人単位/週) 取得")
    ap.add_argument("--login-id", required=True)
    ap.add_argument("--password", help="未指定ならシートから login-id で引く")
    ap.add_argument("--output", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--week-start", help="対象週の月曜 YYYY-MM-DD (既定: 直近の完了週)")
    ap.add_argument("--mode", choices=["create", "collect", "full"], default="full")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--timeout-min", type=float, default=20)
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()
    pw = args.password
    if not pw:
        from scripts.job_application_sync.fetchers.account_loader import (
            find_account_by_login_id,
        )
        acc = find_account_by_login_id(args.login_id)
        if not acc:
            raise SystemExit("PW 取得失敗: シートに該当 login-id が無い。--password で指定可。")
        pw = acc["password"]
    start = (date.fromisoformat(args.week_start) if args.week_start
             else last_full_week())
    if start.weekday() != 0:
        raise SystemExit("--week-start は月曜日を指定してください")
    res = asyncio.run(fetch_aw_billing_weeks(
        args.login_id, pw, [start], Path(args.output),
        headless=not args.headful, mode=args.mode, timeout_min=args.timeout_min))
    for w, st, _ in res:
        print(f"week={w.isoformat()} status={st}")


if __name__ == "__main__":
    main()
