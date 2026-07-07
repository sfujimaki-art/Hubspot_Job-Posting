"""クラウドIP SSO疎通検証 (自己完結・GitHub Actions用).

WBS 1.11.9 クラウド移行 ステップ1。
目的: GitHub runner(=データセンターIP)から媒体へ Playwright ログインが通るか検証。
自己完結: scripts/job_application_sync/ (gitignore対象) に依存しない。シート/HubSpot不要。

環境変数:
  MEDIA=aw|hr
  AW_LOGIN_ID / AW_PW           (aw)
  HRHACKER_USER / HRHACKER_PASS (hr)
出力: 到達URL・成否。書込一切なし (ログインしてURL到達を見るだけ)。
"""
from __future__ import annotations
import asyncio
import io
import os
import sys

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  line_buffering=True)
except Exception:
    pass

from playwright.async_api import async_playwright

AW_LOGIN_URL = "https://ats.rct.airwork.net/airplf/login"
HR_ADMIN_URL = "https://hr-hacker.com/admin"

ID_SELECTORS = ("input[type='email']", "input[name='username']",
                "input[name='email']", "input[name='account']",
                "input[type='text']:visible", "input[type='text']")
PW_SELECTORS = ("input[type='password']", "input[name='password']")
SUBMIT_SELECTORS = ("button[type='submit']", "input[type='submit']",
                    "button:has-text('ログイン')", "button:has-text('ログイン')")


async def _fill(page, selectors, value):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click(page, selectors):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def _login_and_check(page, login_url, id_val, pw_val, success_substr,
                           fail_substr="login"):
    await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
    await _fill(page, ID_SELECTORS, id_val)
    await _fill(page, PW_SELECTORS, pw_val)
    await _click(page, SUBMIT_SELECTORS)
    # URL ポーリングで到達判定 (最大60s)
    for _ in range(30):
        await asyncio.sleep(2)
        u = page.url
        if success_substr in u and "login" not in u.split(success_substr)[-1]:
            return True, u
    return False, page.url


async def test_aw(login_id, pw):
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context()).new_page()
        try:
            ok, url = await _login_and_check(
                page, AW_LOGIN_URL, login_id, pw, "ats.rct.airwork.net")
            print(f"[AW] {'SUCCESS ログイン成功' if ok else 'FAIL 未到達'}: url={url[:90]}")
            return 0 if ok else 1
        finally:
            await b.close()


async def test_hr(user, pw):
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context()).new_page()
        try:
            ok, url = await _login_and_check(
                page, HR_ADMIN_URL, user, pw, "hr-hacker.com/admin")
            ok = ok and "dashboard" in url or ("hr-hacker.com/admin" in url
                                               and "login" not in url)
            print(f"[HR] {'SUCCESS ログイン成功' if ok else 'FAIL 未到達'}: url={url[:90]}")
            return 0 if ok else 1
        finally:
            await b.close()


def main():
    media = os.environ.get("MEDIA", "").lower()
    # runner の egress IP を表示 (どのIPで検証したか記録)
    print(f"=== SSO CloudIP Test media={media} ===")
    if media == "aw":
        lid, pw = os.environ.get("AW_LOGIN_ID", ""), os.environ.get("AW_PW", "")
        if not lid or not pw:
            print("AW_LOGIN_ID / AW_PW 未設定"); return 2
        return asyncio.run(test_aw(lid, pw))
    if media == "hr":
        u, pw = os.environ.get("HRHACKER_USER", ""), os.environ.get("HRHACKER_PASS", "")
        if not u or not pw:
            print("HRHACKER_USER / HRHACKER_PASS 未設定"); return 2
        return asyncio.run(test_hr(u, pw))
    print(f"unknown MEDIA={media}"); return 2


if __name__ == "__main__":
    sys.exit(main())
