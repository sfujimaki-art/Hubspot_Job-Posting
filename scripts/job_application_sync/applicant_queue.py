"""応募連携トリガー: 集約スプレッドシート(queue)検知 + アカウント突合 + 集約.

WBS 1.11.9 スコープ③。設計正本: docs/wbs_outputs/1.11.9_媒体CSV同期実装/
応募連携_トリガーとBAN対策_設計_2026-07-07.md

役割 (すべて読み取り専用 = 副作用なし):
  1. read_new_items()      : 集約シートの queue タブ status=NEW を検知・構造化
  2. AccountResolver       : queue項目 → 企業(B系 AirWork)認証へ複合キー突合
                             (エイリアス→企業名完全一致→A系ID→B系ID→管理メール、fuzzy無し=§24準拠)
  3. aggregate_by_account(): 同一アカウントの複数応募を1グループに集約(BAN対策)

env:
  JAS_APPLICANT_QUEUE_SHEET_ID : 集約シートID (queueタブを持つ)
  ※ 公開リポ方針: シートIDは直書きせず env 注入
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .fetchers import account_loader as al

# ---- 集約シート (queue) -----------------------------------------------------
QUEUE_SHEET_ID = os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", "")
QUEUE_TAB = "queue"

# queue 列 index (実測 2026-07-07)
Q_ID = 0
Q_STATUS = 1
Q_PAYLOAD = 7
Q_ROUTE = 8

# account_loader シート 列 index (実測 2026-07-07)
A_COMP = 2
A_CLOSED = 7
A_RECLOG = 8      # リクロジアドレス (弊社割当の一意顧客キー。ヘッダは's'に改変されているが本来これ)
A_AID = 10        # AirWork ID (A系)
A_BID = 15        # 企業AirWork ID (B系)
A_BPW = 16        # 企業Airwork PW (B系)
A_ALIAS = 17      # エイリアスアドレス


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _split_multi(s: str) -> list[str]:
    """改行/カンマ区切りの複数値を分割. 'ID：xxx' の接頭辞も除去."""
    out = []
    for x in re.split(r"[\n,]", s or ""):
        x = re.sub(r"^ID[：:]\s*", "", x.strip())
        if x:
            out.append(x)
    return out


def _sheets_client(retries: int = 6):
    """Sheets 503 一時障害に耐えるリトライ付きクライアント取得."""
    last = None
    for _ in range(retries):
        try:
            return al.get_sheets_client()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(6)
    raise RuntimeError(f"Sheets client 取得失敗(リトライ尽き): {last}")


# ============================================================================
# 1. queue 検知
# ============================================================================
@dataclass
class QueueItem:
    row_id: str                 # queue の id (JOB-xxx) or シート1直読み時は内容ハッシュ
    media_type: str             # mediaType (HRハッカー / Airワーク... / ...)
    login_id: str               # route.loginId (突合キー)
    company: str                # payload.columns.G (企業名)
    columns: dict = field(default_factory=dict)
    sheet_b_id: str = ""
    sheet_row: int = 0          # シート1直読み時の行番号 (処理後F列マーク用。0=queue由来)

    @property
    def media(self) -> str:
        """媒体を正規化 (HR / AW / OTHER)."""
        mt = self.media_type.lower()
        if "hr" in mt or "ハッカー" in self.media_type:
            return "HR"
        if "airwork" in mt or "ワーク" in self.media_type or "rct.airwork" in mt:
            return "AW"
        return "OTHER"


def read_new_items(sheet_id: str = "") -> list[QueueItem]:
    """集約シート queue の status=NEW を検知して構造化して返す (読み取りのみ)."""
    sid = sheet_id or QUEUE_SHEET_ID
    if not sid:
        raise RuntimeError("JAS_APPLICANT_QUEUE_SHEET_ID 未設定")
    gc = _sheets_client()
    vals = gc.open_by_key(sid).worksheet(QUEUE_TAB).get_all_values()
    items: list[QueueItem] = []
    for r in vals[1:]:
        if len(r) <= Q_ROUTE or r[Q_STATUS] != "NEW":
            continue
        try:
            pl = json.loads(r[Q_PAYLOAD])
            rt = json.loads(r[Q_ROUTE])
        except (json.JSONDecodeError, ValueError):
            continue
        cols = pl.get("columns", {}) or {}
        items.append(QueueItem(
            row_id=r[Q_ID],
            media_type=pl.get("mediaType", ""),
            login_id=rt.get("loginId", ""),
            company=cols.get("G", ""),
            columns=cols,
            sheet_b_id=rt.get("sheetBId", ""),
        ))
    return items


# ============================================================================
# 2. アカウント突合 (queue項目 → 企業B系認証)
# ============================================================================
@dataclass
class ResolvedAccount:
    company: str
    b_ids: list[str]            # 企業AirWork ID (複数の場合あり)
    b_pw: str                   # 企業Airwork PW
    closed: bool
    matched_by: str             # 突合に使ったキー


class AccountResolver:
    """account_loader シートから複合キー逆引き index を構築し、
    queue項目を企業(B系)認証へ突合する。完全一致のみ(fuzzy無し=§24準拠)。"""

    def __init__(self) -> None:
        self.idx_alias: dict[str, list] = {}
        self.idx_bid: dict[str, list] = {}
        self.idx_aid: dict[str, list] = {}
        self.idx_reclog: dict[str, list] = {}
        self.idx_comp: dict[str, list] = {}

    def build(self) -> "AccountResolver":
        gc = _sheets_client()
        av = gc.open_by_key(al.SHEET_ID).sheet1.get_all_values()
        for r in av[1:]:
            g = lambda i: r[i] if len(r) > i else ""  # noqa: E731
            for a in _split_multi(g(A_ALIAS)):
                if "@" in a:
                    self.idx_alias[_norm(a)] = r
            for b in _split_multi(g(A_BID)):
                self.idx_bid[_norm(b)] = r
            if g(A_AID).strip():
                self.idx_aid[_norm(g(A_AID))] = r
            if g(A_RECLOG).strip():
                self.idx_reclog[_norm(g(A_RECLOG))] = r
            if g(A_COMP).strip():
                self.idx_comp[_norm(g(A_COMP))] = r
        return self

    def resolve(self, item: QueueItem) -> Optional[ResolvedAccount]:
        """queue項目 → ResolvedAccount。突合できなければ None (=要報告)."""
        lid = _norm(item.login_id)
        row, by = None, ""
        for key, idx, name in (
            (lid, self.idx_reclog, "リクロジアドレス"),   # 正規の一意顧客キー(最優先)
            (lid, self.idx_alias, "alias"),
            (_norm(item.company), self.idx_comp, "company_exact"),
            (lid, self.idx_aid, "a_id"),
            (lid, self.idx_bid, "b_id"),
        ):
            if key and key in idx:
                row, by = idx[key], name
                break
        if row is None:
            return None
        g = lambda i: row[i] if len(row) > i else ""  # noqa: E731
        b_ids = _split_multi(g(A_BID))
        return ResolvedAccount(
            company=g(A_COMP),
            b_ids=b_ids,
            b_pw=g(A_BPW),
            closed=g(A_CLOSED).upper() == "TRUE",
            matched_by=by,
        )


# ============================================================================
# 3. アカウント集約 (BAN対策: 同一アカウント複数応募を1グループに)
# ============================================================================
def aggregate_by_account(
    items: list[QueueItem], resolver: AccountResolver
) -> tuple[dict[str, list[QueueItem]], list[QueueItem]]:
    """(media, 突合キー) 単位で集約。
    Returns: (集約dict{account_key: [items]}, 未突合items)。
    未突合は放置ゼロ原則で呼出側が報告する。"""
    grouped: dict[str, list[QueueItem]] = {}
    unresolved: list[QueueItem] = []
    for it in items:
        if it.media == "OTHER":
            unresolved.append(it)      # 他媒体はスコープ外→報告対象
            continue
        if it.media == "AW":
            acc = resolver.resolve(it)
            if acc is None:
                unresolved.append(it)
                continue
            key = f"AW::{acc.company}"
        else:  # HR = 1マスター集約 (顧客で分けず媒体単位)
            key = "HR::master"
        grouped.setdefault(key, []).append(it)
    return grouped, unresolved


# ============================================================================
# 4. シート1 直読み (GAS queue投入の座礁を迂回, 2026-07-21)
# ============================================================================
# GASの enqueue が行番号(AIRWORK_LAST_PROCESSED_ROW)で座礁し queue に新規が乗らない
# ため、こちら側で「シート1」を直接読み、F列マーカー(行削除に強い)で未処理を検知する。
SHEET1_TAB = "シート1"
S1_SUBJECT = 0   # A: 件名
S1_MEDIA = 1     # B: 媒体 (admin@hr-hacker.com / Airワーク... )
S1_FROM = 2      # C: 差出人(loginId抽出元)
S1_DATE = 3      # D: 日付
S1_MARK = 5      # F: マーカー (キュー済/済/受入済)
S1_COMPANY = 6   # G: 企業名
# 既存GASマーカー + こちら側マーカー(処理済)
S1_MARKS_DONE = ("キュー済", "済", "受入済")
S1_MARK_ACCEPTED = "受入済"   # こちら側で処理した印
_S1_APP_SUBJECTS = ("応募がありました", "応募通知メール")


def _first_email(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"<\s*([^>]+@\S+)\s*>", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)
    return m.group(0).strip() if m else ""


def _media_type_from_b(b: str) -> str:
    b = (b or "").strip()
    if b == "admin@hr-hacker.com":
        return "HRハッカー"
    if "airwork" in b.lower() or "Airワーク" in b:
        return "Airワーク 採用管理"
    return b


def _s1_date_recent(d: str, cutoff_iso: str) -> bool:
    """'2026/7/21' 形式を ISO 比較。cutoff_iso 以降なら True。空/不正は False。"""
    d = (d or "").strip()
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", d)
    if not m:
        return False
    iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return iso >= cutoff_iso


def read_new_items_from_sheet1(
    sheet_id: str = "", *, cutoff_iso: str = "", media_filter: str = "HR",
    limit: Optional[int] = None,
) -> list[QueueItem]:
    """シート1を直読みし、F列が未処理(空)の応募行から QueueItem を構築 (GAS迂回)。

    cutoff_iso: この日付(ISO 'YYYY-MM-DD')以降の応募のみ (空=全件)。
    media_filter: 'HR'/'AW'/'BOTH'。既定HR (AWは認証別対応まで保留)。
    行番号(sheet_row)を各itemに付与し、処理後 mark_sheet1_rows でF列にマークする。
    """
    sid = sheet_id or QUEUE_SHEET_ID
    if not sid:
        raise RuntimeError("JAS_APPLICANT_QUEUE_SHEET_ID 未設定")
    gc = _sheets_client()
    ws = gc.open_by_key(sid).worksheet(SHEET1_TAB)
    # 必要列 A〜G のみ範囲取得(全列get_all_valuesは52k行で重い)。
    # 返り値の各行 index: A=0,B=1,C=2,D=3,E=4,F=5,G=6 (S1_* 定数と一致)
    vals = ws.get("A2:G")  # 既定=FORMATTED_VALUE (日付は '2026/7/21' 表示)
    items: list[QueueItem] = []
    import hashlib
    for i, r in enumerate(vals, start=2):  # 範囲はA2開始=先頭がデータ行2
        def g(idx: int) -> str:
            return (str(r[idx]) if len(r) > idx and r[idx] is not None
                    else "").strip()
        subj = g(S1_SUBJECT)
        if not any(k in subj for k in _S1_APP_SUBJECTS):
            continue
        if g(S1_MARK) in S1_MARKS_DONE:
            continue  # 既に処理済(F列マーカー=行削除に強い判定)
        media_type = _media_type_from_b(g(S1_MEDIA))
        mt = media_type.lower()
        media = ("HR" if ("hr" in mt or "ハッカー" in media_type)
                 else "AW" if ("airwork" in mt or "ワーク" in media_type)
                 else "OTHER")
        if media_filter != "BOTH" and media != media_filter:
            continue
        date = g(S1_DATE)
        if cutoff_iso and not _s1_date_recent(date, cutoff_iso):
            continue
        company = re.sub(r"[（(].+?[)）]", "", g(S1_COMPANY)).strip()
        # 安定ID = 内容ハッシュ (行番号非依存=削除に強い)
        rid = "S1-" + hashlib.md5(
            f"{media_type}|{g(S1_FROM)}|{date}|{subj}|{company}".encode("utf-8")
        ).hexdigest()[:16]
        items.append(QueueItem(
            row_id=rid, media_type=media_type,
            login_id=_first_email(g(S1_FROM)), company=company,
            columns={"A": subj, "B": g(S1_MEDIA), "C": g(S1_FROM),
                     "D": date, "G": company},
            sheet_row=i,
        ))
        if limit and len(items) >= limit:
            break
    return items


def mark_sheet1_rows(rows: list[int], *, sheet_id: str = "",
                     marker: str = S1_MARK_ACCEPTED) -> int:
    """シート1 の指定行の F列 にマーカーを書き込む(処理済=再取込防止)。件数を返す。"""
    rows = sorted(set(r for r in rows if r and r > 1))
    if not rows:
        return 0
    sid = sheet_id or QUEUE_SHEET_ID
    gc = _sheets_client()
    ws = gc.open_by_key(sid).worksheet(SHEET1_TAB)
    # F列(6)を1件ずつ更新(連続範囲でないため)。gspread batch_update で1リクエスト化。
    data = [{"range": f"F{r}", "values": [[marker]]} for r in rows]
    ws.batch_update(data)
    return len(rows)
