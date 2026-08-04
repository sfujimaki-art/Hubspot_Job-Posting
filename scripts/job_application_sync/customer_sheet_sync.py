# -*- coding: utf-8 -*-
"""応募者情報 → 顧客の応募者管理シート 転記 (2026-08-03 MVP)。

目的:
  応募が来たら顧客の応募者管理シート「応募一覧(自動)」タブへ自動転記し、
  コンサルが既存の管理表へ書き写す際の手打ちを無くす。

安全設計 (顧客の資産に書くため厳格):
  - 許可リスト方式: CUSTOMER_SHEET_ALLOW に無いシートには絶対に書かない
  - 既存タブ不可侵: 書込先は「応募一覧(自動)」タブのみ (無ければ作成)
  - 列はヘッダ名で特定 (位置ハードコード禁止。顧客が列を動かしても壊れない)
    → 必須列が見つからなければ例外で停止 (黙って別列に書かない)
  - 冪等: 「応募ID」列の既存値と突合し、重複追記しない
  - 遡及なし: cutoff (UTC/Z形式必須) 以降に作成された応募のみ
  - dry-run 既定 / PIIはログに出さない (本番リポはPUBLIC)

双方向(Phase2)への布石:
  応募ID = 突合キー / 最終更新日時 = 競合判定の材料。両方を必ず書く。
  転記成功後は HubSpot の kokyakushiitotenkijoukyou を「転記済」に更新し、
  既存運用(BPO/コンサルはこの列を作業起点にする)との二重転記を防ぐ。

検証履歴: 機能E2E16項目 / 逆証明10項目 / サニタイズ実データ4,000件 /
逆流成立性8項目 / 並行レース・append・書式の親実証 / 多角レビュー(5レンズ)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
load_dotenv(_REPO / ".env")

BASE = "https://api.hubapi.com"
TAB_NAME = "応募一覧(自動)"
_JST = timezone(timedelta(hours=9))

# 顧客シート書込用SA (顧客の共有者リストに出るため専用アカウント)
SA_KEY = os.environ.get(
    "CUSTOMER_SHEET_SA_JSON",
    str(Path(os.path.expanduser("~")) / ".config/gws/rl-saiyokanri-sheet-sync.json"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── 列定義 (2026-08-04 MVP12社の実査で改訂) ──────────────────────────
# 書込先はこのモジュールが作る新タブ「応募一覧(自動)」のみ。既存タブ
# (採用管理表)には絶対に書かない。列の並びだけ12社共通の採用管理表
# (No/応募受付日/応募拠点/職種/氏名/ﾌﾘｶﾞﾅ/年齢/電話番号/メール/住所)に
# 揃え、コンサルが行コピーで既存タブへ移す時に列が揃うようにする。
# A: No は顧客採番なので空。L〜S: 社別記入領域。T/U: 管理列
# (多角レビューG5: 途中列に置くとコピーで顧客の記入を潰すため右端固定)
COLUMNS = [
    "No", "応募受付日", "応募拠点", "職種", "氏名",
    "フリガナ", "年齢", "性別", "電話番号", "メールアドレス", "住所",
    "", "", "", "", "", "", "", "",
    "応募ID", "最終更新日時",
]
KEY_COL = "応募ID"          # 突合キー (双方向の印)
SYNCED_COL = "最終更新日時"
# 転記時に値を書く列 (これらがヘッダに無ければ停止。多角レビューG11:
# 顧客が列名を変えると r.get() が黙って空欄化しサイレント欠落するため)
REQUIRED_COLS = ["応募受付日", "職種", "氏名", "フリガナ", "年齢",
                 "電話番号", "メールアドレス", "住所", KEY_COL, SYNCED_COL]
# 先頭0/長桁を守るためTEXT書式で固定する列
TEXT_COLS = ("電話番号", KEY_COL)

HS_PROPS = [
    "hs_object_id", "yingmuri", "oubobaitaimei",
    "ouboshashimei", "ouboshashimei_kana", "nenrei", "seibetsu",
    "denwabangou", "meeruadoresu",
    "yuubinbangou", "todoufuken", "shikuchouson", "shikuchousonikajuusho",
    "oubosaki_shokushu", "oubosaki_kyuujin_title", "oubosaki_kinmuchi",
    "hs_createdate",
]
# 転記完了をHubSpotへ書き戻すプロパティ (既存運用の作業起点列)
PROP_TENKI = "kokyakushiitotenkijoukyou"   # 未転記/転記済/対象外/未設定


def _h() -> dict:
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_ACCESS_TOKEN']}",
            "Content-Type": "application/json"}


def _post_hs(url: str, body: dict, retries: int = 3) -> dict:
    """HubSpot POST + 429/5xx リトライ + 非200を例外化。

    親レビュー所見C10: 素の .json() は 429 のエラーボディでも例外にならず
    results 欠落 → 「対象応募0」として静かに成功扱いになる(サイレント欠落)。
    転記の取りこぼしは誰も気づけないため、失敗は必ず例外で顕在化させる。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=60)
        except requests.RequestException as e:
            # 一時的なネットワーク断/タイムアウトも再試行 (本番run3で実測)
            last = f"{type(e).__name__}"
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            break
        # 207 Multi-Status: v4 association batch は一部が紐付き無しでも
        # 207+results で返す(正常系)。実運用dry-runで発見 2026-08-04
        if r.status_code in (200, 207):
            return r.json()
        last = f"HTTP {r.status_code}: {r.text[:120]}"
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 ** attempt * 2)   # 2s, 4s, 8s
            continue
        break
    raise RuntimeError(f"HubSpot API失敗 ({url.rsplit('/', 2)[-2]}): {last}")


def _exec(req, retries: int = 3):
    """Sheets API execute + 429/5xx 指数バックオフ。

    多角レビューS2: googleapiclient の execute() は既定リトライ0。
    60write/分のユーザークォータに触れると即例外で、addSheet 直後の失敗は
    「空タブだけ残る」恒久停止バグ(G6)の発火点になるため必須。
    """
    from googleapiclient.errors import HttpError
    for attempt in range(retries + 1):
        try:
            return req.execute()
        except HttpError as e:
            code = getattr(getattr(e, "resp", None), "status", None)
            if code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except (TimeoutError, ConnectionError, OSError):
            # ソケットタイムアウト等の一時障害 (本番run3のホンダ西日本で実測)。
            # 権限403等は HttpError 側なのでここには来ない = 恒常エラーは隠さない
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def _a1(ref: str) -> str:
    """タブ名を常にシングルクォートで包んだA1参照 (多角レビューS4/G14)。"""
    return "'" + TAB_NAME.replace("'", "''") + "'!" + ref


def _sheets():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    cred = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    return build("sheets", "v4", credentials=cred)


# ── 媒体由来テキストのサニタイズ (2026-08-03) ──────────────────────────
# 応募媒体の生テキストは我々の管理外で、HubSpot も正規化しきれずそのまま
# 格納しているものがある。実測(直近4,000件)で確認した汚れ:
#   全角数字 266件 (住所) / 半角カナ 11件 ('1ｰ14ｰ6' の ｰ は長音符!) /
#   連続空白・全角空白 7件 / 制御文字・改行 / '（国：日本）' 等のゴミ
# 数式インジェクション(=,+,-,@ 始まり)は valueInputOption=RAW が
# stringValue として保存するため無害 (実測で formulaValue にならないことを確認)。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTISPACE = re.compile(r"[ 　]{2,}")
# AW住所末尾に付く国名表記 (顧客には不要なノイズ)
_ADDR_NOISE = re.compile(r"[\(（]\s*国\s*[:：]\s*日本\s*[\)）]")


def clean_text(s: str, *, normalize_width: bool = False) -> str:
    """媒体由来テキストの共通クリーニング。

    normalize_width=True で全角数字→半角・半角カナ→全角に正規化する
    (住所など機械可読性が要る項目のみ。氏名は原文尊重で False)。
    """
    v = str(s or "")
    if not v:
        return ""
    v = _CTRL.sub("", v)                      # 制御文字除去
    v = v.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    v = v.replace("\t", " ")                  # セル内改行/タブ → 空白
    if normalize_width:
        # NFKC は全角数字→半角、半角カナ→全角 をまとめて行う
        # ('1ｰ14ｰ6' の半角長音符 ｰ も 'ー' に正規化される)
        v = unicodedata.normalize("NFKC", v)
    v = _MULTISPACE.sub(" ", v)               # 連続空白を1つに
    return v.strip()


# 数字に挟まれた長音符は番地のハイフン (実データ '1ｰ14ｰ6' / '2312ｰ30')。
# NFKC は半角長音 ｰ → 全角長音 ー にするだけなので、番地だけハイフンに戻す。
# 「シャーメゾン」等の正当な長音符は数字に挟まれないので影響しない。
_BANCHI_DASH = re.compile(r"(?<=[0-9])[ー−–—](?=[0-9])")


def clean_address(s: str) -> str:
    """住所用: 幅正規化 + 番地ハイフン是正 + 媒体由来のノイズ除去。"""
    v = clean_text(s, normalize_width=True)
    v = _ADDR_NOISE.sub("", v)
    v = _BANCHI_DASH.sub("-", v)
    return _MULTISPACE.sub(" ", v).strip()


def to_domestic_phone(s: str) -> str:
    """E.164(+81…) → 国内形式(0…)。顧客が実際に発信する番号なので国内表記にする。

    HubSpot側は denwabangou が電話型で国番号必須のため +81 で保持している
    (国内形式だと INVALID_PHONE_NUMBER で電話がドロップする事故が過去に発生)。
    そのため変換は「シートへ書き出す瞬間」だけで行い、HubSpotの値は触らない。
    """
    import re as _re
    v = str(s or "").strip()
    if not v:
        return ""
    d = _re.sub(r"\D", "", v)
    if d.startswith("81") and len(d) >= 11:
        return "0" + d[2:]
    return v if not d else d


def format_postal(s: str) -> str:
    """郵便番号を 〒NNN-NNNN 表記に (既存タブの記載形式に合わせる)。"""
    import re as _re
    d = _re.sub(r"\D", "", str(s or ""))
    if len(d) == 7:
        return f"〒{d[:3]}-{d[3:]}"
    return str(s or "").strip()


def build_row(p: dict) -> dict:
    """応募者プロパティ → {ヘッダ名: 値}。空値は空文字。

    媒体由来の汚れは全項目 clean_text を通す。住所は幅正規化+ノイズ除去、
    氏名・カナは原文尊重(幅正規化しない=本人の表記を勝手に変えない)。
    """
    addr = " ".join(x for x in [
        format_postal(p.get("yuubinbangou")),
        clean_address(p.get("todoufuken")),
        clean_address(p.get("shikuchouson")),
        clean_address(p.get("shikuchousonikajuusho")),
    ] if x)
    addr = _MULTISPACE.sub(" ", addr).strip()
    # 年齢は数値で書く (多角レビューS6: 文字列だと顧客のソートで "10"<"9")
    age_s = clean_text(p.get("nenrei"), normalize_width=True)
    age = int(age_s) if age_s.isdigit() else age_s
    return {
        # No は顧客側の採番なので書かない
        "応募受付日": (p.get("yingmuri") or "")[:10],
        # 応募拠点: 求人の勤務地 (新規応募の充足47%、無ければ空欄)
        "応募拠点": clean_text(p.get("oubosaki_kinmuchi")),
        # 職種: 応募先職種 → 無ければ求人タイトル
        "職種": clean_text(p.get("oubosaki_shokushu")
                           or p.get("oubosaki_kyuujin_title")),
        # 氏名・カナは原文尊重 (幅正規化しない)。制御文字/改行/連続空白のみ除去
        "氏名": clean_text(p.get("ouboshashimei")),
        "フリガナ": clean_text(p.get("ouboshashimei_kana")),
        "年齢": age,
        "性別": clean_text(p.get("seibetsu")),
        "電話番号": to_domestic_phone(p.get("denwabangou")),
        "メールアドレス": clean_text(p.get("meeruadoresu"),
                                     normalize_width=True),
        "住所": addr,
        KEY_COL: p.get("hs_object_id") or "",
        # JST明示 (親レビュー所見C2): GitHub Actions は UTC のため naive な
        # datetime.now() だと顧客に9時間ズレた時刻を見せてしまう
        SYNCED_COL: datetime.now(_JST).strftime("%Y-%m-%d %H:%M"),
    }


def fetch_applicants(sheet_id: str, cutoff_iso: str, limit: int = 0) -> list:
    """指定シートURLを持つ求人に紐づく応募を、cutoff以降で取得。

    cutoff_iso は必須 (逆証明A4): 空文字だと全応募が対象になり
    「実装日以降の新規応募のみ」という約束が破れて過去分が顧客シートに
    大量流入する。呼び出し側のミスを黙って通さない。
    """
    if not (cutoff_iso or "").strip():
        raise ValueError(
            "cutoff_iso は必須です (空だと過去の全応募が転記され事故になる)。"
            "例: '2026-08-03T00:00:00Z'")
    # 親レビュー所見C1: hs_createdate(…Z形式)との比較は文字列比較のため、
    # '+09:00'等のオフセット付きを渡すと辞書順比較が意味的に壊れる。
    # UTC( Z終端 )のみ受け付ける。
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
                    cutoff_iso.strip()):
        raise ValueError(
            f"cutoff_iso はUTCのISO形式(…Z)で指定してください: {cutoff_iso!r}")
    # 逆証明A5: sheet_id が短いと CONTAINS_TOKEN が広くヒットして
    # 別顧客の求人を拾う (実測: 'docs' で9,230件)。IDは通常44桁。
    if len(sheet_id.strip()) < 30:
        raise ValueError(
            f"sheet_id が短すぎます({len(sheet_id)}文字)。"
            f"部分一致で別顧客の求人を拾う恐れがあるため停止します")
    # 1) このシートを customer_sheet_url に持つ LISTING
    lids, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [
            {"propertyName": "customer_sheet_url",
             "operator": "CONTAINS_TOKEN", "value": sheet_id}]}],
            "properties": ["hs_name"], "limit": 100}
        if after:
            b["after"] = after
        r = _post_hs(f"{BASE}/crm/v3/objects/0-420/search", b)
        lids += [o["id"] for o in r.get("results", [])]
        after = r.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.15)
    if not lids:
        return []
    # 2) LISTING → 応募 (batch)
    aids = set()
    for i in range(0, len(lids), 100):
        ch = lids[i:i + 100]
        r = _post_hs(f"{BASE}/crm/v4/associations/0-420/0-421/batch/read",
                     {"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            for t in (res.get("to") or []):
                aids.add(str(t["toObjectId"]))
        time.sleep(0.15)
    # 3) 応募プロパティ + cutoff絞り
    out = []
    aids = sorted(aids)
    for i in range(0, len(aids), 100):
        ch = aids[i:i + 100]
        r = _post_hs(f"{BASE}/crm/v3/objects/0-421/batch/read",
                     {"inputs": [{"id": x} for x in ch],
                      "properties": HS_PROPS})
        for o in r.get("results", []):
            p = o.get("properties") or {}
            if cutoff_iso and (p.get("hs_createdate") or "") < cutoff_iso:
                continue
            out.append(p)
        time.sleep(0.15)
    out.sort(key=lambda x: x.get("hs_createdate") or "")
    return out[:limit] if limit else out


def read_existing_ids(svc, sheet_id: str) -> tuple:
    """タブの (ヘッダ行, 既存応募IDの集合, データ行数)。タブ無しは (None, set(), 0)。

    多角レビューG6/S-top: 「タブはあるが空」(顧客が同名タブを手作成、または
    addSheet成功直後の失敗で空タブだけ残った場合) は ([], set(), 0) を返し、
    呼び出し側がヘッダを書いてから追記する。旧実装は None 判定だったため
    ヘッダ無しでデータが書かれ、次回実行が恒久停止するバグがあった。
    """
    m = _exec(svc.spreadsheets().get(spreadsheetId=sheet_id,
                                     fields="sheets.properties.title"))
    titles = [s["properties"]["title"] for s in m.get("sheets", [])]
    if TAB_NAME not in titles:
        return None, set(), 0
    r = _exec(svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=_a1("A1:ZZ")))
    vals = r.get("values", [])
    if not vals:
        return [], set(), 0
    header = vals[0]
    if KEY_COL not in header:
        raise RuntimeError(
            f"タブ「{TAB_NAME}」に列「{KEY_COL}」が見つかりません。"
            f"header={header} — 列名が変更された可能性。停止します。")
    # 逆証明A6: 突合キー列が複数あると、どちらが正か決められないまま
    # 片方だけ読んで重複追記する恐れ → 停止する
    if header.count(KEY_COL) > 1:
        raise RuntimeError(
            f"タブ「{TAB_NAME}」に列「{KEY_COL}」が {header.count(KEY_COL)} 個あります。"
            f"どちらが正か判定できないため停止します (重複列を解消してください)")
    ki = header.index(KEY_COL)
    ids = {str(row[ki]).strip() for row in vals[1:]
           if len(row) > ki and str(row[ki]).strip()}
    return header, ids, len(vals) - 1


def allowed_sheets() -> set:
    """書込を許可するシートID集合 (env: CUSTOMER_SHEET_ALLOW, カンマ区切り)。

    未設定なら空集合 = どこにも書けない (安全側)。顧客の資産に書くため、
    明示的に許可されたシート以外への書込は例外で拒否する。
    """
    raw = os.environ.get("CUSTOMER_SHEET_ALLOW", "")
    return {s.strip() for s in raw.replace(",", " ").split() if s.strip()}


def _ensure_header(svc, sheet_id: str, tab_missing: bool) -> None:
    """タブ作成(必要時) + ヘッダ行 + TEXT書式を冪等に適用。

    多角レビューS3: 旧実装は書式適用が addSheet 経路の内側にあり、
    顧客が手作成したタブには一度も書式が当たらなかった。
    """
    if tab_missing:
        _exec(svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties":
                                             {"title": TAB_NAME}}}]}))
    m = _exec(svc.spreadsheets().get(spreadsheetId=sheet_id,
                                     fields="sheets.properties(title,sheetId)"))
    gid = next(s["properties"]["sheetId"] for s in m["sheets"]
               if s["properties"]["title"] == TAB_NAME)
    _exec(svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=_a1("A1"),
        valueInputOption="RAW", body={"values": [COLUMNS]}))
    reqs = [{"repeatCell": {
        "range": {"sheetId": gid, "startColumnIndex": COLUMNS.index(c),
                  "endColumnIndex": COLUMNS.index(c) + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
        "fields": "userEnteredFormat.numberFormat"}}
        for c in TEXT_COLS if c in COLUMNS]
    if reqs:
        _exec(svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                                             body={"requests": reqs}))


def _mark_transferred(ids: list) -> None:
    """転記成功した応募の kokyakushiitotenkijoukyou を「転記済」に更新。

    多角レビューG3: BPO/コンサルはこの列を作業起点にしており、自動転記済みが
    「未転記」のままだと現場が二重転記する。シート書込は成功済みなので、
    ここの失敗は警告に留める (次回runの冪等性はシート側IDで担保される)。
    """
    try:
        for i in range(0, len(ids), 100):
            ch = ids[i:i + 100]
            _post_hs(f"{BASE}/crm/v3/objects/0-421/batch/update",
                     {"inputs": [{"id": x, "properties":
                                  {PROP_TENKI: "転記済"}} for x in ch]})
            time.sleep(0.15)
        print(f"[customer_sheet_sync] HubSpot転記状況を転記済に更新: "
              f"{len(ids)}件", flush=True)
    except Exception as e:
        print(f"[customer_sheet_sync] ⚠️ 転記済フラグ更新失敗 "
              f"(シート書込は成功済み・要手動確認): {type(e).__name__}",
              flush=True)


def sync(sheet_id: str, cutoff_iso: str, dry_run: bool = True,
         limit: int = 0) -> dict:
    # ★最重要ガード: 許可リストに無いシートには絶対に書かない
    if not dry_run:
        allow = allowed_sheets()
        if sheet_id not in allow:
            raise PermissionError(
                f"書込拒否: このシートは許可リストにありません sheet_id={sheet_id[:12]}... "
                f"(許可={len(allow)}件)。CUSTOMER_SHEET_ALLOW に追加してください")
    svc = _sheets()
    header, existing, nrows = read_existing_ids(svc, sheet_id)
    rows = fetch_applicants(sheet_id, cutoff_iso, limit)
    built = [build_row(p) for p in rows]
    # 逆証明A3: 応募ID(突合キー)が無い行は転記できないが、黙って落とすと
    # 「応募が届いていない」ことに誰も気づけない → 件数を必ず報告する
    noid = [r for r in built if not r[KEY_COL]]
    new = [r for r in built if r[KEY_COL] and r[KEY_COL] not in existing]
    tab_state = ("既存" if header else
                 "既存(空)" if header is not None else "新規作成")
    summary = {"sheet_id": sheet_id[:12] + "...", "対象応募": len(rows),
               "既存(スキップ)": len(rows) - len(new) - len(noid),
               "追記": len(new), "ID無し(要調査)": len(noid),
               "タブ": tab_state, "dry_run": dry_run}
    if noid:
        print(f"[customer_sheet_sync] ⚠️ 応募IDが無い行が {len(noid)} 件あり "
              f"転記できません (HubSpot側のデータ不整合の可能性)", flush=True)
    print(f"[customer_sheet_sync] {summary}", flush=True)
    # 親レビュー所見C17: 本番リポはPUBLICでActionsログは誰でも閲覧可能。
    # 氏名/電話/住所等のPIIは絶対にログへ出さない。応募IDと件数のみ。
    if new:
        print(f"   追記対象の応募ID: {[r[KEY_COL] for r in new[:10]]}"
              f"{' ほか' if len(new) > 10 else ''}", flush=True)
    if dry_run or not new:
        return summary
    if not header:   # タブ無し(None) または 空タブ([]) — G6修正
        _ensure_header(svc, sheet_id, tab_missing=(header is None))
        hdr = list(COLUMNS)
        nrows = 0
    else:
        # 多角レビューG11: 転記に使う列がヘッダから消えていたら停止
        # (r.get(c,"") が黙って空欄化しサイレント欠落するため)
        missing = [c for c in REQUIRED_COLS if c not in header]
        if missing:
            raise RuntimeError(
                f"タブ「{TAB_NAME}」に転記必須列がありません: {missing}。"
                f"列名が変更された可能性。停止します")
        hdr = header
    body = {"values": [[r.get(c, "") for c in hdr] for r in new]}
    # RAW 必須 (2026-08-03): USER_ENTERED は手入力扱いで値を型変換するため
    # 電話番号 07066452004 が数値化され先頭0が落ちる(顧客が発信できなくなる)。
    # 応募IDの長い数値が指数表記になる事故も防ぐ。RAW は文字列をそのまま格納。
    resp = _exec(svc.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=_a1("A1"),
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body=body))
    up = (resp or {}).get("updates", {})
    # 多角レビューG10/S7: 書込結果を検証し「書けたつもり」を防ぐ。
    # updatedRange の開始行が想定(既存データ末尾+1)より小さければ、顧客が
    # 途中に空行を入れて append が中段に挿入した兆候 → 警告 (S1)
    if up.get("updatedRows") != len(new):
        raise RuntimeError(
            f"書込行数が不一致: 期待={len(new)} 実際={up.get('updatedRows')} "
            f"(range={up.get('updatedRange')})")
    m0 = re.search(r"[A-Z]+(\d+)", (up.get("updatedRange") or "").split("!")[-1])
    if m0 and int(m0.group(1)) <= nrows:
        print(f"[customer_sheet_sync] ⚠️ 追記位置が既存データ末尾より上 "
              f"(行{m0.group(1)} ≤ 既存{nrows}行)。顧客がタブ途中に空行を"
              f"入れた可能性 — シートの目視確認を推奨", flush=True)
    summary["書込完了"] = len(new)
    print(f"[customer_sheet_sync] 書込={len(new)}行 "
          f"range={up.get('updatedRange')}", flush=True)
    _mark_transferred([r[KEY_COL] for r in new])
    return summary


def run_all(dry_run: bool = True) -> dict:
    """許可リスト全シートへ転記 (5分毎syncからの入口)。

    - cutoff = max(CUSTOMER_SHEET_START, 現在-72h)。STARTは運用開始日で
      過去分の大量流入を防ぎ(R2)、72hルックバックはランナー停止時の
      取りこぼしを冪等性(シート側ID突合)前提で回収する。stateファイル
      不要 = Actionsキャッシュ消失でも遡及事故が起きない設計。
    - 1社の失敗は捕捉して次の社へ (本線の応募取込を道連れにしない)。
    """
    allow = sorted(allowed_sheets())
    if not allow:
        print("[customer_sheet_sync] 許可リスト空 → 転記スキップ", flush=True)
        return {"sheets": 0, "ok": 0, "fail": 0, "wrote": 0, "errors": []}
    start = os.environ.get("CUSTOMER_SHEET_START", "").strip()
    lookback = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    cutoff = max(start, lookback) if start else lookback
    ok = fail = wrote = 0
    errors: list = []
    for sid in allow:
        try:
            s = sync(sid, cutoff, dry_run=dry_run)
            ok += 1
            wrote += s.get("書込完了", 0)
        except Exception as e:  # noqa: BLE001 — 1社の異常で全体を止めない
            fail += 1
            errors.append(f"{sid[:10]}…: {type(e).__name__}: {str(e)[:60]}")
            print(f"[customer_sheet_sync] ❌ {sid[:10]}… "
                  f"{type(e).__name__}: {str(e)[:80]}", flush=True)
        time.sleep(0.3)
    res = {"sheets": len(allow), "ok": ok, "fail": fail, "wrote": wrote,
           "errors": errors, "cutoff": cutoff, "dry_run": dry_run}
    print(f"[customer_sheet_sync] run_all: {len(allow)}シート 成功={ok} "
          f"失敗={fail} 追記={wrote}行 (cutoff={cutoff})", flush=True)
    return res


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--sheet-id", required=True)
    p.add_argument("--cutoff", default="",
                   help="この日時(UTC ISO, …Z)以降に作成された応募のみ。"
                        "既定=JST本日0時をUTCに変換")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--actual", action="store_true")
    a = p.parse_args(argv)
    if a.cutoff:
        cutoff = a.cutoff
    else:
        # 多角レビューG1: UTC当日0時だとJST 00:00-09:00 の応募が翌UTC日に
        # cutoff下へ沈み取りこぼす。JSTの本日0時をUTCへ変換して使う。
        jst_midnight = datetime.now(_JST).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
        cutoff = jst_midnight.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    sync(a.sheet_id, cutoff, dry_run=not a.actual, limit=a.limit)


if __name__ == "__main__":
    main()
