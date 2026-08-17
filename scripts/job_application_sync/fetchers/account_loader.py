"""顧客管理シート「アカウント情報」タブから AW/HR の ID/PW を取得.

Phase 0a 実測準拠 (列名は実測どおり、改行・空白あり)。

Usage:
    from job_application_sync.fetchers.account_loader import iter_aw_accounts
    for acc in iter_aw_accounts(active_only=True, prefer="A"):
        # acc = {"company_name": str, "login_id": str, "password": str, "source": "A"|"B"}
        ...

セキュリティ:
    - password は dict に保持するが、ログ / ドキュメント / stdout への出力禁止。
    - get_sheets_client() は service account JSON を環境変数 GOOGLE_SA_JSON から読む。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Iterator, Optional

import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as UserCredentials


# 公開リポ方針: シートIDは直書きせず env 注入 (2026-07-07)。
# 実値はコードに残さない(公開安全)。ローカルは .env / CIは Secret から JAS_SHEET_ID。
SHEET_ID = os.environ.get("JAS_SHEET_ID", "")
SHEET_TAB = "アカウント情報"

# Phase 2 動作確認 (2026-06-29 fujimaki OAuth実測): 「会社名」列はヘッダ空文字 (col2)
# 空ヘッダは get_all_values 経由で _COL{i}_ という代替キーで命名される
COL_COMPANY = "_COL2_"  # 実シートで col2=会社名 (ヘッダ空)、_normalize_key 経由でこの名前と一致
COL_AW_ID_A = "AirWork ID"
COL_AW_PW_A = "AirワークPW"
COL_AW_ID_B = "企業AirWork ID"
COL_AW_PW_B = "企業Airwork PW"
COL_CLOSED = "クローズ"  # FALSE=アクティブ / TRUE=クローズ済み
# 管理メール(リクロジ rpo.medica+xxx) = col8。ヘッダ表記は's'で不適切なため位置キーで参照。
# Deal.kanri_mail_address との結合キー (src A/B 両方に存在, 2026-07-03 実測で確定)。
COL_MANAGE_MAIL = "_COL8_"

# ★readonly ではなく書込可にする (2026-08-07)。
#   応募連携は処理済みの印としてシート1のF列に「受入済」を書くが、
#   スコープが readonly だったため `values.batchUpdate` が必ず403になり、
#   **本番で一度も成功していなかった**(実測: F列の「受入済」= 0件、
#   GASが書いた「キュー済」の最終更新が 2026-06-28)。
#   失敗は applicant_sync.py 側の except で握られてログに型名が出るだけで、
#   台帳だけがDONEになる非対称が常態化していた。結果、**現場がシート1を
#   見ても処理済みかどうか判別できない**。
#   同リポの customer_sheet_sync.py:52 は最初から書込スコープを持っており、
#   ここだけが取り残されていた。
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# OAuth トークン既定パス (Phase 2 動作確認 2026-06-29: SA 権限未付与のため OAuth fallback)
DEFAULT_OAUTH_TOKEN = "credentials/fujimaki_token.json"


def sheet_retry(fn: "Callable[..., Any]", *a, retries: int = 5, **kw) -> "Any":
    """Sheets API の一時障害で落ちないように呼び出しを包む。

    ★なぜ要るか (2026-08-17)
      シートを**読む**呼び出しに再試行が無く、Googleが 502/503 を返すと
      その回の応募取込が丸ごと落ちていた。クライアント取得側
      (applicant_queue._sheets_client) には既に再試行があったが、
      データ取得の .get() / .get_all_values() には無かった。

      実測: 本番リポジトリの失敗99件中 86件が Applicant Sync。
            8/16 だけで15件。ログはいずれも
            `gspread.exceptions.APIError: [503] The service is currently unavailable.`
            5分毎に走るので1回の瞬断がそのまま1回分の取りこぼしになる。

      **5xx と 429 だけ再試行する。** 403/404 は権限やIDの設定ミスで、
      何度やっても直らない。再試行すると原因が見えなくなるので即座に上げる。
    """
    delay = 2.0
    last = None
    for i in range(retries + 1):
        try:
            return fn(*a, **kw)
        except gspread.exceptions.APIError as e:
            code = 0
            try:
                code = int(getattr(e, "response", None).status_code)
            except (AttributeError, TypeError, ValueError):
                pass
            if code and not (code >= 500 or code == 429):
                raise                      # 設定ミスは握らない
            last = e
        except (ConnectionError, TimeoutError, OSError) as e:
            last = e                       # DNS断・コネクション切れ
        if i < retries:
            print(f"    [sheets retry {i+1}/{retries}] {type(last).__name__}: "
                  f"{delay:.0f}秒待って再試行", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"Sheets 呼び出しがリトライ上限で失敗: {last}")


def get_sheets_client(auth_mode: Optional[str] = None) -> gspread.Client:
    """Sheets API クライアント取得.

    Args:
        auth_mode:
            - None or "auto": 環境変数 SHEETS_AUTH_MODE 参照, 未設定なら "oauth"
            - "sa":    Service Account (GOOGLE_SA_JSON)
            - "oauth": ユーザー OAuth (GOOGLE_OAUTH_TOKEN or DEFAULT_OAUTH_TOKEN)
    """
    mode = auth_mode or os.environ.get("SHEETS_AUTH_MODE", "oauth")

    if mode == "sa":
        sa_path = os.environ.get("GOOGLE_SA_JSON")
        if not sa_path or not os.path.isfile(sa_path):
            raise RuntimeError(f"GOOGLE_SA_JSON 未設定または不在: {sa_path!r}")
        creds = SACredentials.from_service_account_file(sa_path, scopes=SCOPES)
        return gspread.authorize(creds)

    # OAuth (既定経路)
    oauth_path = os.environ.get("GOOGLE_OAUTH_TOKEN", DEFAULT_OAUTH_TOKEN)
    if not os.path.isabs(oauth_path):
        # リポジトリルート相対 (実行ディレクトリに依存しないように)
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]
        oauth_path = str(repo_root / oauth_path)
    if not os.path.isfile(oauth_path):
        raise RuntimeError(f"OAuth token not found: {oauth_path}")
    creds = UserCredentials.from_authorized_user_file(oauth_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _normalize_key(s: object) -> str:
    """シート列見出しの改行・空白を吸収して比較するためのキー.

    実シートの見出しは「AirWork\\nID」「AirワークPW 」のように
    改行・末尾空白が混ざる。定数側 (例: COL_AW_ID_A="AirWork ID") との
    一致を担保するため、全空白文字 (半角/全角/改行/タブ) を除去して比較する。
    """
    if s is None:
        return ""
    text = str(s)
    return "".join(ch for ch in text if not ch.isspace() and ch != "　")


def _get_field(row: dict, *candidates: str) -> str:
    """正規化キーで row から値を引く (候補列名のいずれかにマッチ)."""
    # row のキーも正規化してマッチ
    normalized = {_normalize_key(k): v for k, v in row.items()}
    for c in candidates:
        key = _normalize_key(c)
        if key in normalized and normalized[key] is not None:
            return str(normalized[key]).strip()
    return ""


# ── セル正規化 (2026-07-24) ─────────────────────────────────────────────
# 顧客管理シートには1セルに複数値/ラベル/メモが混在する行がある(実測 十数件):
#   'ID：sankikoube\nID：@sanki6451\nID：SANKI7228'      … 複数ID+ラベル
#   'ID :kountohoku7200'                                  … ラベル(スペース揺れ)
#   'otsuka@roadcar.jp\n1.0で登録：https://...'           … 資格情報+メモ混在
# 生値のままログインすると必ず失敗するため、最初の妥当な値だけを抽出する。
# クリーンなセル(改行/ラベル無し)は無変更で素通し = 既存挙動を壊さない。
_CRED_LABEL = re.compile(r"^(ID|PW|パスワード)\s*[：:]\s*", re.IGNORECASE)
_CRED_NOTE = re.compile(r"https?://|登録|※|→|↓")


def _first_credential(cell: str, split_comma: bool = True) -> str:
    """複数値/ラベル/メモ混在セルから最初の妥当な資格情報を返す。

    split_comma: IDはカンマ区切りも分割。PWはカンマを含み得るため False 推奨。
    ('yamanaka/kisai/10' のようにスラッシュを含む正規IDがあるため / では割らない)
    """
    cell = (cell or "").strip()
    if "\n" not in cell and not _CRED_LABEL.match(cell):
        return cell  # クリーンなセルは素通し(挙動不変)
    parts = re.split(r"[\n,]" if split_comma else r"\n", cell)
    for p in parts:
        p = _CRED_LABEL.sub("", p.strip())
        if not p or _CRED_NOTE.search(p):
            continue
        return p
    return ""


def _select_credentials(
    row: dict, prefer: str = "B"
) -> Optional[tuple[str, str, str]]:
    """AWログイン資格情報を選ぶ = 企業(B系)のみ使用.

    2026-07-07 ユーザー確定: AWログインに使うのは
    「企業AirWork ID / 企業Airwork PW」(B系, col15/16) のみ。
    「AirWork ID / AirワークPW」(A系, col10/11) は rpo.medica系で
    有効なAW認証ではないため **AWログインに使わない**。
    B系が無い顧客は AWログイン対象外 = None を返す。
    (prefer 引数は後方互換のため残すが無視する = 常にB系)

    Returns:
        (login_id, password, "B") or None
    """
    # プレースホルダ ("ー" 等) は未整備扱い (2026-07-03 10社テストでSSO失敗の一因)
    _PLACEHOLDERS = {"ー", "-", "―", "—", "－", "未設定", "なし"}

    def _clean(v: str) -> str:
        return "" if v.strip() in _PLACEHOLDERS else v

    bid = _clean(_first_credential(_get_field(row, COL_AW_ID_B)))
    bpw = _clean(_first_credential(_get_field(row, COL_AW_PW_B),
                                   split_comma=False))
    if bid and bpw:
        return bid, bpw, "B"
    return None


_COMPANY_PAT = re.compile(r"株式会社|有限会社|合同会社|協同組合")


def _detect_company_key(records: list[dict]) -> str:
    """会社名列のキーを内容で特定 (ヘッダ空=_COLn_ のため位置固定できない)。

    「権限共有」列の挿入で会社名が _COL2_→_COL3_ にズレた実害(2026-07-25)への
    恒久対処。空ヘッダ(_COLn_)キーのうち社名パターン最多のキーを採用。"""
    from collections import Counter
    hits: Counter = Counter()
    for r in records[:300]:
        for k, v in r.items():
            if str(k).startswith("_COL") and _COMPANY_PAT.search(str(v or "")):
                hits[k] += 1
    return hits.most_common(1)[0][0] if hits else COL_COMPANY


def _iter_records(
    records: list[dict], active_only: bool, prefer: str
) -> Iterator[dict]:
    """テスト容易性のため records 引数版を分離 (mock 注入可能)."""
    comp_key = _detect_company_key(records)
    for r in records:
        if active_only:
            closed_v = _get_field(r, COL_CLOSED).upper()
            if closed_v == "TRUE":
                continue
        chosen = _select_credentials(r, prefer=prefer)
        if not chosen:
            continue
        yield {
            "company_name": _get_field(r, comp_key, "会社名"),
            "login_id": chosen[0],
            "password": chosen[1],  # 注: ログ / stdout 出力禁止
            "source": chosen[2],
            # ヘッダ改名('s'→'リクロジアドレス')にも位置(_COL8_)にも耐える二段引き
            "manage_mail": _get_field(
                r, "リクロジアドレス", COL_MANAGE_MAIL).strip().lower(),
        }


# ── シート読取のプロセス内キャッシュ (Sheets API 429根治, 2026-07-09) ──────────
# account_loader は呼ぶたびにシート全体を読んでいた。find_account_by_login_id は
# prefer A/B で2回読むため、aw-collect の per-account 呼出で分間読取上限を突破し
# 429 を招いていた (診断確定)。プロセス内で1回だけ読み、以後は使い回す。
_SHEET_RECORDS_CACHE: Optional[list[dict]] = None


def _load_sheet_records() -> list[dict]:
    """アカウント情報シートを1回だけ読み、records化してキャッシュ."""
    global _SHEET_RECORDS_CACHE
    if _SHEET_RECORDS_CACHE is not None:
        return _SHEET_RECORDS_CACHE
    gc = get_sheets_client()
    sh = sheet_retry(gc.open_by_key, SHEET_ID)
    ws = sheet_retry(sh.worksheet, SHEET_TAB)
    # get_all_records はヘッダ空文字重複で失敗するため get_all_values で自前マッピング
    all_values = sheet_retry(ws.get_all_values)
    records: list[dict] = []
    if all_values:
        # ヘッダ名キー + 位置キー(_COL{i}_) を両方付与 (col8='s'等の不適切ヘッダに堅牢)
        headers = [h if h else f"_COL{i}_" for i, h in enumerate(all_values[0])]
        for row in all_values[1:]:
            rec = dict(zip(headers, row))
            for i, cell in enumerate(row):
                rec[f"_COL{i}_"] = cell  # 位置キーを常に併記
            records.append(rec)
    _SHEET_RECORDS_CACHE = records
    return records


def clear_sheet_cache() -> None:
    """キャッシュ破棄 (長時間プロセスでシート更新を再取得したい場合用)."""
    global _SHEET_RECORDS_CACHE
    _SHEET_RECORDS_CACHE = None


def iter_aw_accounts(
    active_only: bool = True,
    prefer: str = "A",
    *,
    records: Optional[list[dict]] = None,
) -> Iterator[dict]:
    """AW 用 ID/PW が揃った顧客を順次返す.

    Args:
        active_only: True なら クローズ=FALSE のみ
        prefer: "A"=RPO系優先 (AirWork ID + AirワークPW),
                "B"=企業系優先 (企業AirWork ID + 企業Airwork PW)
        records: テスト注入用 (None なら Sheets から実取得)

    Yields:
        {"company_name", "login_id", "password", "source"}
    """
    if prefer not in ("A", "B"):
        raise ValueError(f"prefer は 'A' か 'B': got {prefer!r}")
    if records is None:
        records = _load_sheet_records()  # プロセス内キャッシュ (429根治)
    yield from _iter_records(records, active_only=active_only, prefer=prefer)


def find_account_by_login_id(
    login_id: str,
    *,
    records: Optional[list[dict]] = None,
) -> Optional[dict]:
    """login_id 一致の最初のアカウントを返す (active/closed 問わず両方探索).

    AW CSV fetcher のスタンドアロン実行で PW のみ補完したいケース用。
    """
    for prefer in ("A", "B"):
        for acc in iter_aw_accounts(
            active_only=False, prefer=prefer, records=records
        ):
            if acc["login_id"] == login_id:
                return acc
    return None


def build_manage_mail_index(
    *,
    active_only: bool = True,
    prefer: str = "A",
    records: Optional[list[dict]] = None,
) -> dict[str, dict]:
    """管理メール(rpo.medica+xxx, 小文字) → AWアカウント の索引を返す.

    Deal.kanri_mail_address からAWアカウント(login_id/password)を引くための結合表。
    src A/B 両対応 (col8 管理メールが共通キー, 2026-07-03)。
    同一メールが複数行にある場合は最初の1件を採用。
    """
    idx: dict[str, dict] = {}
    for acc in iter_aw_accounts(
        active_only=active_only, prefer=prefer, records=records
    ):
        mm = (acc.get("manage_mail") or "").strip().lower()
        if mm and mm not in idx:
            idx[mm] = acc
    return idx


def resolve_accounts_for_mails(
    mails: Iterable[str],
    *,
    active_only: bool = True,
    prefer: str = "A",
    records: Optional[list[dict]] = None,
) -> tuple[list[dict], list[str]]:
    """管理メール集合 → (解決できたAWアカウント一覧, 未解決メール一覧).

    Deal起点の巡回対象抽出に使う: アクティブDealの kanri_mail_address 群を渡すと、
    対応するAWアカウント(現役顧客のみ)を返す。シート順依存を排除する。
    """
    idx = build_manage_mail_index(
        active_only=active_only, prefer=prefer, records=records
    )
    seen, hit, miss = set(), [], []
    for m in mails:
        key = (m or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        acc = idx.get(key)
        if acc:
            hit.append(acc)
        else:
            miss.append(key)
    return hit, miss
