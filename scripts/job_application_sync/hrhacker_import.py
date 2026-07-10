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
        for raw in reader:
            row: dict = {}
            for csv_col, key in HR_CSV_COLUMNS.items():
                row[key] = (raw.get(csv_col) or "").strip()
            rows.append(row)
    return rows


# ============================================================================
# HubSpot LISTING 検索 (id_hrhakkaa による)
# ============================================================================
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
                    PROP_HS_KYUUJIN_STATUS, PROP_MEDIA_ORIG_STATUS]
    headers = _headers()
    # IN operator で 100件ずつ検索
    for i in range(0, len(media_job_ids), 100):
        chunk = [j for j in media_job_ids[i:i + 100] if j]
        if not chunk:
            continue
        body = {
            "limit": 200,
            "properties": target_props,
            "filterGroups": [{"filters": [
                {"propertyName": "id_hrhakkaa", "operator": "IN", "values": chunk}
            ]}],
        }
        r = requests.post(f"{BASE}/crm/v3/objects/0-420/search",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        for o in r.json().get("results", []):
            jid = (o.get("properties") or {}).get("id_hrhakkaa")
            if jid:
                result[jid] = {"id": o["id"], "properties": o.get("properties") or {}}
        time.sleep(0.1)
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

    # 管理区分・取込理由
    p[PROP_KANRI_KUBUN] = "弊社管理"
    p[PROP_TORIKOMI_RIYUU] = "HRハッカーCSV新規検出"

    # 同期管理
    p[PROP_SAISHUU_CSV_BI] = today_iso
    p[PROP_KONKAI_CSV_FLAG] = "true"
    p[PROP_LAST_SYNCED] = now_iso
    p[PROP_DOUKI_FILENAME] = source_filename

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
        if u_err: print(f"  更新エラー: {u_err[:3]}")
        if c_err: print(f"  作成エラー: {c_err[:3]}")
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
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run(args.csv, dry_run=not args.actual, limit=args.limit)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
