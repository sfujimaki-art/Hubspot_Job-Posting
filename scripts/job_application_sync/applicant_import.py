"""応募CSV (HR/AW由来) を APPOINTMENT (0-421) に取込み、LISTING (0-420) と紐付ける。

============================================================
v0.2 §24 遵守ガード (最重要)
============================================================
§24-1: 求人未特定時にタイトル類似度マッチングで勝手に紐付けない。
§24-2: 求人未特定時に本文/職種類似度で勝手に紐付けない。
求人未特定 (LISTING 検索ヒットゼロ) の応募は必ず:
  - APPOINTMENT を kokyakushiitotenkijoukyou="対象外" + 内部メモ「求人未特定」で作成
  - Association は **張らない** (タイトル名寄せ等で代替紐付けしない)
  - 別ログ unlinked_applicants_{timestamp}.json に出力し人間レビューへ
本ファイル全体で「類似度」「name match」「fuzzy」関数は実装しないこと。
============================================================

Phase 0 確定事項 (docs/wbs_outputs/1.11.9_媒体CSV同期実装/Phase0_*.md):
- Association typeId=5 (USER_DEFINED 「応募先求人」, APPT→LISTING)
- Association typeId=6 (USER_DEFINED 「応募」, LISTING→APPT)
- 媒体求人ID は APPOINTMENT 側に持たない (LISTING側 id_hrhakkaa/id_airwork 検索でルックアップ)
- AW LISTING は id_airwork + airwork_account_login_id の複合ユニーク
- oubobaitaimei: HRハッカー / AirWork / jobギア / ジモティー / Indeed / ぶるる /
                 エンゲージ / ジョブオプLite / 求人ボックス / ガテン系求人 / その他 / 未設定

CLI:
  --csv <path>    必須。応募CSV (cp932/utf-8 両対応試行)
  --dry-run       既定。HubSpot書込を行わずプランを JSON 出力。
  --actual        実際にHubSpotへ書込。
  --limit <N>     先頭N行のみ処理 (テスト用)。
  --login-id <s>  AW時の client_code (例: 1658755)。LISTING の airwork_account_login_id に
                  入っている値 (SSO email ではない。aw_orchestrator の _extract_client_code 参照)。
                  CSV行側の airwork_account_login_id が空の場合のフォールバックとして使う。

AW LISTING 突合ルール (レビューHIGH是正 2026-07-03):
  - login_id あり → (id_airwork AND airwork_account_login_id) 複合キー検索
  - login_id 無し → id_airwork 単独 EQ 検索 + 一意性ガード:
      ヒット0 → unlinked / ヒット1 → linked /
      ヒット2以上 → unlinked + "ambiguous: N listings matched, login_id必須"
    (顧客横断の誤紐付け絶対禁止 = v0.2 §24 準拠)

入力CSVスキーマ:
  汎用: 応募者氏名, カナ, 電話, メール, 応募日, 媒体名, 媒体求人ID, airwork_account_login_id
  実媒体生ヘッダ (2026-07-03 実物確認済) も直接受付可:
    - HRハッカー応募CSV (cp932, 33列): 応募者id/応募求人先/名前/... → HR_APPLICANT_COLUMNS で変換
    - AirWORK応募CSV (utf-8 BOM, 59列): 応募ID/応募求人ID/応募者名/... → AW_APPLICANT_COLUMNS で変換
  detect_media_from_header がヘッダから自動判定。汎用ヘッダは従来通り無変換。

出力:
  scripts/job_application_sync/logs/applicant_import_{ts}.json
  scripts/job_application_sync/logs/unlinked_applicants_{ts}.json (求人未特定のみ)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol


# ---- Association type IDs (Phase 0 実測確定) ------------------------------
ASSOC_TYPE_ID_APPT_TO_LISTING = 5  # USER_DEFINED 「応募先求人」
ASSOC_TYPE_ID_LISTING_TO_APPT = 6  # USER_DEFINED 「応募」

# ---- oubobaitaimei enum 値 (Phase 0 実測 12択) -----------------------------
OUBO_BAITAI_VALUES = {
    "HRハッカー", "AirWork", "jobギア", "ジモティー", "Indeed", "ぶるる",
    "エンゲージ", "ジョブオプLite", "求人ボックス", "ガテン系求人", "その他", "未設定",
}

# ---- 入力CSVの媒体名表記ゆれを enum 正規値に正規化 -------------------------
MEDIA_NAME_ALIAS = {
    "hrハッカー": "HRハッカー",
    "hr hacker": "HRハッカー",
    "hrhacker": "HRハッカー",
    "airwork": "AirWork",
    "air work": "AirWork",
    "エアワーク": "AirWork",
}

# ---- 必須ヘッダ (暫定。Phase 0b で実物確認後に確定) -----------------------
REQUIRED_CSV_COLS = ["応募者氏名", "電話", "メール", "応募日", "媒体名", "媒体求人ID"]

# ============================================================================
# 実媒体CSV (生ヘッダ) → 汎用スキーマ マッピング層 (2026-07-03 実CSVヘッダで確定)
# ============================================================================
# HRハッカー応募CSV (cp932, 33列)。生列名 → 汎用キー。
# キーは媒体求人ID (応募求人先) のみ。タイトル/本文は使わない (v0.2 §24)。
HR_APPLICANT_COLUMNS = {
    "応募求人先": "媒体求人ID",
    "名前": "応募者氏名",
    "名前フリガナ": "カナ",
    "電話番号": "電話",
    "メールアドレス": "メール",
    "応募日時": "応募日",
}

# AirWORK応募CSV (utf-8 BOM, 59列, 全セル二重引用符)。生列名 → 汎用キー。
AW_APPLICANT_COLUMNS = {
    "応募求人ID": "媒体求人ID",
    "応募者名": "応募者氏名",
    "ふりがな": "カナ",
    "電話番号": "電話",
    "メールアドレス": "メール",
    "応募日時": "応募日",
}

# detect_media_from_header の戻り値 → 列マッピング
RAW_MEDIA_COLUMN_MAPS = {
    "HRハッカー": HR_APPLICANT_COLUMNS,
    "AirWORK": AW_APPLICANT_COLUMNS,
}


def detect_media_from_header(header_cols: Iterable[str]) -> Optional[str]:
    """生媒体CSVのヘッダから媒体を判定する純関数。

    - 「応募者id」+「応募求人先」あり → "HRハッカー"
    - 「応募ID」+「応募求人ID」あり → "AirWORK"
    - どちらでもない → None (既存汎用スキーマとして処理継続)
    """
    cols = set(header_cols)
    if "応募者id" in cols and "応募求人先" in cols:
        return "HRハッカー"
    if "応募ID" in cols and "応募求人ID" in cols:
        return "AirWORK"
    return None


def _convert_raw_media_rows(raw_rows: list[dict], media: str) -> list[dict]:
    """生媒体CSV行を汎用スキーマ行に変換する。媒体名は固定値で付与。"""
    colmap = RAW_MEDIA_COLUMN_MAPS[media]
    out = []
    for r in raw_rows:
        g = {generic: str(r.get(raw) or "").strip() for raw, generic in colmap.items()}
        g["媒体名"] = media  # normalize_media で enum 正規値 (AirWORK→AirWork) に揃う
        out.append(g)
    return out


# ============================================================================
# データクラス
# ============================================================================
@dataclass
class ApplicantRow:
    """正規化済み応募1行。"""
    name: str
    kana: str
    phone: str
    email: str
    apply_date: str            # YYYY-MM-DD
    media: str                 # 正規化済み (oubobaitaimei の値)
    media_job_id: str
    airwork_login_id: str = ""  # AW のみ
    raw_lineno: int = 0


@dataclass
class ProcessResult:
    status: str                # 'linked' / 'unlinked' / 'skip_duplicate' / 'error'
    applicant_key: str
    media: str
    media_job_id: str
    listing_id: Optional[str] = None
    appointment_id: Optional[str] = None
    appointment_properties: dict = field(default_factory=dict)
    message: str = ""


# ============================================================================
# HubSpot クライアント プロトコル (テストでモック差替え用)
# ============================================================================
class HubSpotClient(Protocol):
    def search_listing_hr(self, media_job_id: str) -> Optional[str]: ...
    def search_listing_aw(self, media_job_id: str, airwork_login_id: str) -> Optional[str]: ...
    def search_listing_aw_by_id(self, media_job_id: str) -> list[str]: ...
    def find_existing_appointment(
        self, media: str, media_job_id: str, phone: str, email: str, apply_date: str = ""
    ) -> Optional[str]: ...
    def create_appointment(self, properties: dict) -> str: ...
    def associate_appointment_to_listing(self, appointment_id: str, listing_id: str) -> None: ...


# ============================================================================
# ドライランクライアント (--dry-run 既定で使用)
# ============================================================================
class DryRunClient:
    """HubSpot書込なし。検索結果はインメモリ辞書から返す。

    テスト/dry-run共用。本番呼出を行わないので副作用なし。
    """
    def __init__(
        self,
        listings_hr: Optional[dict] = None,   # {id_hrhakkaa: listing_id}
        listings_aw: Optional[dict] = None,   # {(id_airwork, login_id): listing_id}
        existing_appts: Optional[dict] = None,
        # existing_appts のキー形式 (2026-07-03 dedup修正後):
        #   (media, apply_date, "email", normalize_email(email)) または
        #   (media, apply_date, "phone", format_jp_phone(phone))
        # 照合セマンティクスは RealHubSpotClient.find_existing_appointment と同一:
        #   media 必須一致 / apply_date 非空なら一致必須 / email優先・phoneフォールバック
    ):
        self.listings_hr = listings_hr or {}
        self.listings_aw = listings_aw or {}
        self.existing_appts = existing_appts or {}
        self.search_log: list = []
        self.created_appts: list = []
        self.associations: list = []
        self._next_id = 1000000

    def search_listing_hr(self, media_job_id: str) -> Optional[str]:
        self.search_log.append({"op": "search_listing_hr", "key": {"id_hrhakkaa": media_job_id}})
        return self.listings_hr.get(media_job_id)

    def search_listing_aw(self, media_job_id: str, airwork_login_id: str) -> Optional[str]:
        # 必ず id_airwork + airwork_account_login_id の両方を使う (v0.2 AW LISTING 複合ユニーク)
        self.search_log.append({
            "op": "search_listing_aw",
            "key": {"id_airwork": media_job_id, "airwork_account_login_id": airwork_login_id},
        })
        return self.listings_aw.get((media_job_id, airwork_login_id))

    def search_listing_aw_by_id(self, media_job_id: str) -> list[str]:
        # login_id 無し時の id_airwork 単独検索 (一意性ガードは呼出側で実施)
        self.search_log.append({
            "op": "search_listing_aw_by_id",
            "key": {"id_airwork": media_job_id},
        })
        return [lid for (jid, _login), lid in self.listings_aw.items() if jid == media_job_id]

    def find_existing_appointment(
        self, media: str, media_job_id: str, phone: str, email: str, apply_date: str = ""
    ) -> Optional[str]:
        em = normalize_email(email)
        ph = format_jp_phone(phone) if phone else ""
        if not em and not ph:
            return None  # dedup不能 → 作成に進む (Real側と同一セマンティクス)
        for key, appt_id in self.existing_appts.items():
            k_media, k_date, k_kind, k_val = key
            if k_media != media:
                continue
            if apply_date and k_date != apply_date:
                continue
            if em:
                if k_kind == "email" and k_val == em:
                    return appt_id
            else:
                if k_kind == "phone" and k_val == ph:
                    return appt_id
        return None

    def create_appointment(self, properties: dict) -> str:
        self._next_id += 1
        appt_id = f"DRY-{self._next_id}"
        self.created_appts.append({"id": appt_id, "properties": properties})
        return appt_id

    def associate_appointment_to_listing(self, appointment_id: str, listing_id: str) -> None:
        self.associations.append({
            "appointment_id": appointment_id,
            "listing_id": listing_id,
            "type_id": ASSOC_TYPE_ID_APPT_TO_LISTING,
        })


# ============================================================================
# 本番クライアント (HubSpot REST API)
# ============================================================================
class RealHubSpotClient:
    """実HubSpot REST API クライアント。--actual 指定時のみ使用。"""
    BASE = "https://api.hubapi.com"

    def __init__(self, token: str):
        # 遅延 import (テスト時に requests/dotenv 不要)
        import requests  # noqa
        self._requests = requests
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _post(self, path: str, body: dict) -> Any:
        for attempt in range(3):
            r = self._requests.post(f"{self.BASE}{path}", headers=self.headers, json=body, timeout=60)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return r  # type: ignore[name-defined]

    def search_listing_hr(self, media_job_id: str) -> Optional[str]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "id_hrhakkaa", "operator": "EQ", "value": media_job_id},
            ]}],
            "properties": ["hs_object_id"],
            "limit": 2,
        }
        r = self._post("/crm/v3/objects/0-420/search", body)
        if r.status_code >= 300:
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None

    def search_listing_aw(self, media_job_id: str, airwork_login_id: str) -> Optional[str]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "id_airwork", "operator": "EQ", "value": media_job_id},
                {"propertyName": "airwork_account_login_id", "operator": "EQ", "value": airwork_login_id},
            ]}],
            "properties": ["hs_object_id"],
            "limit": 2,
        }
        r = self._post("/crm/v3/objects/0-420/search", body)
        if r.status_code >= 300:
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None

    def search_listing_aw_by_id(self, media_job_id: str) -> list[str]:
        """login_id 無し時の id_airwork 単独 EQ 検索。ヒットID全件を返す (一意性ガードは呼出側)。"""
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "id_airwork", "operator": "EQ", "value": media_job_id},
            ]}],
            "properties": ["hs_object_id"],
            "limit": 10,
        }
        r = self._post("/crm/v3/objects/0-420/search", body)
        if r.status_code >= 300:
            return []
        # limit=10 で切り捨てられても、2件以上返れば呼出側の ambiguous 判定は成立する
        return [x["id"] for x in r.json().get("results", [])]

    def find_existing_appointment(
        self, media: str, media_job_id: str, phone: str, email: str, apply_date: str = ""
    ) -> Optional[str]:
        """重複APPOINTMENT検索 (2026-07-03 実測に基づく修正).

        実測事実:
        - denwaseikika は Search API の EQ フィルタに使うと検索自体がエラー
          (読取専用/計算プロパティ) → 使用禁止。
        - meeruadoresu の EQ フィルタは正常動作 (実ヒット確認済)。
        - denwabangou には format_jp_phone 整形後の値が格納されている。

        照合: oubobaitaimei 必須 + yingmuri (apply_date 非空時。別日の別求人応募を
        誤dedupしないため) + email優先 / email空なら denwabangou フォールバック。
        email も phone も空なら dedup不能 → None (作成に進む)。
        """
        em = normalize_email(email)
        ph = format_jp_phone(phone) if phone else ""
        if not em and not ph:
            return None
        filters = [{"propertyName": "oubobaitaimei", "operator": "EQ", "value": media}]
        if apply_date:
            # date型プロパティのEQは "YYYY-MM-DD" 文字列だと検索400 (既知教訓:
            # saishuu_csv_kenshutsu_bi でも同事象)。UTC深夜0時の epoch millis に変換必須。
            try:
                from datetime import datetime as _dt, timezone as _tz
                ms = int(_dt.strptime(apply_date, "%Y-%m-%d")
                         .replace(tzinfo=_tz.utc).timestamp() * 1000)
                filters.append({"propertyName": "yingmuri", "operator": "EQ",
                                "value": str(ms)})
            except ValueError:
                pass  # 不正日付はフィルタに含めない (media+email/phone で照合)
        if em:
            filters.append({"propertyName": "meeruadoresu", "operator": "EQ", "value": em})
        else:
            # 格納値は format_jp_phone 整形済のため、dedup検索も同形式で照合
            filters.append({"propertyName": "denwabangou", "operator": "EQ", "value": ph})
        body = {
            "filterGroups": [{"filters": filters}],
            "properties": ["hs_object_id"],
            "limit": 2,
        }
        r = self._post("/crm/v3/objects/0-421/search", body)
        if r.status_code >= 300:
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None

    def create_appointment(self, properties: dict) -> str:
        r = self._post("/crm/v3/objects/0-421", {"properties": properties})
        if r.status_code == 400 and "INVALID_PHONE_NUMBER" in r.text:
            # フォールバック: 電話が依然不正なら電話抜きで作成し備考へ退避 (レコード喪失防止)
            p2 = {k: v for k, v in properties.items() if k != "denwabangou"}
            note = p2.get("bikou_hiaringu", "")
            raw_phone = properties.get("denwabangou", "")
            p2["bikou_hiaringu"] = ((note + " / ") if note else "") + \
                f"電話番号(HubSpot形式不正で退避): {raw_phone}"
            r = self._post("/crm/v3/objects/0-421", {"properties": p2})
        r.raise_for_status()
        return r.json()["id"]

    def associate_appointment_to_listing(self, appointment_id: str, listing_id: str) -> None:
        body = [{"associationCategory": "USER_DEFINED",
                 "associationTypeId": ASSOC_TYPE_ID_APPT_TO_LISTING}]
        url = (f"{self.BASE}/crm/v4/objects/0-421/{appointment_id}"
               f"/associations/0-420/{listing_id}")
        r = self._requests.put(url, headers=self.headers, json=body, timeout=60)
        r.raise_for_status()


# ============================================================================
# 正規化ヘルパ
# ============================================================================
def normalize_phone(s: str) -> str:
    """電話番号: 数字のみ抽出。空文字は空のまま。"""
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


def format_jp_phone(s: str) -> str:
    """HubSpot電話型(denwabangou)が受理する国番号付き E.164 形式へ整形.

    2026-07-07 実書込テストで判明: HubSpot denwabangou は国番号(+81)必須。
    国内形式(080-1234-5678 / 08012345678)は INVALID_PHONE_NUMBER で400、
    フォールバックで電話がドロップされ全応募の電話が消えていた → +81 に修正。
    - 0始まり 10/11桁 → 先頭0を除き +81 を付与 (E.164)  例 08012345678 → +818012345678
    - 既に 81始まり → + を付与
    - それ以外は原文 strip (冪等)
    """
    digits = re.sub(r"\D", "", str(s or ""))
    if not digits:
        return str(s or "").strip()
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+81" + digits[1:]
    if digits.startswith("81") and len(digits) in (11, 12):
        return "+" + digits
    return str(s or "").strip()


def normalize_email(s: str) -> str:
    if not s:
        return ""
    return str(s).strip().lower()


def normalize_media(s: str) -> str:
    """媒体名を oubobaitaimei enum 正規値に。未マッチは原文 strip 返却。"""
    if not s:
        return "未設定"
    raw = str(s).strip()
    if raw in OUBO_BAITAI_VALUES:
        return raw
    low = raw.lower().replace(" ", "")
    return MEDIA_NAME_ALIAS.get(low, raw)


def normalize_date(s: str) -> str:
    """応募日: YYYY-MM-DD で返す。失敗時は原文 strip 返却。"""
    if not s:
        return ""
    raw = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):  # 実媒体CSVの応募日時 (時刻付き)
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


# ============================================================================
# CSV 読込
# ============================================================================
def load_applicants_csv(path: Path) -> list[ApplicantRow]:
    """応募CSVを読込み、正規化済 ApplicantRow リストを返す。

    エンコーディングは utf-8-sig strict → 失敗なら cp932 の順で自動判定
    (AW=UTF-8 BOM / HR=Shift-JIS 実物確認済 2026-07-03)。

    ヘッダが実媒体CSVの生ヘッダ (HRハッカー/AirWORK) なら汎用スキーマに
    変換してから処理する。汎用ヘッダなら従来通り。後段 (run_import 等) は無改変。
    """
    raw_rows: list[dict] = []
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                raw_rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError as e:
            last_err = e
            continue
    else:
        raise RuntimeError(f"CSV decode failed: {last_err}")

    if raw_rows:
        raw_media = detect_media_from_header(raw_rows[0].keys())
        if raw_media is not None:
            raw_rows = _convert_raw_media_rows(raw_rows, raw_media)
        missing = [c for c in REQUIRED_CSV_COLS if c not in raw_rows[0]]
        if missing:
            raise ValueError(f"必須列が欠落: {missing} (実際の列: {list(raw_rows[0].keys())})")

    out = []
    for i, r in enumerate(raw_rows, start=2):  # 2 = header行を1としてデータ先頭
        out.append(ApplicantRow(
            name=str(r.get("応募者氏名") or "").strip(),
            kana=str(r.get("カナ") or "").strip(),
            phone=normalize_phone(r.get("電話")),
            email=normalize_email(r.get("メール")),
            apply_date=normalize_date(r.get("応募日")),
            media=normalize_media(r.get("媒体名")),
            media_job_id=str(r.get("媒体求人ID") or "").strip(),
            airwork_login_id=str(r.get("airwork_account_login_id") or "").strip(),
            raw_lineno=i,
        ))
    return out


# ============================================================================
# APPOINTMENT プロパティ構築
# ============================================================================
def build_appointment_properties(row: ApplicantRow, *, linked: bool) -> dict:
    """ApplicantRow から APPOINTMENT 書込用プロパティを構築。

    linked=False (求人未特定) の場合:
      kokyakushiitotenkijoukyou="対象外" + bikou_hiaringu に「求人未特定」記録
    """
    phone_fmt = format_jp_phone(row.phone)
    props = {
        "hs_appointment_name": row.name,
        "ouboshashimei": row.name,
        "ouboshashimei_kana": row.kana,
        "denwabangou": phone_fmt,
        # denwaseikika は書込黙殺 (読取専用/計算プロパティ、格納値None実測) のため送らない
        "meeruadoresu": row.email,
        "yingmuri": row.apply_date,
        "oubobaitaimei": row.media,
        # 媒体求人ID を応募求人メモに保持 (全応募)。求人が後から出来た時の
        # 再紐付けスイープ(applicant_sync --relink)がこの値でLISTINGを引く。
        "oubokyuujinmemo": row.media_job_id,
    }
    if not linked:
        # v0.2 §24 ガード: タイトル類似で勝手紐付けせず人間レビューへ
        props["kokyakushiitotenkijoukyou"] = "対象外"
        props["bikou_hiaringu"] = (
            f"求人未特定: 媒体={row.media} 媒体求人ID={row.media_job_id} "
            f"(applicant_import 取込時にLISTING検索ヒットゼロ。"
            f"v0.2 §24-1 §24-2 によりタイトル類似度等での自動紐付けは禁止。人間確認要。)"
        )
    # 空文字プロパティは除外 (HubSpot側で「空書込で既存値消去」を避ける)
    return {k: v for k, v in props.items() if v != ""}


# ============================================================================
# 主要処理
# ============================================================================
def process_applicant(
    row: ApplicantRow, client: HubSpotClient, default_login_id: str = ""
) -> ProcessResult:
    """1応募行を処理。LISTING検索 → 重複検出 → APPOINTMENT 作成 → Association。

    §24 ガード: 求人未特定時は Association を張らずに unlinked 扱い。
    default_login_id: CLI --login-id (AW時の client_code)。CSV行側の
    airwork_account_login_id が空のときのフォールバック。HR経路では使わない。
    """
    applicant_key = f"L{row.raw_lineno}:{row.name}/{row.phone}/{row.email}"

    # 1. LISTING検索 (媒体別)
    listing_id: Optional[str] = None
    unlinked_message = "LISTING検索ヒットゼロ → §24ガードに従いAssociation張らず"
    if row.media == "HRハッカー":
        if row.media_job_id:
            listing_id = client.search_listing_hr(row.media_job_id)
    elif row.media == "AirWork":
        if row.media_job_id:
            login_id = row.airwork_login_id or default_login_id
            if login_id:
                # 本来の複合キー (id_airwork AND airwork_account_login_id) 検索
                listing_id = client.search_listing_aw(row.media_job_id, login_id)
            else:
                # login_id 無し → id_airwork 単独検索 + 一意性ガード
                candidates = client.search_listing_aw_by_id(row.media_job_id)
                if len(candidates) == 1:
                    listing_id = candidates[0]
                elif len(candidates) >= 2:
                    # 顧客横断の誤紐付け絶対禁止 (v0.2 §24) → unlinked
                    unlinked_message = (
                        f"ambiguous: {len(candidates)} listings matched, login_id必須"
                    )
    # その他媒体は LISTING 検索仕様未確立 → 求人未特定扱いで unlinked

    # 2. 重複応募検出
    existing = client.find_existing_appointment(
        row.media, row.media_job_id, row.phone, row.email, apply_date=row.apply_date
    )
    if existing:
        return ProcessResult(
            status="skip_duplicate",
            applicant_key=applicant_key,
            media=row.media,
            media_job_id=row.media_job_id,
            listing_id=listing_id,
            appointment_id=existing,
            message=f"既存APPOINTMENT {existing} と (media, media_job_id, 電話, メール) 一致",
        )

    # 3. APPOINTMENT 作成
    props = build_appointment_properties(row, linked=listing_id is not None)
    appt_id = client.create_appointment(props)

    # 4. Association (求人特定時のみ)
    if listing_id:
        client.associate_appointment_to_listing(appt_id, listing_id)
        return ProcessResult(
            status="linked", applicant_key=applicant_key,
            media=row.media, media_job_id=row.media_job_id,
            listing_id=listing_id, appointment_id=appt_id,
            appointment_properties=props,
        )
    else:
        return ProcessResult(
            status="unlinked", applicant_key=applicant_key,
            media=row.media, media_job_id=row.media_job_id,
            listing_id=None, appointment_id=appt_id,
            appointment_properties=props,
            message=unlinked_message,
        )


def run_import(
    rows: Iterable[ApplicantRow], client: HubSpotClient, default_login_id: str = ""
) -> list[ProcessResult]:
    results = []
    for row in rows:
        try:
            results.append(process_applicant(row, client, default_login_id))
        except Exception as e:
            results.append(ProcessResult(
                status="error",
                applicant_key=f"L{row.raw_lineno}:{row.name}",
                media=row.media, media_job_id=row.media_job_id,
                message=f"{type(e).__name__}: {e}",
            ))
    return results


def summarize(results: list[ProcessResult]) -> dict:
    summary = {"total": len(results), "linked": 0, "unlinked": 0,
               "skip_duplicate": 0, "error": 0}
    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
    return summary


# ============================================================================
# CLI
# ============================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="応募CSV → APPOINTMENT 取込 (WBS 1.11.9 B-1)")
    p.add_argument("--csv", required=True, type=Path, help="応募CSV path")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                      help="既定。HubSpot書込なしでプラン出力。")
    mode.add_argument("--actual", dest="dry_run", action="store_false",
                      help="HubSpotへ実書込。")
    p.add_argument("--limit", type=int, default=None, help="先頭N行のみ処理")
    p.add_argument("--login-id", dest="login_id", default="",
                   help="AW時の client_code (例: 1658755)。LISTING の airwork_account_login_id 値。"
                        "CSV行側が空のときのフォールバック。無指定時は id_airwork 単独検索+一意性ガード。")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    rows = load_applicants_csv(args.csv)
    if args.limit:
        rows = rows[:args.limit]

    if args.dry_run:
        client: HubSpotClient = DryRunClient()
        mode_label = "DRY-RUN"
    else:
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        except ImportError:
            pass
        token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
        if not token:
            print("ERROR: HUBSPOT_ACCESS_TOKEN env var not set", file=sys.stderr)
            return 2
        client = RealHubSpotClient(token)
        mode_label = "ACTUAL"

    results = run_import(rows, client, default_login_id=args.login_id)
    summary = summarize(results)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"applicant_import_{ts}.json"
    log_path.write_text(
        json.dumps({
            "mode": mode_label,
            "csv": str(args.csv),
            "summary": summary,
            "results": [asdict(r) for r in results],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    unlinked = [asdict(r) for r in results if r.status == "unlinked"]
    if unlinked:
        unlinked_path = log_dir / f"unlinked_applicants_{ts}.json"
        unlinked_path.write_text(
            json.dumps(unlinked, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    print(f"[{mode_label}] 応募 {summary['total']} 件処理 / "
          f"紐付け成功 {summary['linked']} / 求人未特定 {summary['unlinked']} / "
          f"重複skip {summary['skip_duplicate']} / error {summary['error']}")
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
