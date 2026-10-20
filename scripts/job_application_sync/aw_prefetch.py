"""AW 1社分の HubSpot 問い合わせを、行ごとではなく社ごとにまとめて行うクライアント。

なぜ (2026-10-13 専用PCの実測):
  応募 CSV は過去の応募を含めて全行 (1社 数十〜数百行) が毎回来る。
  これまでは 1 行ごとに 求人の検索 1〜2 回・重複の検索 1 回・求人の担当者の GET・
  応募の現在値の GET を順番に送っており、1 行 3.5〜4 秒 (Actions は約 0.5 秒) かかっていた。
  例: 122 行の社で 30 分。

やり方:
  判定ロジック (applicant_import._process_applicant) は 1 行も変えない。
  そこから呼ばれる問い合わせのうち、社ごとにまとめて読めるものだけを、
  最初にまとめて読んだ結果で答える。まとめて読めなかったもの (求人IDの無い行、
  読み込みの失敗、1万件を超えた検索) は、従来どおり 1 行ずつ HubSpot に問い合わせる。

  - 求人 (0-420): id_airwork IN [その社の求人ID] を 100 件ずつ検索
      → search_listing_aw / search_listing_aw_by_id / get_listing_owner /
        get_listing_ichijitaiou を手元で答える
  - 既存の応募 (0-421): 媒体 EQ かつ 求人ID IN [その社の求人ID] を 100 件ずつ検索
      → find_existing_appointment / get_appointment_props (重複時の補完) を手元で答える
  - get_oubosaki_props と求人のメモ本文は、同じ求人の 2 行目以降は覚えた値を使う
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

try:
    from scripts.job_application_sync import applicant_import as ai
except ImportError:  # pragma: no cover
    import applicant_import as ai  # type: ignore

DM = ai.DM

# 求人の検索で一緒に読む項目 (get_listing_owner / get_listing_ichijitaiou が読むもの)
_LISTING_PROPS = ["id_airwork", "airwork_account_login_id", "hubspot_owner_id",
                  "ichijitaiounoumu_deforuto"]
# 既存の応募の検索で読む項目: 重複の照合キー + 重複時の補完で読む項目
_APPT_KEY_PROPS = ["oubobaitaimei", "oubokyuujinmemo", "yingmuri",
                   "meeruadoresu", "denwabangou"]
_APPT_FILL_PROPS = ["seibetsu", "seinengappi", "nenrei", "yuubinbangou",
                    "todoufuken", "shikuchouson", "shikuchousonikajuusho",
                    "ouboshashimei_kana", "yingmujinglu", "hubspot_owner_id"]
_CHUNK = 100          # IN に渡す値の数
_PAGE = 200           # Search の 1 ページ
_SEARCH_MAX = 10000   # Search が返せる件数の上限 (超えたら手元では答えない)


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _date_str(v) -> str:
    """HubSpot の日付型の値 (YYYY-MM-DD / ISO / epoch ミリ秒) を YYYY-MM-DD に。"""
    s = str(v or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return s[:10]


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


class PrefetchingHubSpotClient(ai.RealHubSpotClient):
    """AW 1社分を先にまとめて読み、行ごとの問い合わせを手元で答えるクライアント。"""

    def __init__(self, token: str):
        super().__init__(token)
        self.stats = {"search_calls": 0, "listing_jobs": 0, "appt_jobs": 0,
                      "appts": 0, "fallback": 0}
        # 求人: id_airwork → [(listing_id, airwork_account_login_id)]
        self._listings: dict = {}
        self._listing_props: dict = {}     # listing_id → props
        self._listing_covered: set = set()  # 手元で答えてよい求人ID
        # 既存の応募: 求人ID → [{"id":..., props}]
        self._appts: dict = {}
        self._appt_props: dict = {}        # appointment_id → props
        self._appt_covered: set = set()
        self._appt_media = ""
        self._created: set = set()         # この実行で作った応募
        self._oubosaki: dict = {}
        self._note_body: dict = {}

    # ---- まとめて読む ------------------------------------------------------
    def _search_all(self, object_type: str, filters: list, props: list) -> Optional[list]:
        """Search を全ページ読む。失敗・上限超えは None (呼び出し側は手元で答えない)。"""
        out: list = []
        after = None
        while True:
            body = {"filterGroups": [{"filters": filters}], "properties": props,
                    "limit": _PAGE}
            if after:
                body["after"] = after
            self.stats["search_calls"] += 1
            try:
                r = self._post(f"/crm/v3/objects/{object_type}/search", body)
            except Exception:  # noqa: BLE001 — 通信の失敗は従来の 1 行ずつに戻す
                return None
            if r.status_code >= 300:
                return None
            j = r.json()
            if int(j.get("total") or 0) > _SEARCH_MAX:
                return None
            out.extend(j.get("results", []))
            after = (j.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return out

    def prefetch_listings(self, job_ids) -> bool:
        ids = sorted({str(j).strip() for j in job_ids if str(j or "").strip()})
        ok = True
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            res = self._search_all("0-420", [
                {"propertyName": "id_airwork", "operator": "IN", "values": chunk}],
                _LISTING_PROPS)
            if res is None:
                ok = False
                continue
            for o in res:
                p = o.get("properties") or {}
                jid = str(p.get("id_airwork") or "").strip()
                if not jid:
                    continue
                self._listings.setdefault(jid, []).append(
                    (str(o["id"]), p.get("airwork_account_login_id")))
                self._listing_props[str(o["id"])] = p
            self._listing_covered.update(chunk)
        self.stats["listing_jobs"] = len(self._listing_covered)
        return ok

    def add_listings(self, jid_to_listing: dict, login_id: str) -> None:
        """この実行で作った求人を手元に足す (Search の反映を待たずに紐付けるため)。"""
        for jid, lid in (jid_to_listing or {}).items():
            jid, lid = str(jid).strip(), str(lid)
            known = self._listings.setdefault(jid, [])
            if all(x[0] != lid for x in known):
                known.append((lid, login_id))

    def missing_listings(self, job_ids) -> Optional[list]:
        """HubSpot に求人が無い求人ID。手元で答えられないものがあれば None。"""
        ids = [str(j).strip() for j in job_ids if str(j or "").strip()]
        if any(j not in self._listing_covered for j in ids):
            return None
        return [j for j in dict.fromkeys(ids) if not self._listings.get(j)]

    def prefetch_appointments(self, media: str, job_ids) -> bool:
        ids = sorted({str(j).strip() for j in job_ids if str(j or "").strip()})
        self._appt_media = media
        ok = True
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            res = self._search_all("0-421", [
                {"propertyName": "oubobaitaimei", "operator": "EQ", "value": media},
                {"propertyName": "oubokyuujinmemo", "operator": "IN", "values": chunk}],
                _APPT_KEY_PROPS + _APPT_FILL_PROPS)
            if res is None:
                ok = False
                continue
            for o in res:
                p = o.get("properties") or {}
                self._index_appt(str(o["id"]), p)
            self._appt_covered.update(chunk)
        self.stats["appt_jobs"] = len(self._appt_covered)
        self.stats["appts"] = len(self._appt_props)
        return ok

    def _index_appt(self, appt_id: str, p: dict) -> None:
        jid = str(p.get("oubokyuujinmemo") or "").strip()
        self._appt_props[appt_id] = p
        self._appts.setdefault(jid, []).append(appt_id)

    # ---- 求人 ---------------------------------------------------------------
    def search_listing_aw(self, media_job_id: str, airwork_login_id: str) -> Optional[str]:
        jid = str(media_job_id or "").strip()
        if jid in self._listing_covered:
            for lid, login in self._listings.get(jid, []):
                if login == airwork_login_id:
                    return lid
            return None
        self.stats["fallback"] += 1
        return super().search_listing_aw(media_job_id, airwork_login_id)

    def search_listing_aw_by_id(self, media_job_id: str) -> list[str]:
        jid = str(media_job_id or "").strip()
        if jid in self._listing_covered:
            return [lid for lid, _ in self._listings.get(jid, [])][:10]
        self.stats["fallback"] += 1
        return super().search_listing_aw_by_id(media_job_id)

    def get_listing_owner(self, listing_id: str) -> Optional[str]:
        p = self._listing_props.get(str(listing_id))
        if p is not None:
            return p.get("hubspot_owner_id")
        v = super().get_listing_owner(listing_id)
        if v is not None:   # 失敗 (None) は覚えない
            self._listing_props.setdefault(str(listing_id), {})["hubspot_owner_id"] = v
        return v

    def get_listing_ichijitaiou(self, listing_id: str) -> Optional[str]:
        p = self._listing_props.get(str(listing_id))
        if p is not None and "ichijitaiounoumu_deforuto" in p:
            return p.get("ichijitaiounoumu_deforuto")
        return super().get_listing_ichijitaiou(listing_id)

    def get_oubosaki_props(self, listing_id: str, media: str,
                           login_id: str, media_job_id: str) -> dict:
        key = (str(listing_id), media, login_id, str(media_job_id))
        if key not in self._oubosaki:
            self._oubosaki[key] = super().get_oubosaki_props(
                listing_id, media, login_id, media_job_id)
        return dict(self._oubosaki[key])

    # ---- 既存の応募 -----------------------------------------------------------
    def find_existing_appointment(self, media: str, media_job_id: str, phone: str,
                                  email: str, apply_date: str = "") -> Optional[str]:
        jid = str(media_job_id or "").strip()
        if not (jid and media == self._appt_media and jid in self._appt_covered):
            self.stats["fallback"] += 1
            return super().find_existing_appointment(media, media_job_id, phone,
                                                     email, apply_date=apply_date)
        # 従来の検索と同じ条件: 媒体 = / 求人ID = / 応募日 = (正しい日付のときだけ)
        #   / (メール = または 電話 =)。メールは小文字にして比べる (HubSpot の文字の = は
        #   大文字小文字を区別しない)。電話は数字だけで比べる (格納値は +81 形式)。
        em = ai.normalize_email(email)
        ph = _digits(ai.format_jp_phone(phone)) if phone else ""
        if not em and not ph:
            return None
        use_date = bool(apply_date) and _valid_date(apply_date)
        for appt_id in self._appts.get(jid, []):
            p = self._appt_props.get(appt_id) or {}
            if p.get("oubobaitaimei") and p.get("oubobaitaimei") != media:
                continue
            if use_date and _date_str(p.get("yingmuri")) != apply_date:
                continue
            if em and ai.normalize_email(p.get("meeruadoresu")) == em:
                return appt_id
            if ph and _digits(p.get("denwabangou")) == ph:
                return appt_id
        return None

    def get_appointment_props(self, appointment_id: str, properties: list):
        # まとめて読んだときに頼んだ項目なら、応募に値が無い (空) ことも分かっている
        p = self._appt_props.get(str(appointment_id))
        known = set(_APPT_KEY_PROPS + _APPT_FILL_PROPS)
        if p is not None and all(k in known or k in p for k in properties):
            return {k: p.get(k) for k in properties}
        return super().get_appointment_props(appointment_id, properties)

    def update_appointment(self, appointment_id: str, properties: dict) -> None:
        super().update_appointment(appointment_id, properties)
        p = self._appt_props.get(str(appointment_id))
        if p is not None:
            p.update(properties)

    def create_appointment(self, properties: dict) -> str:
        appt_id = super().create_appointment(properties)
        self._created.add(str(appt_id))
        jid = str(properties.get("oubokyuujinmemo") or "").strip()
        if jid:
            self._index_appt(str(appt_id), {k: properties.get(k) for k in
                                            _APPT_KEY_PROPS + _APPT_FILL_PROPS})
        return appt_id

    # ---- 求人のメモの複製 -------------------------------------------------------
    def copy_listing_note(self, listing_id: str, appointment_id: str) -> Optional[str]:
        """従来と同じ複製。違いは 2 つだけ:
        - 求人のメモ本文は同じ求人の 2 行目以降は覚えた値を使う
        - この実行で作ったばかりの応募は「複製済みか」の確認を省く
          (作った直後で、複製の印を持つメモが付いていることはない)
        """
        if str(appointment_id) not in self._created:
            return super().copy_listing_note(listing_id, appointment_id)
        try:
            try:
                from scripts.job_application_sync import notes as N
            except ImportError:  # pragma: no cover
                import notes as N  # type: ignore
            token = self.headers["Authorization"].split(" ", 1)[1]
            lid = str(listing_id)
            if lid not in self._note_body:
                self._note_body[lid] = N.get_listing_template_note_body(lid, token=token)
            body = self._note_body[lid]
            if not body:
                return None
            header = (f"<p><small>{N.COPIED_NOTE_MARKER} "
                      f"listing={lid}】</small></p>")
            nid = N.create_note(header + body, token=token)
            if not nid:
                return None
            if not N.associate_note(nid, N.APPOINTMENT, appointment_id, token=token):
                if not N.delete_note(nid, token=token):
                    print(f"[warn] ②孤児Note削除失敗 note={nid} appt={appointment_id} "
                          f"(要手動掃除)", flush=True)
                return None
            return nid
        except Exception as e:  # noqa: BLE001
            print(f"[warn] ②Note複製 失敗 listing={listing_id} "
                  f"appt={appointment_id}: {e}", flush=True)
            return None
