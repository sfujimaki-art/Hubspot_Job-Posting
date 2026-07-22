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
# --- 列位置は「ハードコードしない」(運用者が列を挿入/削除してもズレる, 2026-07-22) ---
# ヘッダ名で引ける列は名前で、ヘッダ空の列(件名/日付/マーカー)は内容で特定する。
# 過去、G列(担当CS=人名)を会社名と取り違えてAW突合が全滅した反省(位置依存の罠)。
_S1_HDR_MEDIA = ("媒体",)               # 媒体 (admin@hr-hacker.com / Airワーク…)
_S1_HDR_FROM = ("顧客", "差出人")        # 差出人(loginId抽出元)
_S1_HDR_COMPANY = ("応募管理シート",)    # 会社名(アカウント情報シートと突合する本丸)
# 既存GASマーカー + こちら側マーカー(処理済)。'キュー済'=GASの enqueue 済み印。
S1_MARKS_DONE = ("キュー済", "済", "受入済")
S1_MARK_ENQUEUED = "キュー済"   # マーカー列の一意特定に使う(チェック列の"済"と区別)
S1_MARK_ACCEPTED = "受入済"     # こちら側で処理した印
_S1_APP_SUBJECTS = ("応募がありました", "応募通知メール")
# 列挿入に耐えるため header/data はやや広め(A:Q)に読む。
_S1_READ_RANGE = "A1:Q"


def _norm_header(h: str) -> str:
    return (h or "").strip().replace(" ", "").replace("　", "")


def _col_letter(idx: int) -> str:
    """0始まり列index → A1記法の列文字 (0→A, 25→Z, 26→AA)。"""
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _resolve_s1_columns(header: list, rows: list) -> dict:
    """シート1の列位置を「ヘッダ名 + 内容」で動的特定し {name: index|None} を返す。

    - 媒体/差出人/会社名: ヘッダ名で引く(_S1_HDR_*)。
    - 件名/日付/マーカー: ヘッダが空なので内容パターンで特定
      (件名='応募がありました'を含む / 日付='YYYY/M/D' / マーカー='キュー済'を含む)。
    列を挿入・削除されても壊れない。見つからない列は None(呼び出し側で警告)。
    """
    hmap: dict = {}
    for i, h in enumerate(header):
        key = _norm_header(str(h))
        if key and key not in hmap:
            hmap[key] = i

    def by_name(names):
        for n in names:
            if n in hmap:
                return hmap[n]
        return None

    cols = {
        "media": by_name(_S1_HDR_MEDIA),
        "from": by_name(_S1_HDR_FROM),
        "company": by_name(_S1_HDR_COMPANY),
        "subject": None, "date": None, "marker": None,
    }
    taken = {v for v in cols.values() if v is not None}
    ncol = max((len(r) for r in rows), default=0)
    for ci in range(ncol):
        if ci in taken:
            continue
        col_vals = [str(r[ci]).strip() for r in rows
                    if len(r) > ci and r[ci] not in (None, "")]
        if not col_vals:
            continue
        if cols["subject"] is None and any(
                any(k in v for k in _S1_APP_SUBJECTS) for v in col_vals):
            cols["subject"] = ci
            continue
        # マーカー列は「キュー済」を含む列で一意特定(チェック列の"済"と混同しない)。
        if cols["marker"] is None and any(
                S1_MARK_ENQUEUED in v for v in col_vals):
            cols["marker"] = ci
            continue
        # 日付列: 過半数が YYYY/M/D 形式の列。
        if cols["date"] is None:
            hit = sum(1 for v in col_vals
                      if re.match(r"\d{4}/\d{1,2}/\d{1,2}", v))
            if hit and hit >= len(col_vals) // 2:
                cols["date"] = ci
    return cols


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
    """シート1を直読みし、マーカー列が未処理の応募行から QueueItem を構築 (GAS迂回)。

    列位置はヘッダ名+内容で動的特定(_resolve_s1_columns)。列を挿入/削除されても壊れない。
    cutoff_iso: この日付(ISO 'YYYY-MM-DD')以降の応募のみ (空=全件)。
    media_filter: 'HR'/'AW'/'BOTH'。既定HR (AWは認証別対応まで保留)。
    行番号(sheet_row)を各itemに付与し、処理後 mark_sheet1_rows でマーカー列に書く。
    """
    sid = sheet_id or QUEUE_SHEET_ID
    if not sid:
        raise RuntimeError("JAS_APPLICANT_QUEUE_SHEET_ID 未設定")
    gc = _sheets_client()
    ws = gc.open_by_key(sid).worksheet(SHEET1_TAB)
    # header + data を A:Q で取得(列挿入headroom)。全列get_all_valuesは52k行で重い。
    vals = ws.get(_S1_READ_RANGE)  # 既定=FORMATTED_VALUE
    if not vals:
        return []
    header, data = vals[0], vals[1:]
    cols = _resolve_s1_columns(header, data)
    # 突合キー(会社名)と応募判定(件名)は必須。欠けたら位置依存へ落とさず明示エラー。
    missing = [k for k in ("subject", "media", "from", "company")
               if cols.get(k) is None]
    if missing:
        raise RuntimeError(
            f"シート1の列をヘッダ名/内容で特定できず: {missing} "
            f"(header={[_norm_header(str(h)) for h in header]}) — "
            f"'媒体'/'顧客'/'応募管理シート' 見出しと件名列を確認")

    def col(name: str):
        return cols.get(name)

    items: list[QueueItem] = []
    import hashlib
    for i, r in enumerate(data, start=2):  # data先頭=シート行2
        def g(name: str) -> str:
            idx = col(name)
            if idx is None:
                return ""
            return (str(r[idx]) if len(r) > idx and r[idx] is not None
                    else "").strip()
        subj = g("subject")
        if not any(k in subj for k in _S1_APP_SUBJECTS):
            continue
        if g("marker") in S1_MARKS_DONE:
            continue  # 既に処理済(マーカー列=行削除に強い判定)
        media_type = _media_type_from_b(g("media"))
        mt = media_type.lower()
        media = ("HR" if ("hr" in mt or "ハッカー" in media_type)
                 else "AW" if ("airwork" in mt or "ワーク" in media_type)
                 else "OTHER")
        if media_filter != "BOTH" and media != media_filter:
            continue
        date = g("date")
        if cutoff_iso and not _s1_date_recent(date, cutoff_iso):
            continue
        company = re.sub(r"[（(].+?[)）]", "", g("company")).strip()
        # 安定ID = 内容ハッシュ (行番号非依存=削除に強い)
        rid = "S1-" + hashlib.md5(
            f"{media_type}|{g('from')}|{date}|{subj}|{company}".encode("utf-8")
        ).hexdigest()[:16]
        items.append(QueueItem(
            row_id=rid, media_type=media_type,
            login_id=_first_email(g("from")), company=company,
            columns={"A": subj, "B": g("media"), "C": g("from"),
                     "D": date, "G": company},
            sheet_row=i,
        ))
        if limit and len(items) >= limit:
            break
    return items


def mark_sheet1_rows(rows: list[int], *, sheet_id: str = "",
                     marker: str = S1_MARK_ACCEPTED) -> int:
    """シート1 の指定行のマーカー列にマーカーを書き込む(処理済=再取込防止)。件数を返す。

    マーカー列は位置固定でなくヘッダ行から動的特定する(列挿入/削除に強い)。
    """
    rows = sorted(set(r for r in rows if r and r > 1))
    if not rows:
        return 0
    sid = sheet_id or QUEUE_SHEET_ID
    gc = _sheets_client()
    ws = gc.open_by_key(sid).worksheet(SHEET1_TAB)
    # マーカー列を特定(ヘッダ空のためデータ内容='キュー済'から引く)。
    probe = ws.get(_S1_READ_RANGE + "1000")  # 先頭~1000行で列特定(52k全読み回避)
    header = probe[0] if probe else []
    mcol = _resolve_s1_columns(header, probe[1:] if probe else []).get("marker")
    letter = _col_letter(mcol) if mcol is not None else "F"  # 不明時のみ従来F
    data = [{"range": f"{letter}{r}", "values": [[marker]]} for r in rows]
    ws.batch_update(data)
    return len(rows)
