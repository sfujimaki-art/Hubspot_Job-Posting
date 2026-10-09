# -*- coding: utf-8 -*-
"""HRハッカーの応募者項目 → HubSpot 応募者 (0-421) プロパティの対応表 (2026-10-09)。

★ここが唯一の対応表。項目を足す・プロパティ名を決めるときはこのファイルだけ直す。

二つの取得元
  1. 応募者CSV (毎回取れる)        : CSV_FIELD_MAP
  2. 応募者の詳細ページ (1人ずつ)  : PAGE_FIELD_MAP
     CSV に無い項目 (メモ・選考履歴・自由項目の「質問文」) はここにしか無い。

書き込みの方針 (policy)
  "fill_empty" : 人も触るプロパティ。HubSpot 側が空のときだけ書く (手修正を潰さない)
  "hr_copy"    : HR のコピー専用で人が触らないプロパティ。CSV に値があり、
                 HubSpot の値と違えば更新する

プロパティ名が None の項目は「まだ対応先が無い」。何も書かない。
→ 全部 None のままなら、詳細ページの取得も丸ごと省く (Actions の時間を使わない)。

似た項目でも中身が違うものは重複ではない
  例: CSV の「メールアドレス」(meeruadoresu) と、自由項目の回答
  「メールアドレス（indeed メール以外）」は別物。後者は qa に入れ、
  meeruadoresu とは突き合わせも統合もしない。

HubSpot に無いプロパティを書くとレコードごと拒否されるため、
書く前に必ず filter_known_props で 0-421 に実在するものだけに絞る。

公開ログ (Actions) には件数だけ出す。氏名・電話・メール・応募者ID・自由記述は出さない。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterable, Optional

FILL_EMPTY = "fill_empty"
HR_COPY = "hr_copy"

# ---------------------------------------------------------------- 対応表
# CSV列名 -> (HubSpotプロパティ名 or None, policy)
# 載せていない列: 更新日 / 店舗ID / 店舗名 (不要)、自由項目1-3 (詳細ページで質問文と対にする)
CSV_FIELD_MAP: dict = {
    "学歴": ("gakureki", FILL_EMPTY),
    "連絡希望日": ("renrakukanouyoubijikantai", FILL_EMPTY),
    # --- 対応先のプロパティは後で設計・作成する (今は何も書かない) ---
    "応募者id": (None, HR_COPY),
    "応募経路": (None, HR_COPY),
    "現在の職業": (None, HR_COPY),
    "連絡方法": (None, HR_COPY),
    "見学会希望有無": (None, HR_COPY),
    "見学会希望日": (None, HR_COPY),
    "希望面接形態": (None, HR_COPY),
    "勤務可能期間": (None, HR_COPY),
    "選考ステータス": (None, HR_COPY),
    "面接予定日": (None, HR_COPY),
    "選考理由": (None, HR_COPY),
}

# 詳細ページの項目 -> (HubSpotプロパティ名 or None, policy)
#   qa                : 「質問文：回答」を1行ずつ (自由項目1-3)
#   memo              : メモ欄
#   selection_history : 「日時／選考種別／選考理由」を1行ずつ
PAGE_FIELD_MAP: dict = {
    "qa": (None, FILL_EMPTY),
    "memo": (None, FILL_EMPTY),
    "selection_history": (None, FILL_EMPTY),
}

HR_ID_COLUMN = "応募者id"


# ---------------------------------------------------------------- 方針の引き当て
def policy_for(prop: str) -> str:
    """プロパティ名から書き込み方針を引く。対応表に無ければ安全側の fill_empty。"""
    for maps in (CSV_FIELD_MAP, PAGE_FIELD_MAP):
        for p, pol in maps.values():
            if p == prop:
                return pol
    return FILL_EMPTY


def plan_property_updates(wanted: dict, current: dict) -> dict:
    """wanted(書きたい値) と current(HubSpotの現在値) から、実際に書く分を返す。

    - 空の値は絶対に書かない
    - fill_empty : 現在値が空のときだけ
    - hr_copy    : 現在値と違うときだけ
    """
    out = {}
    for prop, val in wanted.items():
        v = "" if val is None else str(val).strip()
        if not v:
            continue
        cur = str(current.get(prop) or "").strip()
        if policy_for(prop) == HR_COPY:
            if cur != v:
                out[prop] = v
        elif not cur:
            out[prop] = v
    return out


# ---------------------------------------------------------------- Stage 1: CSV
def csv_extra_from_raw(raw: dict) -> dict:
    """HR生CSV1行 -> {HubSpotプロパティ名: 値}。対応先が無い・値が空の項目は含めない。"""
    out: dict = {}
    for col, (prop, _pol) in CSV_FIELD_MAP.items():
        if not prop or prop in out:
            continue
        v = str(raw.get(col) or "").strip()
        if v:
            out[prop] = v
    return out


def filter_known_props(props: dict, known: Optional[Iterable[str]]) -> tuple:
    """0-421 に実在しないプロパティを落とす。 -> (残った dict, 落とした名前のリスト)。

    known が None (プロパティ一覧を取れなかった) のときは全部落とす。
    実在しない名前を1つでも書くと HubSpot がレコードごと拒否するため。
    """
    if known is None:
        return {}, sorted(props)
    ks = set(known)
    kept = {k: v for k, v in props.items() if k in ks}
    return kept, sorted(k for k in props if k not in ks)


# ---------------------------------------------------------------- Stage 2: 詳細ページ
class HrLoginPageError(Exception):
    """詳細ページの代わりにログイン画面が返った (セッション切れ)。"""


def active_page_fields(known: Optional[Iterable[str]]) -> dict:
    """詳細ページから取る価値のある項目 {項目名: プロパティ名}。

    対応先が未設定、または HubSpot に実在しない項目は含めない。
    空なら詳細ページの取得は丸ごと省く。
    """
    if known is None:
        return {}
    ks = set(known)
    return {f: p for f, (p, _pol) in PAGE_FIELD_MAP.items() if p and p in ks}


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class _DetailParser(HTMLParser):
    """編集フォームから 質問文+回答 / メモ / 選考履歴 を拾う。

    構造 (実ページ 2026-10-09 で確認):
      li.box_fill_list_item > div.box_fill_list_item_left > p.name  = 質問文(項目名)
                            > div.box_fill_list_item_right > ... > textarea[name=free_text_N|memo]
      table.history > tr > td x3 (日時 / 選考種別 / 選考理由)  ※見出し行は th
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.is_edit_form = False
        self.has_password = False
        self.last_question = ""
        self._in_name_p = False
        self._name_buf: list = []
        self._ta: Optional[str] = None
        self._ta_buf: list = []
        self.free: dict = {}          # N -> (質問文, 回答)
        self.memo = ""
        self._in_history = False
        self._row: Optional[list] = None
        self._cell: Optional[list] = None
        self.history: list = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "form" and "/admin/applicants/edit/" in (a.get("action") or ""):
            self.is_edit_form = True
        elif tag == "input" and (a.get("type") or "").lower() == "password":
            self.has_password = True
        elif tag == "p" and "name" in cls:
            self._in_name_p = True
            self._name_buf = []
        elif tag == "br" and self._in_name_p:
            self._name_buf.append(" ")
        elif tag == "textarea":
            n = a.get("name") or ""
            if re.fullmatch(r"free_text_[1-3]", n) or n == "memo":
                self._ta, self._ta_buf = n, []
        elif tag == "table" and "history" in cls:
            self._in_history = True
        elif tag == "tr" and self._in_history:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_name_p:
            self._in_name_p = False
            self.last_question = _squash("".join(self._name_buf))
        elif tag == "textarea" and self._ta:
            body = "".join(self._ta_buf).replace("\r\n", "\n").replace("\r", "\n").strip()
            if self._ta == "memo":
                self.memo = body
            else:
                self.free[int(self._ta[-1])] = (self.last_question, body)
            self._ta = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(_squash("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and any(self._row):
                self.history.append(tuple((self._row + ["", "", ""])[:3]))
            self._row = None
        elif tag == "table" and self._in_history:
            self._in_history = False

    def handle_data(self, data):
        if self._in_name_p:
            self._name_buf.append(data)
        if self._ta is not None:
            self._ta_buf.append(data)
        if self._cell is not None:
            self._cell.append(data)


def parse_applicant_detail(html: str) -> Optional[dict]:
    """応募者詳細ページ(編集フォーム) -> {"qa": [(質問, 回答)], "memo": str,
    "selection_history": [(日時, 種別, 理由)]}。

    - ログイン画面なら HrLoginPageError (呼び出し側で再ログインして取り直す)
    - どちらでもない (想定外のページ) なら None
    - 回答が空の自由項目は飛ばす。質問文が空なら「自由項目N」を使う
    """
    p = _DetailParser()
    p.feed(html or "")
    p.close()
    if not p.is_edit_form:
        if p.has_password or "accounts-invision" in (html or ""):
            raise HrLoginPageError("ログイン画面が返りました")
        return None
    qa = []
    for n in sorted(p.free):
        q, ans = p.free[n]
        if not ans:
            continue
        qa.append((q or f"自由項目{n}", ans))
    return {"qa": qa, "memo": p.memo, "selection_history": list(p.history)}


def format_page_values(parsed: dict) -> dict:
    """parse_applicant_detail の結果 -> {項目名: 書き込み用テキスト}。空の項目は含めない。"""
    out = {}
    qa = "\n".join(f"{q}：{a}" for q, a in parsed.get("qa", []))
    if qa:
        out["qa"] = qa
    memo = (parsed.get("memo") or "").strip()
    if memo:
        out["memo"] = memo
    hist = "\n".join("／".join(x for x in row if x)
                     for row in parsed.get("selection_history", []))
    if hist.strip():
        out["selection_history"] = hist
    return out


def page_props_from_parsed(parsed: dict, fields: dict) -> dict:
    """詳細ページの値 -> {プロパティ名: 値}。fields は active_page_fields の結果。

    同じプロパティに複数の項目が向く場合は改行でつなぐ。
    """
    vals = format_page_values(parsed)
    out: dict = {}
    for f, prop in fields.items():
        v = vals.get(f)
        if v:
            out[prop] = f"{out[prop]}\n{v}" if prop in out else v
    return out


def apply_page_fields(client, appointment_id: str, parsed: dict, fields: dict) -> str:
    """詳細ページの値を応募者に書く。 -> "written" / "nothing" / "error"。

    現在値を取れなかったとき ("error") は何も書かない (空と見て上書きしない)。
    """
    wanted = page_props_from_parsed(parsed, fields)
    if not wanted:
        return "nothing"
    try:
        cur = client.get_appointment_props_strict(appointment_id, list(wanted))
        upd = plan_property_updates(wanted, cur)
        if upd:
            client.update_appointment(appointment_id, upd)
            return "written"
        return "nothing"
    except Exception:  # noqa: BLE001
        return "error"


# ---------------------------------------------------------------- 取得対象の選び方
DETAIL_KIND = "hr_detail"
DETAIL_MAX_ATTEMPTS = 3


def select_detail_ids(ledger, ids: Iterable[str], max_n: int) -> tuple:
    """詳細ページを取りに行く応募者IDを選ぶ。 -> (選んだID, 完了済み件数, 諦めた件数)。

    - 完了済み (台帳 hr_detail が done) は二度と取らない
    - 3回失敗したものは諦める (件数だけ数える)
    - 並びは渡された順 (呼び出し側が新しい応募から並べる)。最大 max_n 件
    """
    picked: list = []
    done = gave_up = 0
    seen: set = set()
    for i in ids:
        if not i or i in seen:
            continue
        seen.add(i)
        m = ledger.meta(DETAIL_KIND, i)
        if m.get("detail_status") == "done":
            done += 1
        elif int(m.get("attempts", 0)) >= DETAIL_MAX_ATTEMPTS:
            gave_up += 1
        elif len(picked) < max_n:
            picked.append(i)
    return picked, done, gave_up


def record_detail_failure(ledger, hr_id: str) -> int:
    n = int(ledger.meta(DETAIL_KIND, hr_id).get("attempts", 0)) + 1
    ledger.set_meta(DETAIL_KIND, hr_id, detail_status="pending", attempts=n)
    return n


def record_detail_done(ledger, hr_id: str) -> None:
    ledger.set_meta(DETAIL_KIND, hr_id, detail_status="done")

