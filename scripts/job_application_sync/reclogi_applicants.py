# -*- coding: utf-8 -*-
"""リクロジ (自社求人サイト reclogi.jp) の応募を応募者 (0-421) に取り込む (2026-10-20)。

流れ
  1. 応募メールの本文は GAS が抜き出し、「応募者情報管理シート」
     (JAS_APPLICANT_QUEUE_SHEET_ID = シート1 と同じブック) のタブ「リクロジ応募」へ
     1応募1行で追記する。ここはそのタブを読むだけ。
  2. 「HubSpot登録」が空の行だけを、既存の applicant_import の流れ
     (重複判定・担当者・取引からの写し・一次対応・古い応募の扱い・ピン留めメモ・紐付け)
     にそのまま通す。媒体名は「リクロジメディア」。
  3. 求人は HRハッカー求人ID で LISTING (id_hrhakkaa) を引く。リクロジの求人は
     HRハッカーの求人で、求人ページの「求人ID」欄に
     「[リクロジ求人ID] 1 - 店舗ID - HRハッカー求人ID」と載っている
     (2026-10-20 実ページ10件で、店舗IDが HubSpot の id_shop_hrhakkaa と10/10一致)。
  4. 見つからないときだけ、掲載求人URLのページから求人を作る。プロパティは
     HRハッカー取込 (hrhacker_import.build_create_props) に合わせる。
     id_hrhakkaa を入れるので、後で HRハッカー取込が同じレコードを更新する
     (取込の upsert キーは id_hrhakkaa。find_hubspot_jobs で引けたものは更新に回る)。
  5. 結果を最後の2列「HubSpot登録」「HubSpot応募者ID」に書き戻す。

列は**ヘッダ名で引く** (現場が列を足すことがある)。
タブが無いときは警告を出して何もしない (GAS 反映前でも落ちない)。
dry-run では HubSpot にもシートにも書かない (求人の検索とページの取得=読むだけは行う)。

公開ログ (Actions) には件数だけ出す。氏名・電話・メール・会社名は出さない。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable, Optional

try:  # パッケージ実行/スクリプト直実行の両対応 (CIは直実行)
    from . import applicant_import as ai
    from . import applicant_queue as aq
    from . import hrhacker_import as hi
    from . import private_log as plog
    from .airwork_import import _KYUUYO_KEITAI_OK
    from .fetchers import account_loader as al
except ImportError:  # pragma: no cover
    from scripts.job_application_sync import applicant_import as ai  # type: ignore
    from scripts.job_application_sync import applicant_queue as aq  # type: ignore
    from scripts.job_application_sync import hrhacker_import as hi  # type: ignore
    from scripts.job_application_sync import private_log as plog  # type: ignore
    from scripts.job_application_sync.airwork_import import _KYUUYO_KEITAI_OK  # type: ignore
    from scripts.job_application_sync.fetchers import account_loader as al  # type: ignore

TAB = "リクロジ応募"
MEDIA = ai.RECLOGI_MEDIA

# ---- タブの列 (GAS が書くヘッダ名。位置は見ない) ----------------------------
C_MESSAGE_ID = "messageId"
C_RECEIVED = "受信日時"
C_APPLY_ID = "応募ID"
C_APPLIED_AT = "応募日時"
C_HR_JOB_ID = "HRハッカー求人ID"
C_SHOP_ID = "店舗id"
C_JOB_URL = "掲載求人URL"
C_LAST, C_FIRST = "姓", "名"
C_LAST_KANA, C_FIRST_KANA = "姓（カナ）", "名（カナ）"
C_GENDER = "性別"
C_BIRTH = "生年月日"
C_AGE = "年齢"
C_PHONE = "電話番号"
C_MAIL = "メールアドレス"
C_POSTAL = "郵便番号"
C_PREF = "都道府県"
C_CITY = "市区町村"
C_TOWN = "町域"
C_STREET = "番地・建物"
C_STATUS = "HubSpot登録"          # ここから2列だけ Python が書く
C_APPT_ID = "HubSpot応募者ID"
REQUIRED_COLUMNS = (C_MESSAGE_ID, C_STATUS, C_APPT_ID)

# 自由記述 → 応募者の既存プロパティ (0-421 に実在するものだけ。2026-10-20 確認)
#   本人希望記入欄・応募ID・応募ページURL・ユーザーID は入れる先が無いので入れない
EXTRA_COLUMNS = {
    "保有資格": "hoyuushikaku",        # 保有資格
    "自己PR": "jikopr",                # 自己PR
    "志望動機": "oubodouki",           # 応募動機
    C_JOB_URL: "oubosaki_kyuujin_url",  # 応募先求人URL (応募した求人のページ)
}

# ---- 「HubSpot登録」に書く値 --------------------------------------------------
ST_DONE = "受入済"
ST_UNLINKED = "求人未特定"         # 応募者は作ったが求人に紐付かない
ST_ERROR_PREFIX = "エラー: "
LEDGER_PREFIX = "RL-"

# 求人ページ。python-requests の既定 UA は 403 で弾かれる (2026-10-20 実測。curl/ブラウザは 200)
PAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reclogi-applicant-sync)"}
PAGE_TIMEOUT_SEC = 20


# ============================================================================
# タブの行
# ============================================================================
@dataclass
class ReclogiRecord:
    sheet_row: int                  # シート上の行番号 (1始まり。ヘッダが1)
    values: dict = field(default_factory=dict)   # {ヘッダ名: 値}

    def get(self, name: str) -> str:
        return str(self.values.get(name) or "").strip()

    @property
    def key(self) -> str:
        """台帳の鍵。messageId が無ければ応募ID。どちらも無ければ空 (処理しない)。"""
        mid = self.get(C_MESSAGE_ID) or self.get(C_APPLY_ID)
        return f"{LEDGER_PREFIX}{mid}" if mid else ""


def _norm_header(h) -> str:
    return re.sub(r"[\s　]", "", str(h or ""))


def header_index(header: list) -> dict:
    """{ヘッダ名: 列index}。前後の空白は無視する。同名があれば左を採る。"""
    out: dict = {}
    for i, h in enumerate(header):
        k = _norm_header(h)
        if k and k not in out:
            out[k] = i
    return out


def records_from_values(values: list) -> tuple:
    """get_all_values() の結果 → (ヘッダの {名前: index}, [ReclogiRecord])。"""
    if not values:
        return {}, []
    idx = header_index(values[0])
    recs = []
    for n, r in enumerate(values[1:], start=2):
        if not any(str(c or "").strip() for c in r):
            continue
        recs.append(ReclogiRecord(sheet_row=n, values={
            name: (r[i] if i < len(r) else "") for name, i in idx.items()}))
    return idx, recs


def is_target(rec: ReclogiRecord) -> bool:
    """処理するか。「HubSpot登録」が空の行だけ (受入済・求人未特定・エラー・現場の「対象外」は飛ばす)。"""
    return not rec.get(C_STATUS)


# ============================================================================
# 行 → ApplicantRow
# ============================================================================
_DATE_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")


def _iso_date(s: str) -> str:
    """'2026-10-14 12:54' / '2026/10/14' → '2026-10-14'。読めなければ空。"""
    m = _DATE_RE.search(str(s or ""))
    if not m:
        return ""
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _phone_digits(s: str) -> str:
    """数字だけにする。先頭の0が落ちた番号 (シートが数値として持ったもの) は0を戻す。

    国内の電話番号は必ず0で始まる。9〜10桁で0始まりでない値は、0が落ちた形。
    """
    d = ai.normalize_phone(s)
    if d and not d.startswith("0") and len(d) in (9, 10):
        d = "0" + d
    return d


def _join(*parts: str, sep: str = " ") -> str:
    return sep.join(p for p in (str(x or "").strip() for x in parts) if p)


def to_applicant_row(rec: ReclogiRecord) -> "ai.ApplicantRow":
    apply_date = _iso_date(rec.get(C_APPLIED_AT)) or _iso_date(rec.get(C_RECEIVED))
    birth = _iso_date(rec.get(C_BIRTH))
    age_raw = re.sub(r"\D", "", rec.get(C_AGE))
    age = age_raw if age_raw else ai.compute_age(birth, apply_date)
    pref = rec.get(C_PREF)
    extra = {prop: rec.get(col) for col, prop in EXTRA_COLUMNS.items() if rec.get(col)}
    return ai.ApplicantRow(
        name=_join(rec.get(C_LAST), rec.get(C_FIRST)),
        kana=_join(rec.get(C_LAST_KANA), rec.get(C_FIRST_KANA)),
        phone=_phone_digits(rec.get(C_PHONE)),
        email=ai.normalize_email(rec.get(C_MAIL)),
        apply_date=apply_date,
        media=MEDIA,
        media_job_id=rec.get(C_HR_JOB_ID),
        raw_lineno=rec.sheet_row,
        gender=ai.normalize_gender(rec.get(C_GENDER)),
        birthdate=birth,
        age=age,
        postal=rec.get(C_POSTAL),
        pref=pref if pref in ai._PREFS else "",
        city=rec.get(C_CITY),
        addr_rest=_join(rec.get(C_TOWN), rec.get(C_STREET)),
        extra=extra,
    )


# ============================================================================
# 求人ページ (reclogi.jp/job-data/{id}) の解析
# ============================================================================
_JOB_ID_RE = re.compile(r"\[(\d+)\]\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)")
_SALARY_TYPE_RE = re.compile(r'class="salary-type"[^>]*>\s*([^<]+?)\s*<', re.S)
_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


_BR = "\x00"   # <br> の印 (直後のソース改行と重ねないため)


class _FieldParser(HTMLParser):
    """Drupal の項目ブロック <div class="field__label">見出し</div><div class="field__item">値</div>
    を (見出し, 値) の並びとして集める。値の中の <br> と div の切れ目は改行にする。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list = []
        self._depth = 0
        self._mode = ""          # "label" / "item" / ""
        self._start = 0
        self._buf: list = []
        self._label: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag == "br" and self._mode:
            self._buf.append(_BR)
        if tag != "div":
            return
        self._depth += 1
        if self._mode:
            self._buf.append("\n")
            return
        cls = (dict(attrs).get("class") or "").split()
        if "field__label" in cls:
            self._mode, self._start, self._buf = "label", self._depth, []
        elif "field__item" in cls and self._label is not None:
            self._mode, self._start, self._buf = "item", self._depth, []

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._mode:
            self._buf.append(_BR)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self._mode and self._depth == self._start:
            # <br /> の直後の改行はソースの折り返しなので1つにまとめる
            text = _tidy(re.sub(_BR + r"\r?\n?", "\n", "".join(self._buf)))
            if self._mode == "label":
                self._label = text
            else:
                self.pairs.append((self._label, text))
                self._label = None
            self._mode = ""
        elif self._mode:
            self._buf.append("\n")
        self._depth -= 1

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


def _tidy(s: str) -> str:
    """行ごとに前後の空白 (全角含む) を落とし、空行の連続を1つにする。"""
    lines = [ln.strip(" \t\r　") for ln in str(s or "").split("\n")]
    out: list = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _job_posting(page: str) -> dict:
    """JSON-LD の JobPosting。無ければ {}。"""
    for m in _LDJSON_RE.finditer(page):
        try:
            data = json.loads(m.group(1), strict=False)
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict) and n.get("@type") == "JobPosting":
                return n
    return {}


def parse_job_page(page: str) -> dict:
    """求人ページ → 求人を作るのに使う値。読めない項目は空文字。

    ids は「[リクロジ求人ID] 1 - 店舗ID - HRハッカー求人ID」の欄から取る。
    この欄が読めなければ hr_job_id が空になり、呼び出し側は求人を作らない。
    """
    out = {k: "" for k in ("reclogi_id", "shop_id", "hr_job_id", "title", "occupation",
                           "description", "employment", "worktime", "salary_type",
                           "holidays", "pref", "city", "street", "postal")}
    m = _JOB_ID_RE.search(page)
    if m:
        out["reclogi_id"], out["shop_id"], out["hr_job_id"] = m.group(1), m.group(3), m.group(4)
    p = _FieldParser()
    p.feed(page)
    fields: dict = {}
    for label, value in p.pairs:
        if label and label not in fields:
            fields[label] = value
    out["occupation"] = fields.get("職種", "")
    out["description"] = fields.get("職務内容", "")
    out["employment"] = fields.get("雇用形態", "")
    out["worktime"] = fields.get("勤務時間", "")
    out["holidays"] = fields.get("休日・休暇", "")
    st = _SALARY_TYPE_RE.search(page)
    out["salary_type"] = _tidy(_html.unescape(st.group(1))) if st else ""
    jp = _job_posting(page)
    out["title"] = str(jp.get("title") or "").strip()
    if not out["title"]:
        h1 = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S)
        out["title"] = _tidy(_html.unescape(re.sub(r"<[^>]+>", "", h1.group(1)))) if h1 else ""
    addr = ((jp.get("jobLocation") or {}).get("address") or {}) if jp else {}
    out["pref"] = str(addr.get("addressRegion") or "").strip()
    out["city"] = str(addr.get("addressLocality") or "").strip()
    out["street"] = str(addr.get("streetAddress") or "").strip()
    out["postal"] = str(addr.get("postalCode") or "").strip()
    return out


def fetch_job_page(url: str) -> Optional[str]:
    """求人ページの HTML。200 以外 (410=掲載終了 など)・取得失敗は None。

    文字コードは応答ヘッダに従う (2026-10-20 実測は UTF-8)。読めなければ cp932 も試す。
    """
    if not re.match(r"^https://reclogi\.jp/job-data/\d+/?$", str(url or "").strip()):
        return None
    import requests
    try:
        r = requests.get(url.strip(), headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT_SEC)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    for enc in [e for e in (r.encoding, "utf-8", "cp932") if e]:
        try:
            return r.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def build_listing_props(parsed: dict, page_url: str,
                        today_iso: str = "", now_iso: str = "") -> dict:
    """ページの値 → 新規 LISTING のプロパティ。HRハッカー取込の作り方に合わせる。

    - hrhacker_import.build_create_props をそのまま使う
      (id_hrhakkaa / id_shop_hrhakkaa / url_hrhakkaa / hs_name / 主要出稿媒体 / 管理区分 等)
    - CSV で見つけたわけではないので、CSV 検出の印 (最終CSV検出日・今回CSV検出フラグ) は付けない
    - 公開状態はページからは分からない (実測: リクロジに載っている10件はすべて
      HubSpot で 公開終了 / HR の原ステータス 非公開)。ステータスとステージは書かず、
      HRハッカー取込が CSV で見つけたときに付ける
    - 求人詳細は AirWork 取込 (_aw_listing_detail_props) と同じ項目に詰める
    """
    today_iso = today_iso or datetime.now().strftime("%Y-%m-%d")
    now_iso = now_iso or datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"media_job_id": parsed.get("hr_job_id", ""), "shop_id": parsed.get("shop_id", ""),
           "job_name": parsed.get("title") or parsed.get("occupation") or ""}
    p = hi.build_create_props(row, today_iso, now_iso, source_filename=page_url)
    if not p:
        return {}
    p.pop(hi.PROP_SAISHUU_CSV_BI, None)
    p.pop(hi.PROP_KONKAI_CSV_FLAG, None)
    p[hi.PROP_TORIKOMI_RIYUU] = "リクロジ応募から作成"
    pref = parsed.get("pref", "")
    pref = pref if pref in hi.PREFECTURES else ""
    city = parsed.get("city", "")
    for val, prop in ((parsed.get("description"), "shigotonaiyou"),
                      (parsed.get("occupation"), "zhizhong"),
                      ((pref + city) if pref else "", "qinwude"),
                      (parsed.get("worktime"), "kinmujikan"),
                      (parsed.get("holidays"), "kyuujitsu"),
                      (pref, hi.PROP_PREFECTURE),
                      (city if pref else "", hi.PROP_CITY)):
        v = str(val or "").strip()
        if v:
            p[prop] = v
    if parsed.get("salary_type") in _KYUUYO_KEITAI_OK:
        p["kyuuyokeitai"] = parsed["salary_type"]
    return p


def create_listing(props: dict) -> Optional[str]:
    """LISTING を1件作り、HRハッカー取込と同じく暗黙知テンプレNoteを付ける。作れなければ None。"""
    ok, ng, errors, idmap = hi.batch_create([{"properties": props}])
    lid = idmap.get(props.get("id_hrhakkaa", ""))
    if not lid:
        plog.detail("reclogi_listing_create_failed", errors=errors[:3])
        return None
    try:
        try:
            from .notes import attach_template_notes
        except ImportError:  # pragma: no cover
            from scripts.job_application_sync.notes import attach_template_notes  # type: ignore
        attach_template_notes([lid])
    except Exception as e:  # noqa: BLE001 — Note は best-effort。求人の作成は成功している
        plog.public(f"[reclogi] 新規求人のテンプレNote付与失敗: {type(e).__name__}")
    return lid


class _KnownListings:
    """直前に引いた/作った求人を search_listing_hr で返す (作った直後は検索の反映が遅れるため)。
    それ以外の呼び出しは元のクライアントへそのまま渡す。"""

    def __init__(self, inner, known: dict) -> None:
        self._inner = inner
        self._known = known

    def search_listing_hr(self, media_job_id: str):
        return self._known.get(media_job_id) or self._inner.search_listing_hr(media_job_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ============================================================================
# シートの入出力
# ============================================================================
def open_tab():
    """タブ「リクロジ応募」の worksheet。シートIDが無い・タブが無いときは (None, 理由)。"""
    import gspread
    sid = os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", "") or aq.QUEUE_SHEET_ID
    if not sid:
        return None, "JAS_APPLICANT_QUEUE_SHEET_ID 未設定"
    gc = aq._sheets_client()
    book = al.sheet_retry(gc.open_by_key, sid)
    try:
        return book.worksheet(TAB), ""
    except gspread.exceptions.WorksheetNotFound:
        return None, f"タブ「{TAB}」が無い"


def write_back(ws, updates: dict) -> int:
    """{台帳の鍵: (HubSpot登録, HubSpot応募者ID)} をシートへ書く。書いたセル行数を返す。

    行番号は書く直前に読み直して messageId で引き直す (読んでから書くまでに
    行が消されても、別の行に書かない)。値が既に入っている行には書かない
    (現場が先に「対象外」等を書いた行を潰さない)。
    """
    if not updates:
        return 0
    idx, recs = records_from_values(al.sheet_retry(ws.get_all_values))
    if any(_norm_header(c) not in idx for c in REQUIRED_COLUMNS):
        return 0
    c_st, c_id = idx[C_STATUS], idx[C_APPT_ID]
    data = []
    for rec in recs:
        v = updates.get(rec.key)
        if not v or rec.get(C_STATUS):
            continue
        status, appt = v
        data.append({"range": f"{aq._col_letter(c_st)}{rec.sheet_row}", "values": [[status]]})
        if appt:
            data.append({"range": f"{aq._col_letter(c_id)}{rec.sheet_row}",
                         "values": [[str(appt)]]})
    # ★送る前に数える。gspread の batch_update は data の range を「'タブ名'!A1」に
    #   書き換える (6.2.1 で確認) ので、送った後に range で数えると0になる
    #   (2026-10-20 本番初回: 実際は17行書けていたのにログは「書き戻し=0行」)。
    n = sum(1 for d in data if d["range"].startswith(aq._col_letter(c_st)))
    if data:
        al.sheet_retry(ws.batch_update, data)
    return n


# ============================================================================
# シート1 (現場の作業シート) のリクロジ行: 顧客=応募者ページURL / 担当CS=コンサル担当の姓
# ============================================================================
S1_ID_HEADER = "messageId"         # 「messageId（自動転記用・編集しないでください）」等。含む列で引く
S1_CUSTOMER_HEADER = "顧客"         # 通常は宛先メール。リクロジ行は support@reclogi.jp が入る
S1_CS_HEADER = "担当CS"
RECLOGI_SENDER = "support@reclogi.jp"
APPT_URL_TMPL = "https://app.hubspot.com/contacts/23708633/record/0-421/{}"
# シート1に行が出てこないまま、受信からこの日数を過ぎたら探すのをやめる
# (GAS の転記はふつう数分。現場が消した行を永遠に探さない)
S1_GIVE_UP_DAYS = int(os.environ.get("JAS_RECLOGI_S1_GIVE_UP_DAYS", "3"))
# partial = 顧客は済んだが担当CSの姓が取れなかった。担当CSだけ次の回にやり直す
S1_DONE, S1_GAVE_UP, S1_PARTIAL = "done", "gave_up", "partial"
S1_KEYS = ("id", "customer", "cs")
_MAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def s1_columns(header: list) -> dict:
    """シート1のヘッダ → {"id"|"customer"|"cs": 列index or None}。位置は見ない。"""
    cols = {"id": None, "customer": None, "cs": None}
    for i, h in enumerate(header):
        k = _norm_header(h)
        if cols["id"] is None and S1_ID_HEADER.lower() in k.lower():
            cols["id"] = i
        elif cols["customer"] is None and k == S1_CUSTOMER_HEADER:
            cols["customer"] = i
        elif cols["cs"] is None and k == S1_CS_HEADER:
            cols["cs"] = i
    return cols


_MSGID_RE = re.compile(r"^[0-9a-f]{16}$")
S1_SAMPLE_ROWS = 3000        # 中身で列を決めるときに見る末尾の行数
S1_SAMPLE_MIN = 3            # 値がこれより少ない列は候補にしない
S1_MIN_WIDTH = 27            # 見出しが短くても AA 列までは候補にする


def msgid_column_by_content(block: list, skip: set = frozenset()) -> tuple:
    """末尾の行の値 → (messageId の列index or None, 候補の列index一覧)。

    値の過半が 16桁の16進 (Gmail の messageId) の列。候補が2列以上なら決めない。
    """
    width = max((len(r) for r in block), default=0)
    cands = []
    for ci in range(width):
        if ci in skip:
            continue
        vals = [str(r[ci]).strip() for r in block if len(r) > ci and str(r[ci]).strip()]
        if len(vals) >= S1_SAMPLE_MIN and \
                sum(1 for v in vals if _MSGID_RE.match(v)) * 2 > len(vals):
            cands.append(ci)
    return (cands[0] if len(cands) == 1 else None), cands


def resolve_s1_columns(ws1) -> dict:
    """シート1の列。messageId は (a) 見出しに messageId を含む列、(b) 無ければ中身で引く。

    ★(b) が要る理由 (2026-10-20 実測): AA1 に見出しが無く、データ行の値
      (16桁の16進) が入っていた。現場の並べ替え等で見出しセルが動いたとみられ、
      GAS は AA1 が空のときしか見出しを書かないので自然には直らない。
      applicant_queue._resolve_s1_columns と同じく、見出しは手がかりに留める。
    """
    header = al.sheet_retry(ws1.row_values, 1)
    cols = s1_columns(header)
    if cols["id"] is not None:
        return cols
    width = max(len(header), S1_MIN_WIDTH)
    # データの最終行: A列 (件名) と顧客列の長い方 (片方の末尾が空でも取りこぼさない)
    anchors = sorted({0} | ({cols["customer"]} if cols["customer"] is not None else set()))
    last = max(len(al.sheet_retry(ws1.col_values, c + 1)) for c in anchors)
    if last < 2:
        return cols
    start = max(2, last - S1_SAMPLE_ROWS + 1)
    block = al.sheet_retry(ws1.get, f"A{start}:{aq._col_letter(width - 1)}{last}")
    skip = {v for k, v in cols.items() if v is not None}
    ci, cands = msgid_column_by_content(block, skip)
    if ci is None and len(cands) > 1:
        plog.public(f"[reclogi] ⚠️ シート1の messageId 列の候補が複数 "
                    f"{[aq._col_letter(c) for c in cands]} → 書かない")
    cols["id"] = ci
    return cols


def customer_writable(cur: str) -> bool:
    """顧客欄に応募者ページURLを書いてよいか: 空か、宛先 (support@reclogi.jp) のままのときだけ。"""
    v = str(cur or "").strip()
    if not v:
        return True
    if "http" in v.lower():
        return False
    return {m.lower() for m in _MAIL_RE.findall(v)} == {RECLOGI_SENDER}


def cs_writable(cur: str) -> bool:
    """担当CS欄は空のときだけ書く (現場が書いた名前を潰さない)。"""
    return not str(cur or "").strip()


def appt_url(appointment_id: str) -> str:
    return APPT_URL_TMPL.format(appointment_id)


def open_sheet1():
    """同じブックのタブ「シート1」。シートIDが無ければ None。"""
    sid = os.environ.get("JAS_APPLICANT_QUEUE_SHEET_ID", "") or aq.QUEUE_SHEET_ID
    if not sid:
        return None
    gc = aq._sheets_client()
    return al.sheet_retry(gc.open_by_key, sid).worksheet(aq.SHEET1_TAB)


def _cell(vr) -> str:
    try:
        return str(vr[0][0] or "").strip()
    except (IndexError, TypeError, KeyError):
        return ""


def consultant_last_name(client, listing_id: str) -> str:
    """求人の今の契約 (latest_live) のコンサル担当の姓。取れなければ空。"""
    get_c = getattr(client, "get_listing_consultant", None)
    get_n = getattr(client, "get_owner_last_name", None)
    if not (listing_id and get_c and get_n):
        return ""
    owner = get_c(listing_id, MEDIA, "")
    return (get_n(owner) or "").strip() if owner else ""


def _too_old(received_iso: str, today: str, days: int) -> bool:
    try:
        d = datetime.strptime(received_iso, "%Y-%m-%d").date()
        t = datetime.strptime(today, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False             # 受信日が読めないものは諦めない
    return (t - d).days > days


def sync_sheet1(ws1, items: list, client, ledger=None, today: str = "") -> dict:
    """シート1のリクロジ行の 顧客/担当CS を書く。

    items: [{"key", "mid", "appt", "listing", "received"}]
    - 行は messageId 列だけを読んで引く (シート1は約7万行あるので全体は読まない)
    - 書く直前に messageId・顧客・担当CS を読み直し、同じ ID のときだけ書く
      (並べ替え・削除で別の行に書かない)
    - 行が見つからない: 次の回に再試行。受信から S1_GIVE_UP_DAYS 日を過ぎたら諦める
    台帳の項目 "s1" に done / gave_up を残す (ledger が None なら残さない)。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    out = {"s1_targets": len(items), "s1_rows": 0, "s1_customer": 0, "s1_cs": 0,
           "s1_waiting": 0, "s1_gave_up": 0, "s1_partial": 0}
    if not items:
        return out

    def mark(key: str, state: str) -> None:
        if ledger is None:
            return
        e = ledger.data.setdefault(key, {"attempts": 0, "status": "DONE"})
        e["s1"] = state
        e["updated"] = datetime.now().isoformat()

    cols = resolve_s1_columns(ws1)
    if cols["id"] is None or (cols["customer"] is None and cols["cs"] is None):
        found_cols = {k: v is not None for k, v in cols.items()}
        plog.public(f"[reclogi] ⚠️ シート1の列が見つからない {found_cols} "
                    f"→ 顧客/担当CS は書かない (次の回に再試行)")
        out["s1_waiting"] = len(items)
        return out
    for k, name in (("customer", S1_CUSTOMER_HEADER), ("cs", S1_CS_HEADER)):
        if cols[k] is None:
            plog.public(f"[reclogi] ⚠️ シート1に「{name}」列が見つからない → その列は書かない")
    row_of: dict = {}
    for n, v in enumerate(al.sheet_retry(ws1.col_values, cols["id"] + 1), start=1):
        v = str(v or "").strip()
        if n > 1 and v:
            row_of[v] = n            # 同じIDが2行あれば下 (後から転記された方)
    found = []
    for it in items:
        r = row_of.get(it["mid"])
        if r:
            found.append((it, r))
        elif _too_old(it.get("received", ""), today, S1_GIVE_UP_DAYS):
            out["s1_gave_up"] += 1
            mark(it["key"], S1_GAVE_UP)
            plog.detail("reclogi_s1_gave_up", message_id=it["mid"])
        else:
            out["s1_waiting"] += 1   # GAS の転記がまだ。次の回に再試行
    if not found:
        return out
    keys = [k for k in S1_KEYS if cols[k] is not None]
    L = {k: aq._col_letter(cols[k]) for k in keys}
    cur = al.sheet_retry(ws1.batch_get, [f"{L[k]}{r}" for _it, r in found for k in keys])
    data, states = [], {}
    for i, (it, r) in enumerate(found):
        got = dict(zip(keys, (_cell(v) for v in cur[i * len(keys):(i + 1) * len(keys)])))
        if got.get("id") != it["mid"]:
            out["s1_waiting"] += 1   # 読んでから並べ替えられた。次の回に引き直す
            continue
        if "customer" in L and customer_writable(got.get("customer", "")):
            data.append({"range": f"{L['customer']}{r}", "values": [[appt_url(it["appt"])]]})
            out["s1_customer"] += 1
        if "cs" in L and cs_writable(got.get("cs", "")):
            listing = it.get("listing") or ""
            if not listing and hasattr(client, "get_appointment_listing"):
                listing = client.get_appointment_listing(it["appt"]) or ""
            name = consultant_last_name(client, listing)
            if name:
                data.append({"range": f"{L['cs']}{r}", "values": [[name]]})
                out["s1_cs"] += 1
            elif _too_old(it.get("received", ""), today, S1_GIVE_UP_DAYS):
                states[it["key"]] = S1_GAVE_UP
                out["s1_gave_up"] += 1
                continue
            else:
                # 姓が取れない (取引がまだ求人に付いていない・一時エラー)。担当CSだけ次の回に
                states[it["key"]] = S1_PARTIAL
                out["s1_partial"] += 1
                continue
        states[it["key"]] = S1_DONE
    if data:
        al.sheet_retry(ws1.batch_update, data)
    for key, state in states.items():
        mark(key, state)
    out["s1_rows"] = sum(1 for v in states.values() if v != S1_GAVE_UP)
    return out


def s1_items(recs: list, ledger, fresh: dict) -> list:
    """シート1に書く対象。応募者IDがあり (受入済/求人未特定)、台帳でシート1が未済の行。

    fresh: この回に作った {台帳の鍵: (応募者ID, 求人ID)}。タブはこの回の書き戻し前に
    読んだものなので、この回の分は fresh から取る。
    """
    items = []
    for rec in recs:
        mid = rec.get(C_MESSAGE_ID)
        if not (rec.key and mid):
            continue
        e = (ledger.data.get(rec.key) or {}) if ledger is not None else {}
        if e.get("s1") in (S1_DONE, S1_GAVE_UP):
            continue
        is_fresh = rec.key in fresh
        appt, listing = fresh.get(rec.key, ("", ""))
        status = ST_DONE if is_fresh else (rec.get(C_STATUS) or e.get("sheet_status", ""))
        appt = appt or rec.get(C_APPT_ID) or e.get("appointment_id", "")
        if not appt or status not in (ST_DONE, ST_UNLINKED):
            continue
        items.append({"key": rec.key, "mid": mid, "appt": str(appt),
                      "listing": listing or e.get("listing_id", ""),
                      # この回に作ったものは諦めの判定に掛けない
                      "received": "" if is_fresh else
                      (_iso_date(rec.get(C_RECEIVED)) or _iso_date(rec.get(C_APPLIED_AT)))})
    return items


# ============================================================================
# 本体
# ============================================================================
def _status_of(res) -> Optional[tuple]:
    """ProcessResult → (HubSpot登録, 応募者ID)。error は None (再試行に回す)。"""
    if res.status == "linked" or res.status == "skip_duplicate":
        return ST_DONE, res.appointment_id or ""
    if res.status == "unlinked":
        return ST_UNLINKED, res.appointment_id or ""
    return None


def run(dry_run: bool = True, ledger=None, max_attempts: int = 3,
        notify: Optional[Callable[[str], object]] = None,
        ws=None, client=None,
        fetch_page: Callable[[str], Optional[str]] = fetch_job_page,
        make_listing: Callable[[dict], Optional[str]] = create_listing,
        ws1=None) -> dict:
    """タブ「リクロジ応募」の未処理行を取り込む。summary を返す。

    ledger: applicant_sync の台帳。鍵は "RL-<messageId>"。
      - 書き戻しに失敗しても二重に取り込まない (DONE は飛ばし、書き戻しだけやり直す)
      - HubSpot のエラーは max_attempts 回まで空欄のまま再試行し、超えたら「エラー: …」を書く
    応募者ができた行は、同じ回にシート1の 顧客/担当CS も書く (sync_sheet1)。
    ws / client / fetch_page / make_listing / ws1 はテスト用の差し替え口
    (ws を渡して ws1 を渡さないときはシート1を触らない)。
    """
    use_s1 = ws is None or ws1 is not None
    s = {"rows": 0, "targets": 0, "linked": 0, "unlinked": 0, "dup": 0, "error": 0,
         "gave_up": 0, "listing_found": 0, "listing_created": 0, "listing_create_failed": 0,
         "page_unavailable": 0, "rewrite": 0, "written": 0, "dry_run": dry_run}
    if ws is None:
        ws, why = open_tab()
        if ws is None:
            plog.public(f"[reclogi] ⚠️ {why} → リクロジ応募の取り込みは行わない")
            s["skipped"] = why
            return s
    idx, recs = records_from_values(al.sheet_retry(ws.get_all_values))
    missing = [c for c in REQUIRED_COLUMNS if _norm_header(c) not in idx]
    if missing:
        plog.public(f"[reclogi] ⚠️ タブ「{TAB}」に必要な列が無い: {missing} → 取り込みは行わない")
        s["skipped"] = f"列なし {missing}"
        return s
    s["rows"] = len(recs)
    updates: dict = {}
    targets = []
    for rec in recs:
        if not is_target(rec) or not rec.key:
            continue
        e = (ledger.data.get(rec.key) or {}) if ledger is not None else {}
        if e.get("status") == "DONE":
            # 取り込み済みなのにシートが空 = 前回の書き戻しが失敗した。書き戻しだけやり直す
            if e.get("sheet_status"):
                updates[rec.key] = (e["sheet_status"], e.get("appointment_id", ""))
                s["rewrite"] += 1
            continue
        targets.append(rec)
    s["targets"] = len(targets)
    s1_pending = s1_items(recs, ledger, {}) if use_s1 else []
    if not targets and not updates and not s1_pending:
        plog.public(f"[reclogi] 未処理なし (行={s['rows']})")
        return s

    if client is None:
        token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
        client = ai.RealHubSpotClient(token) if (token or not dry_run) else None
    rows = [to_applicant_row(r) for r in targets]

    # 1. 求人: HRハッカー求人IDで引く → 無ければページから作る (1回の実行で同じ求人は1回だけ)
    known: dict = {}
    by_job: dict = {}
    for rec, row in zip(targets, rows):
        if row.media_job_id:
            by_job.setdefault(row.media_job_id, rec)
    for jid, rec in by_job.items():
        lid = client.search_listing_hr(jid) if client is not None else None
        if lid:
            known[jid] = lid
            s["listing_found"] += 1
            continue
        page = fetch_page(rec.get(C_JOB_URL))
        parsed = parse_job_page(page) if page else {}
        if not parsed.get("hr_job_id"):
            s["page_unavailable"] += 1       # 掲載終了 (410) など。応募者は求人未特定で作る
            continue
        sheet_shop = rec.get(C_SHOP_ID)
        if parsed["hr_job_id"] != jid or (sheet_shop and parsed["shop_id"] != sheet_shop):
            # メールとページで求人が食い違う。別の求人を作らない
            s["listing_create_failed"] += 1
            plog.detail("reclogi_page_mismatch", sheet_row=rec.sheet_row, hr_job_id=jid,
                        page_hr_job_id=parsed["hr_job_id"], page_shop_id=parsed["shop_id"])
            continue
        if dry_run:
            s["listing_created"] += 1         # 作る予定の件数
            continue
        lid = make_listing(build_listing_props(parsed, rec.get(C_JOB_URL)))
        if lid:
            known[jid] = lid
            s["listing_created"] += 1
        else:
            s["listing_create_failed"] += 1

    if dry_run:
        plog.public(f"[reclogi] dry-run: 対象={s['targets']} 求人あり={s['listing_found']} "
                    f"求人を作る予定={s['listing_created']} ページなし={s['page_unavailable']} "
                    f"シート1待ち={len(s1_pending)} (HubSpot・シートには書かない)")
        return s

    # 2. 応募者: applicant_import の流れをそのまま通す
    results = ai.run_import(rows, _KnownListings(client, known)) if rows else []
    failed_rows = []
    fresh: dict = {}
    for rec, res in zip(targets, results):
        st = _status_of(res)
        if st is not None:
            s["linked" if res.status == "linked" else
              "dup" if res.status == "skip_duplicate" else "unlinked"] += 1
            if ledger is not None:
                ledger.mark(rec.key, "DONE")
                ledger.data[rec.key].update(sheet_status=st[0], appointment_id=st[1],
                                            listing_id=res.listing_id or "")
            updates[rec.key] = st
            if st[1]:
                fresh[rec.key] = (st[1], res.listing_id or "")
            continue
        s["error"] += 1
        msg = (res.message or "不明なエラー")[:200]
        atts = ledger.bump(rec.key, msg) if ledger is not None else max_attempts
        if atts >= max_attempts:
            s["gave_up"] += 1
            updates[rec.key] = (f"{ST_ERROR_PREFIX}{msg}", "")
            failed_rows.append(rec.sheet_row)
            plog.detail("reclogi_import_error", sheet_row=rec.sheet_row, error=msg)
    if failed_rows and notify:
        notify(f"⚠️ リクロジ応募の取り込みに {max_attempts}回失敗: {len(failed_rows)}件 "
               f"(タブ「{TAB}」の行 {', '.join(map(str, failed_rows[:10]))})\n"
               f"→ 「HubSpot登録」に理由を書きました。直したらその欄を空にすると取り込み直します")

    # 3. 書き戻し (失敗しても台帳があるので二重には取り込まない。次の回にやり直す)
    try:
        s["written"] = write_back(ws, updates)
    except Exception as e:  # noqa: BLE001
        plog.public(f"[reclogi] ⚠️ シートへの書き戻し失敗 (次の回にやり直す): "
                    f"{type(e).__name__}: {str(e)[:120]}")
    # 4. シート1の 顧客/担当CS (失敗しても次の回にやり直す。本線は止めない)
    if use_s1:
        try:
            items = s1_items(recs, ledger, fresh)
            w1 = (ws1 if ws1 is not None else open_sheet1()) if items else None
            if w1 is not None:
                s.update(sync_sheet1(w1, items, client, ledger))
        except Exception as e:  # noqa: BLE001
            plog.public(f"[reclogi] ⚠️ シート1への書き込み失敗 (次の回にやり直す): "
                        f"{type(e).__name__}: {str(e)[:120]}")
        if s.get("s1_targets"):
            plog.public(f"[reclogi] シート1: 対象={s['s1_targets']} 書いた行={s['s1_rows']} "
                        f"(顧客={s['s1_customer']} 担当CS={s['s1_cs']}) "
                        f"担当CS待ち={s.get('s1_partial', 0)} 転記待ち={s['s1_waiting']} 諦め={s['s1_gave_up']}")
    plog.public(f"[reclogi] 対象={s['targets']} 紐付け={s['linked']} 求人未特定={s['unlinked']} "
                f"重複={s['dup']} エラー={s['error']} (諦め={s['gave_up']}) / "
                f"求人あり={s['listing_found']} 作成={s['listing_created']} "
                f"作成失敗={s['listing_create_failed']} ページなし={s['page_unavailable']} / "
                f"書き戻し={s['written']}行 (うち前回分={s['rewrite']})")
    return s
