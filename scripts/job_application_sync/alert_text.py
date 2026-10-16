"""Slack に送る失敗の通知を、原因が分かる言葉にする部品 (2026-10-16)。

なぜ (ユーザー指示「エラーの報告内容が原因を特定できるようにしてほしい」):
  - AirWork の CloudFront が 403 で弾いたときも「ボタン未検出」「ID/PW フィールド未検出」
    と出ていて、IDやPWの問題に見えていた (実際は GitHub Actions からのアクセス拒否)。
  - ID/PW の誤りは「ログイン後 dashboards に遷移せず」と出ていて、何を直せばよいか
    分からなかった (AirRegi が login_failed=true を返している)。
  - 通知が専用PCからか GitHub Actions からか、Slack 上で見分けられなかった。
"""
from __future__ import annotations

import os


def source_tag() -> str:
    """通知の出どころ。GitHub Actions なら [Actions]、それ以外 (専用PC) は [PC]。"""
    return "[Actions]" if os.environ.get("GITHUB_ACTIONS") == "true" else "[PC]"


def classify_aw_error(error: str, blocked_403: bool = False) -> tuple[str, str]:
    """AW の取り込み失敗の例外文 → (原因, 対処)。

    blocked_403: この PC が AirWork の CloudFront 403 を受けて止まっている最中か。
    """
    e = str(error or "")
    if "403" in e or "Request blocked" in e or blocked_403:
        return ("AirWork がこのアクセスを拒否しました (CloudFront 403)。ID や PW の問題ではありません",
                "時間を置いて自動でやり直します。続く場合はアクセス元 (Actions/PC) を確認")
    if "login_failed=true" in e or "login_failed=tru" in e:
        return ("ID か PW が違います (AirRegi がログインを拒否しました)",
                "顧客管理シートの企業AirWorkのID/PWを確認してください")
    if "GITHUB_ACTIONS" in os.environ and ("未検出" in e or "遷移せず" in e):
        return ("GitHub Actions からは AirWork にアクセスできません (海外のサーバーとして拒否)。"
                "画面が出ないため部品が見つかりません",
                "AW は専用PCで取り込みます。Actions の AW 処理を止めてください")
    if "ID/PWフィールド未検出" in e:
        return ("AirRegi のログイン画面が想定と違います (画面の作り変え・メンテナンス・アクセス拒否の可能性)",
                "PCのログの失敗画面 (debug_*.png) を確認してください")
    if "ボタン未検出" in e:
        return ("応募者一覧の画面にダウンロードのボタンがありません (画面の作り変え・権限・アクセス拒否の可能性)",
                "PCのログの失敗画面 (debug_*.png) を確認してください")
    if "遷移せず" in e:
        return ("ログイン後に管理画面へ入れません (契約の状態・権限・追加の確認画面の可能性)",
                "その社の AirWork に手でログインして確認してください")
    if "ERR_NAME_NOT_RESOLVED" in e or "Timeout" in e or "ERR_" in e:
        return ("通信の失敗です (一時的なもの)", "自動でやり直します")
    return ("原因を分類できませんでした", "理由の全文を確認してください")


def classify_hr_error(error: str) -> tuple[str, str]:
    """HRハッカーの取り込み失敗の理由 → (原因, 対処)。理由の文は fetch_and_import_hr が作る。"""
    e = str(error or "")
    if "有効な日付なし" in e:
        return ("通知の応募日 (シート1の D 列) が読めず、取り込む期間を決められません",
                "シート1の該当行の D 列の日付を確認してください")
    if "取込エラー" in e:
        return ("HRハッカーの CSV は取れましたが、HubSpot への登録が全行で失敗しました",
                "PCのログ (非公開ログの行ごとの理由) を確認してください")
    if "ログイン" in e or "login" in e.lower():
        return ("HRハッカーにログインできません (ID/PW・画面の変更の可能性)",
                "HRハッカーのマスターアカウントで手でログインできるか確認してください")
    if "Timeout" in e or "ERR_" in e or "getaddrinfo" in e or "Connection" in e:
        return ("通信の失敗です (一時的なもの)", "自動でやり直します")
    return ("原因を分類できませんでした", "理由の全文を確認してください")


def hr_failure_message(error: str, attempts: int) -> str:
    cause, action = classify_hr_error(error)
    return (f"⚠️ {source_tag()} HR応募登録失敗\n"
            f"・原因: {cause}\n"
            f"・対処: {action}\n"
            f"・{attempts}回試行済 (以後は時間を置いて自動で再試行。この通知は1日1回)\n"
            f"・理由の全文: {error}")


def aw_failure_message(company: str, error: str, attempts: int,
                       blocked_403: bool = False) -> str:
    cause, action = classify_aw_error(error, blocked_403)
    return (f"⚠️ {source_tag()} 応募登録失敗: {company}\n"
            f"・原因: {cause}\n"
            f"・対処: {action}\n"
            f"・{attempts}回試行済 (以後は時間を置いて自動で再試行。この社の通知は1日1回)\n"
            f"・理由の全文: {error}")
