"""A-1: HRハッカーCSV取込 → LISTING (0-420) upsert.

設計準拠:
- ~/Downloads/hubspot_job_application_design_for_ai_v0.2.md §10, §11, §23.1
- docs/wbs_outputs/1.11.9_媒体CSV同期実装/Phase0_LISTING_実測.md
- 参照ベース: scripts/job_listing_hubspot_match/create_new_listings_v3.py,
              scripts/job_listing_hubspot_match/update_listing_hrhacker_info.py

================================================================================
v0.2 §24 やってはいけない設計 (遵守ガード)
================================================================================
本実装は以下の原則を厳守する:
  1. 媒体間でタイトル名寄せを行わない (id_hrhakkaa 1次キーのみ)
  2. 媒体間で本文類似度名寄せを行わない
  3. HRはCSV未検出を「公開終了」と判断しない (CSVに居なければ単に未検出。
     HubSpot側のステータスは変更しない。本スクリプトはCSV内のレコードのみ処理)
  4. AW新規は最初から弊社管理にしない (本スクリプトはHR専用なのでHR新規は弊社管理でOK)
  5. 全求人数を契約求人数として扱わない (契約求人数 = 媒体=HRハッカー AND 公開中)
  6. その他媒体に同精度要求しない (本スクリプトはHR専用)
  7. 人手3媒体リアルタイム更新前提にしない (定期バッチ前提)
================================================================================

ステータス正規化 (v0.2 §10):
  公開開始前 → 公開前
  公開       → 公開中
  公開終了   → 公開終了
  非公開     → None ("契約求人数除外" = HubSpot共通ステータスは未設定 or 要定義)

CLI:
  python hrhacker_import.py --csv <path> [--dry-run|--actual] [--limit N]

出力:
  dry-run: 更新/作成予定をJSON + サマリ (X件処理, Y更新, Z新規, W未検出)
  actual:  HubSpot API実行 + 同一形式ログ
  ログ:    scripts/job_application_sync/logs/hrhacker_import_{timestamp}.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

try:  # パッケージ実行/スクリプト直実行の両対応 (CIは直実行)
    from . import listing_stage as _stage
    from . import private_log as plog
except ImportError:  # pragma: no cover
    import listing_stage as _stage  # type: ignore
    import private_log as plog  # type: ignore

# ============================================================================
# 環境設定
# ============================================================================
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# .env からトークン読込 (テスト時は読込不要なので存在チェックのみ)
_ENV_PATH = REPO / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

BASE = "https://api.hubapi.com"


def _headers() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ============================================================================
# CSV列マッピング (Phase 0b 確定後に書換え可能)
# ============================================================================
# Phase 0b 実測確定 (2026-06-26): HR ハッカー CSV (Shift-JIS / 84列) 列名 → 内部キー
# 出典: docs/wbs_outputs/1.11.9_媒体CSV同期実装/Phase0b_HR_CSV実測_2026-06-26.md
HR_CSV_COLUMNS: dict[str, str] = {
    "求人id": "media_job_id",         # idx 0
    "店舗id": "shop_id",              # idx 1 (LISTING直接保持先は未確定、F0後追い検討)
    "案件名": "job_name",             # idx 3 (= 求人名)
    "公開開始日時": "start_date",      # idx 81
    "公開終了日時": "end_date",        # idx 82
    "公開": "original_status",         # idx 83 (値: 公開/非公開/公開開始前/公開終了)
}
# 求人票の本文・画像 (2026-10-08): 求人文面管理 (HR_HR /app/job-copy) が本文・給与・画像の
# 変化を版として比べるため、CSV の求人票の列を 2 つのプロパティにまとめて書く。
# HubSpot のプロパティ履歴がそのまま版の履歴になるので、値が変わったときだけ書く。
# 載せるのは求人票として公開される列だけ。制作メモ・電話番号・連絡先/通知先メール・
# フォーム設定・ID・公開日時/状態は入れない (社内メモと連絡先を求人票の履歴に混ぜない)。
COPY_TEXT_COLUMNS: tuple[str, ...] = (
    "案件名", "仕事内容", "通勤経路", "最寄り駅", "キャッチコピー", "メリット",
    "仕事情報補足1のタイトル", "仕事情報補足2のタイトル", "仕事情報補足3のタイトル", "仕事情報補足4のタイトル",
    "仕事情報補足1の内容", "仕事情報補足2の内容", "仕事情報補足3の内容", "仕事情報補足4の内容",
    "雇用形態", "Indeed表示職種名", "応募資格", "給与形態", "基本給与 最小", "基本給与 最大",
    "タスクの所要時間", "タスクの単位", "平均稼働時間", "平均稼働日数", "固定残業代", "想定残業時間",
    "条件付き給与1 条件", "条件付き給与1 深夜帯", "条件付き給与1 最小給与", "条件付き給与1 最大給与",
    "条件付き給与2 条件", "条件付き給与2 深夜帯", "条件付き給与2 最小給与", "条件付き給与2 最大給与",
    "条件付き給与3 条件", "条件付き給与3 深夜帯", "条件付き給与3 最小給与", "条件付き給与3 最大給与",
    "給与補足", "試用・研修の有無", "試用・研修時の雇用条件", "試用・研修期の雇用形態",
    "試用・研修期の給与のタイプ", "試用・研修期の基本給与 最小", "試用・研修期の基本給与 最大",
    "試用・研修期のタスクの所要時間", "試用・研修期のタスクの単位", "試用・研修期の平均稼働時間",
    "試用・研修期の平均稼働日数", "試用・研修期の固定残業代", "試用・研修期の想定残業時間",
    "試用・研修の詳細情報", "勤務時間", "勤務時間帯",
    "自由項目1のタイトル", "自由項目2のタイトル", "自由項目3のタイトル", "自由項目4のタイトル",
    "自由項目1の内容", "自由項目2の内容", "自由項目3の内容", "自由項目4の内容",
    "受動喫煙対策", "受動喫煙についての補足情報", "応募方法", "応募後のプロセス", "採用予定人数",
)
COPY_IMAGE_COLUMNS: tuple[str, ...] = ("画像1", "画像2", "画像3")
PROP_COPY_BODY = "hrh_kyuujinhyou_honbun"     # 求人票の本文（HRハッカー）
PROP_COPY_IMAGES = "hrh_kyuujinhyou_gazou"    # 求人票の画像（HRハッカー）
# HubSpot の文字列プロパティの上限 (65,536 文字) を超える値は、切り詰めず書かない
# (切った値を書くと、変わっていない本文が「変わった」履歴になる)。
COPY_MAX_CHARS = 65_000


def _clean(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def compose_copy_body(raw: dict, header: list[str]) -> Optional[str]:
    """求人票の文章系の列を決まった順の全文にする. CSV にその列が 1 つも無ければ None.

    1 列 = 「列名：値」。値が複数行なら「列名：」の次の行から値。空の列は出さない。
    """
    present = [c for c in COPY_TEXT_COLUMNS if c in header]
    if not present:
        return None
    blocks = []
    for col in present:
        v = _clean(raw.get(col))
        if not v:
            continue
        blocks.append(f"{col}：\n{v}" if "\n" in v else f"{col}：{v}")
    return "\n".join(blocks)


def compose_copy_images(raw: dict, header: list[str]) -> Optional[str]:
    """画像1〜3 を並び順のまま 1 行ずつ。空の枠は「なし」。CSV に画像列が無ければ None."""
    if not all(c in header for c in COPY_IMAGE_COLUMNS):
        return None
    return "\n".join(f"{c}：{_clean(raw.get(c)) or 'なし'}" for c in COPY_IMAGE_COLUMNS)


def copy_props(row: dict, existing_props: Optional[dict]) -> dict:
    """本文・画像のうち、今の HubSpot の値と違うものだけを返す (同じ値は書かない)."""
    p: dict = {}
    for key, prop in (("copy_body", PROP_COPY_BODY), ("copy_images", PROP_COPY_IMAGES)):
        value = row.get(key)
        if value is None or len(value) > COPY_MAX_CHARS:
            continue
        current = (existing_props or {}).get(prop)
        if (current or "") != value:
            p[prop] = value
    return p


# CSVエンコーディング: Shift-JIS (BOMなし) — Phase 0b 28382 実測で確定
HR_CSV_ENCODING = "shift_jis"

# 内部キー → HubSpotプロパティ内部名 (Phase 0 実測準拠)
HUBSPOT_PROPERTY_MAP: dict[str, str] = {
    "media_job_id": "id_hrhakkaa",
    "job_name": "hs_name",
    # 媒体原ステータス・正規化ステータス・管理区分等は処理内で個別に組立
}

# 媒体固定値
MEDIA_NAME = "HRハッカー"
URL_TMPL = "https://hr-hacker.com/f-a-c-rikurozi/job-offers/show/{}"

# 新規プロパティ (Phase 0 で「新規必須」とされた内部名提案 - Phase 1 で作成後利用)
PROP_MEDIA_ORIG_STATUS = "baitai_genjoukyou_hrhakkaa"   # 媒体原ステータス_HRハッカー
PROP_HS_KYUUJIN_STATUS = "kyuujin_status"               # HubSpot求人ステータス (公開前/公開中/公開終了) — hs_予約語回避でリネーム (2026-06-26)
PROP_KANRI_KUBUN = "kanri_kubun"                        # 管理区分 (弊社管理/未判定/...)
PROP_TORIKOMI_RIYUU = "torikomi_riyuu"                  # 取込理由
PROP_SAISHUU_CSV_BI = "saishuu_csv_kenshutsu_bi"        # 最終CSV検出日 (date)
PROP_KONKAI_CSV_FLAG = "konkai_csv_kenshutsu_flag"      # 今回CSV検出フラグ (bool)
PROP_DOUKI_FILENAME = "douki_moto_filename"             # 同期元ファイル名
PROP_LAST_SYNCED = "doukisaishuujikoku"                 # 最終同期日 (既存)
PROP_MEDIA_NAME = "shuyoushukkoubaitai"                 # 主要出稿媒体 (既存enum)


# ============================================================================
# ステータス正規化 (v0.2 §10)
# ============================================================================
def map_hr_status(original: str) -> Optional[str]:
    """HRハッカー原ステータスを HubSpot共通ステータスへ正規化.

    Returns:
        "公開前" / "公開中" / "公開終了" or None (非公開 = 契約求人数除外)
    """
    if original is None:
        return None
    s = original.strip()
    if s == "公開開始前":
        return "公開前"
    if s == "公開":
        return "公開中"
    if s == "公開終了":
        return "公開終了"
    if s == "非公開":
        # 2026-07-09 ユーザー確定(a): 非公開になった求人は kyuujin_status も
        # 「公開終了」に自動変更する(ステータス自動変更機能)。
        # 契約求人数(=公開中)には引き続き含まれない(公開終了なので除外は維持)。
        return "公開終了"
    # 未知ステータス: そのまま渡さず None
    return None


# ============================================================================
# CSV読込 (Shift-JIS 既定、Phase 0b 実測準拠)
# ============================================================================
def load_hr_csv(path: str | Path, encoding: str = HR_CSV_ENCODING) -> list[dict]:
    """HRハッカーCSVを読込み, 内部キー辞書のリストを返す.

    encoding: 既定 "shift_jis"  (Phase 0b 実測, ID 28382, 84列)。
    """
    rows: list[dict] = []
    with open(path, encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        for raw in reader:
            row: dict = {}
            for csv_col, key in HR_CSV_COLUMNS.items():
                row[key] = (raw.get(csv_col) or "").strip()
            row["copy_body"] = compose_copy_body(raw, header)
            row["copy_images"] = compose_copy_images(raw, header)
            rows.append(row)
    return rows


# ============================================================================
# HubSpot LISTING 検索 (id_hrhakkaa による)
# ============================================================================
# Search API の間隔と再試行 (2026-10-09)。
# HubSpot の Search は口座全体で約 5 回/秒、HR_HR アプリと共有 (外部バッチに残るのは
# 約 2 回/秒)。旧 0.1 秒間隔 (約 10 回/秒) で 327 回検索して 429 になり、取込全体が落ちた。
SEARCH_INTERVAL_SEC = 0.5
SEARCH_MAX_RETRIES = 5
RETRY_AFTER_DEFAULT_SEC = 2.0
RETRY_AFTER_CAP_SEC = 30.0
# 検索の総時間の上限。ワークフロー (job_daily) の timeout は 30 分なので、
# 後続の更新 (batch_update 等) の時間を残すため 20 分で打ち切って明示エラーにする。
# 通常は 327 回 × 0.5 秒 ≒ 3 分弱。
SEARCH_TIME_BUDGET_SEC = 20 * 60


def _retry_after_sec(resp) -> float:
    try:
        v = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        v = RETRY_AFTER_DEFAULT_SEC
    return min(max(v, 0.0), RETRY_AFTER_CAP_SEC)


def _search_with_retry(url: str, headers: dict, body: dict):
    """Search を 1 回呼ぶ. 429 は Retry-After (既定2秒, 上限30秒) 待って、5xx は指数
    バックオフで、最大 SEARCH_MAX_RETRIES 回まで再試行. それ以外のエラーは raise."""
    attempt = 0
    while True:
        r = requests.post(url, headers=headers, json=body, timeout=30)
        code = r.status_code
        if code == 429 or 500 <= code < 600:
            if attempt >= SEARCH_MAX_RETRIES:
                r.raise_for_status()
            wait = (_retry_after_sec(r) if code == 429
                    else min(RETRY_AFTER_DEFAULT_SEC * (2 ** attempt), RETRY_AFTER_CAP_SEC))
            attempt += 1
            plog.public(f"[hrhacker_import] Search HTTP {code} → {wait:.0f}秒待って再試行 "
                        f"({attempt}/{SEARCH_MAX_RETRIES})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r


def find_hubspot_jobs(media_job_ids: list[str]) -> dict[str, dict]:
    """id_hrhakkaa リスト → {media_job_id: {id, properties}} の辞書を返す.

    Search API でバッチ取得 (100件チャンク, OR検索).
    id_shop_hrhakkaa は「既存空欄のみ補完」ガードのため既存値を取得する
    (HR-01: 未取得だとガードが死に毎回無条件上書きになる)。
    """
    result: dict[str, dict] = {}
    if not media_job_ids:
        return result
    target_props = ["id_hrhakkaa", "hs_name", "url_hrhakkaa",
                    "id_shop_hrhakkaa",
                    PROP_HS_KYUUJIN_STATUS, PROP_MEDIA_ORIG_STATUS,
                    # 本文・画像は値が変わったときだけ書くため、今の値を読む
                    PROP_COPY_BODY, PROP_COPY_IMAGES,
                    # ステージ保護判定に必要 (2026-08-06): 現ステージが
                    # 選考進行中/採用決定なら機械は上書きしない
                    "hs_pipeline_stage"]
    headers = _headers()
    started = time.monotonic()
    calls = 0
    # IN operator で 100件ずつ検索
    for i in range(0, len(media_job_ids), 100):
        chunk = [j for j in media_job_ids[i:i + 100] if j]
        if not chunk:
            continue
        if time.monotonic() - started > SEARCH_TIME_BUDGET_SEC:
            raise RuntimeError(
                f"find_hubspot_jobs: 検索の総時間が上限 {SEARCH_TIME_BUDGET_SEC}s を超えた "
                f"(実行 {calls} 回). ワークフローの timeout 前に中断")
        body = {
            "limit": 200,
            "properties": target_props,
            "filterGroups": [{"filters": [
                {"propertyName": "id_hrhakkaa", "operator": "IN", "values": chunk}
            ]}],
        }
        r = _search_with_retry(f"{BASE}/crm/v3/objects/0-420/search", headers, body)
        calls += 1
        for o in r.json().get("results", []):
            jid = (o.get("properties") or {}).get("id_hrhakkaa")
            if jid:
                result[jid] = {"id": o["id"], "properties": o.get("properties") or {}}
        time.sleep(SEARCH_INTERVAL_SEC)
    return result


# ============================================================================
# プロパティビルダ
# ============================================================================
def build_update_props(row: dict, existing_props: dict, today_iso: str,
                       now_iso: str, source_filename: str) -> dict:
    """既存LISTING更新用プロパティ. 既存値保護を適用.

    更新対象 (常に更新):
      - 媒体原ステータス (CSV生値)
      - HubSpot求人ステータス (正規化結果, None ならば送らない)
      - 最終CSV検出日 (今日)
      - 今回CSV検出フラグ = true
      - 最終同期日 (今)
      - 同期元ファイル名

    媒体SSOT (①2026-07-10): 媒体を正として常に上書き (CSV非空時のみ):
      - hs_name (求人名)
    ※url_hrhakkaa は HR CSV にURL列が無いため更新では触らない (新規作成時に
      求人IDからテンプレ生成する build_create_props 側で設定。HR-02)。
    媒体と無関係なメモ欄 (baitaibetsushousaimemo / kaizenmemo /
    ichijimensetsu_hiaringukoumoku 等) は同期対象外 = 一切書かない (手入力保護)。
    """
    p: dict = {}

    # 媒体原ステータス (常に上書き = CSV生値を保持)
    if row.get("original_status"):
        p[PROP_MEDIA_ORIG_STATUS] = row["original_status"]

    # 正規化ステータス
    normalized = map_hr_status(row.get("original_status", ""))
    if normalized is not None:
        p[PROP_HS_KYUUJIN_STATUS] = normalized
        # ステージも追従させる (2026-08-06): kyuujin_status だけ更新して
        # hs_pipeline_stage を放置していたため「公開終了なのにボードは募集中」
        # が1,083件溜まっていた。選考進行中/採用決定は人の領域なので保護。
        p.update(_stage.stage_props(
            normalized, (existing_props or {}).get("hs_pipeline_stage")))

    # 同期管理列
    p[PROP_SAISHUU_CSV_BI] = today_iso
    p[PROP_KONKAI_CSV_FLAG] = "true"
    p[PROP_LAST_SYNCED] = now_iso
    p[PROP_DOUKI_FILENAME] = source_filename

    # ①媒体SSOT (2026-07-10): タイトルは媒体を正として常に上書き(CSV非空時)。
    # 空CSV値では上書きしない(ブランク化防止)。メモ欄は同期対象外=一切書かない。
    # URLはHR CSVに列が無いため更新では扱わない(HR-02)。
    csv_name = (row.get("job_name") or "").strip()
    if csv_name:
        p["hs_name"] = csv_name

    # 店舗ID: 既存空欄なら補完 (Deal突合キー。既存1379件以外を埋める)
    if row.get("shop_id"):
        ex_shop = existing_props.get("id_shop_hrhakkaa")
        ex_shop = ex_shop.strip() if isinstance(ex_shop, str) else ""
        if not ex_shop:
            p["id_shop_hrhakkaa"] = str(row["shop_id"]).strip()

    # 公開開始日/終了日 (§21.1): 常に最新CSV値で更新 (媒体側の日付は変わりうる)
    for src, prop in [("start_date", "koukai_kaishi_nichiji"),
                      ("end_date", "koukai_shuuryou_nichiji")]:
        ms = _hr_date_to_millis(row.get(src))
        if ms is not None:
            p[prop] = ms

    # 求人票の本文・画像: 値が変わったときだけ (プロパティ履歴 = 版の履歴)
    p.update(copy_props(row, existing_props))

    return p


def _hr_date_to_millis(s: object) -> Optional[int]:
    """CSVの日時文字列 → epoch millis(UTC midnight) (HubSpot date用)。不能はNone。"""
    import calendar
    if not s:
        return None
    txt = str(s).strip().replace("/", "-")
    if not txt:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
            return calendar.timegm((dt.year, dt.month, dt.day, 0, 0, 0)) * 1000
        except ValueError:
            continue
    return None


def build_create_props(row: dict, today_iso: str, now_iso: str,
                       source_filename: str) -> dict:
    """新規LISTING作成用プロパティ."""
    p: dict = {}
    jid = row.get("media_job_id", "")
    if not jid:
        return p

    # 必須: 媒体ID
    p["id_hrhakkaa"] = jid

    # 店舗ID (§21.1 + Deal突合キー): CSV「店舗id」→ id_shop_hrhakkaa。
    # これが無いと後段の LISTING→Deal 関連付け(shop_id→hrhacker_shop_ids)が出来ない。
    if row.get("shop_id"):
        p["id_shop_hrhakkaa"] = str(row["shop_id"]).strip()

    # 公開開始日/終了日 (§21.1): CSVの日時 → LISTINGへ格納
    for src, prop in [("start_date", "koukai_kaishi_nichiji"),
                      ("end_date", "koukai_shuuryou_nichiji")]:
        ms = _hr_date_to_millis(row.get(src))
        if ms is not None:
            p[prop] = ms

    # URL (CSVに無ければ URL テンプレートから生成)
    # ※ 2026-07-08 HTTP検証: URL_TMPL(.../job-offers/show/{id_hrhakkaa})は
    #   公開中求人で200・id一致=正しい個別URL。既存LISTINGも全て正しく保持済。
    url = row.get("url") or URL_TMPL.format(jid)
    p["url_hrhakkaa"] = url

    # 求人名 = LISTING 必須プロパティ (Phase 0b 実測 2026-06-26 で判明)
    # CSV にあればそれを使い、空ならフォールバックで「HRハッカー求人 <id>」を仮タイトル化
    p["hs_name"] = row.get("job_name") or f"HRハッカー求人 {jid}"

    # 媒体名 enum (Phase 0 で HRハッカー が enum に未追加 = Phase 1 で追加後利用)
    p[PROP_MEDIA_NAME] = MEDIA_NAME

    # ステータス
    if row.get("original_status"):
        p[PROP_MEDIA_ORIG_STATUS] = row["original_status"]
    normalized = map_hr_status(row.get("original_status", ""))
    if normalized is not None:
        p[PROP_HS_KYUUJIN_STATUS] = normalized
        # 新規作成時からステージを付ける (2026-08-06)。未設定だとボードに出ない
        p.update(_stage.stage_props(normalized))

    # 管理区分・取込理由
    p[PROP_KANRI_KUBUN] = "弊社管理"
    p[PROP_TORIKOMI_RIYUU] = "HRハッカーCSV新規検出"

    # 同期管理
    p[PROP_SAISHUU_CSV_BI] = today_iso
    p[PROP_KONKAI_CSV_FLAG] = "true"
    p[PROP_LAST_SYNCED] = now_iso
    p[PROP_DOUKI_FILENAME] = source_filename

    # 求人票の本文・画像 (最初の版)
    p.update(copy_props(row, None))

    return p


# ============================================================================
# HubSpot 書込 (batch)
# ============================================================================
def batch_update(updates: list[dict]) -> tuple[int, int, list[str]]:
    """batch/update を 100件チャンクで実行. (ok, ng, errors) を返す."""
    if not updates:
        return 0, 0, []
    headers = _headers()
    ok = ng = 0
    errors: list[str] = []
    for i in range(0, len(updates), 100):
        chunk = updates[i:i + 100]
        r = requests.post(f"{BASE}/crm/v3/objects/0-420/batch/update",
                          headers=headers, json={"inputs": chunk}, timeout=60)
        if r.status_code in (200, 207):
            j = r.json()
            ok += len(j.get("results", []))
            ne = j.get("numErrors", 0)
            ng += ne
            if ne:
                errors.append(f"chunk {i//100}: {r.text[:300]}")
        else:
            ng += len(chunk)
            errors.append(f"HTTP {r.status_code}: {r.text[:300]}")
        time.sleep(0.15)
    return ok, ng, errors


def batch_create(creates: list[dict]) -> tuple[int, int, list[str], dict]:
    """batch/create を 100件チャンクで実行. (ok, ng, errors, idmap) を返す."""
    if not creates:
        return 0, 0, [], {}
    headers = _headers()
    ok = ng = 0
    errors: list[str] = []
    idmap: dict = {}
    for i in range(0, len(creates), 100):
        chunk = creates[i:i + 100]
        r = requests.post(f"{BASE}/crm/v3/objects/0-420/batch/create",
                          headers=headers, json={"inputs": chunk}, timeout=60)
        if r.status_code in (200, 201, 207):
            j = r.json()
            for o in j.get("results", []):
                jid = (o.get("properties") or {}).get("id_hrhakkaa")
                if jid:
                    idmap[jid] = o["id"]
            ok += len(j.get("results", []))
            ne = j.get("numErrors", 0)
            ng += ne
            if ne:
                errors.append(f"chunk {i//100}: {r.text[:300]}")
        else:
            ng += len(chunk)
            errors.append(f"HTTP {r.status_code}: {r.text[:300]}")
        time.sleep(0.15)
    return ok, ng, errors, idmap


# ============================================================================
# メイン処理
# ============================================================================
def run(csv_path: str, dry_run: bool = True, limit: Optional[int] = None) -> dict:
    """A-1 メインオーケストレーション. 結果サマリ辞書を返す."""
    csv_path = str(csv_path)
    source_filename = os.path.basename(csv_path)
    today_iso = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"=== hrhacker_import (dry_run={dry_run}) ===")
    print(f"CSV: {csv_path}")

    rows = load_hr_csv(csv_path)
    if limit:
        rows = rows[:limit]
    print(f"CSV件数: {len(rows)}")

    # 媒体求人IDが空の行を除外
    valid_rows = [r for r in rows if r.get("media_job_id")]
    skipped_no_id = len(rows) - len(valid_rows)
    if skipped_no_id:
        print(f"⚠️ 媒体求人ID欠落でスキップ: {skipped_no_id} 件")

    media_job_ids = [r["media_job_id"] for r in valid_rows]

    # 既存LISTING検索
    if not dry_run or os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        try:
            existing = find_hubspot_jobs(media_job_ids)
            print(f"既存LISTING: {len(existing)} 件")
        except Exception as e:
            if dry_run:
                print(f"⚠️ Search API失敗 (dry-run継続): {e}")
                existing = {}
            else:
                raise
    else:
        existing = {}

    updates: list[dict] = []
    creates: list[dict] = []
    update_preview: list[dict] = []
    create_preview: list[dict] = []

    for row in valid_rows:
        jid = row["media_job_id"]
        if jid in existing:
            props = build_update_props(row, existing[jid]["properties"],
                                       today_iso, now_iso, source_filename)
            updates.append({"id": existing[jid]["id"], "properties": props})
            update_preview.append({"id_hrhakkaa": jid,
                                   "hubspot_id": existing[jid]["id"],
                                   "properties": props})
        else:
            props = build_create_props(row, today_iso, now_iso, source_filename)
            creates.append({"properties": props})
            create_preview.append({"id_hrhakkaa": jid, "properties": props})

    summary = {
        "csv_path": csv_path,
        "csv_total_rows": len(rows),
        "skipped_no_id": skipped_no_id,
        "valid_rows": len(valid_rows),
        "existing_listings": len(existing),
        "updates_planned": len(updates),
        "creates_planned": len(creates),
        "dry_run": dry_run,
    }

    print(f"\n--- 集計 ---")
    print(f"  更新予定: {len(updates)} 件")
    print(f"  新規作成予定: {len(creates)} 件")

    if dry_run:
        log = {
            "summary": summary,
            "updates_preview": update_preview[:10],
            "creates_preview": create_preview[:10],
        }
    else:
        print("\n--- 本番実行 ---")
        u_ok, u_ng, u_err = batch_update(updates)
        c_ok, c_ng, c_err, idmap = batch_create(creates)
        summary.update({
            "updates_ok": u_ok, "updates_ng": u_ng,
            "creates_ok": c_ok, "creates_ng": c_ng,
        })
        print(f"  更新: ✅{u_ok} ❌{u_ng}")
        print(f"  作成: ✅{c_ok} ❌{c_ng}")
        # HubSpot のエラー文は値 (会社名など) を含みうるので件数だけ。本文は非公開ログ
        if u_err:
            print(f"  更新エラー: {len(u_err)}件 (内容は非公開ログ)")
            plog.detail("hrhacker_import_update_errors", errors=u_err[:20])
        if c_err:
            print(f"  作成エラー: {len(c_err)}件 (内容は非公開ログ)")
            plog.detail("hrhacker_import_create_errors", errors=c_err[:20])
        # ③新規LISTINGに暗黙知テンプレNoteを付与 (best-effort, 既ピン留めはskip)
        try:
            from scripts.job_application_sync.notes import attach_template_notes
        except ImportError:
            from notes import attach_template_notes
        note_ok, note_failed = attach_template_notes(list(idmap.values()))
        summary["template_notes_attached"] = note_ok
        summary["template_notes_failed"] = note_failed
        print(f"  テンプレNote付与: ✅{note_ok}"
              + (f" ❌{len(note_failed)} {note_failed[:5]}" if note_failed else ""))
        log = {
            "summary": summary,
            "errors": {"update": u_err, "create": c_err},
            "created_idmap": idmap,
        }

    # ログ出力
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = LOG_DIR / f"hrhacker_import_{'dry' if dry_run else 'actual'}_{ts}.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\nログ: {log_path}")

    return summary


def run_copy_only(csv_path: str, dry_run: bool = True,
                  limit: Optional[int] = None) -> dict:
    """一回限りの埋め戻し: 既存 LISTING の本文・画像 2 プロパティだけを更新する.

    - 新規作成はしない (batch_create / テンプレNote は呼ばない)
    - 触るのは hrh_kyuujinhyou_honbun / hrh_kyuujinhyou_gazou だけ
      (ステータス・ステージ・名前・日付・同期管理列は一切書かない)
    - HubSpot に無い id は件数だけ数えて飛ばす. 値が同じ行は書かない (copy_props)
    - 公開ログには件数だけ出す (顧客名・ID は出さない)
    """
    rows = load_hr_csv(str(csv_path))
    if limit:
        rows = rows[:limit]
    valid_rows = [r for r in rows if r.get("media_job_id")]
    media_job_ids = list(dict.fromkeys(r["media_job_id"] for r in valid_rows))
    plog.public(f"=== hrhacker_import --copy-only (dry_run={dry_run}) === CSV {len(rows)} 件")

    summary: dict = {
        "mode": "copy_only", "dry_run": dry_run, "csv_total_rows": len(rows),
        "valid_rows": len(valid_rows), "not_found": 0, "unchanged": 0,
        "updates_planned": 0,
    }
    if dry_run and not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        summary["search_skipped"] = "dry_run_no_token"
        plog.public("  (dry-run かつトークン無し: HubSpot 検索を省略)")
        return summary

    existing = find_hubspot_jobs(media_job_ids)
    updates: list[dict] = []
    seen_ids: set[str] = set()
    for row in valid_rows:
        hit = existing.get(row["media_job_id"])
        if hit is None:
            summary["not_found"] += 1
            continue
        if hit["id"] in seen_ids:  # CSV 内の重複行 (batch に同じ id を 2 度入れない)
            continue
        props = copy_props(row, hit["properties"])
        if not props:
            summary["unchanged"] += 1
            continue
        seen_ids.add(hit["id"])
        updates.append({"id": hit["id"], "properties": props})
    summary["updates_planned"] = len(updates)
    plog.public(f"  更新予定 {len(updates)} 件 / 変更なし {summary['unchanged']} 件 / "
                f"HubSpotに無い {summary['not_found']} 件")

    if not dry_run:
        u_ok, u_ng, u_err = batch_update(updates)
        summary.update({"updates_ok": u_ok, "updates_ng": u_ng})
        plog.public(f"  更新: OK {u_ok} / NG {u_ng}")
        if u_err:
            plog.public(f"  更新エラー {len(u_err)} 件 (内容は非公開ログ)")
            plog.detail("hrhacker_import_copy_only_errors", errors=u_err[:20])
    return summary


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="HRハッカーCSV取込 (LISTING upsert)")
    p.add_argument("--csv", required=True, help="HR CSV パス")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="(既定) 実行計画のみ出力")
    g.add_argument("--actual", action="store_true",
                   help="HubSpot API を実行する")
    p.add_argument("--limit", type=int, default=None,
                   help="CSV先頭N件のみ処理 (テスト用)")
    p.add_argument("--copy-only", action="store_true",
                   help="一回限りの埋め戻し: 既存 LISTING の本文・画像 2 プロパティだけを更新 "
                        "(新規作成・他プロパティは一切触らない)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.copy_only:
        res = run_copy_only(args.csv, dry_run=not args.actual, limit=args.limit)
        if res.get("updates_ng"):
            sys.exit(1)  # 書込に失敗した分があればジョブを失敗にする
        return
    run(args.csv, dry_run=not args.actual, limit=args.limit)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    finally:
        plog.flush()
