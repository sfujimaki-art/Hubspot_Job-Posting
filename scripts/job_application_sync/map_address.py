"""地図用の住所 (2026-11-26).

HR_HR の CRM の地図タブが使う住所を、取り込みのときに入れる。
- 求人票 (0-420) の kinmuchi_juusho: 勤務地の住所 (番地まで。郵便番号・建物名は入れない)
- 応募 (0-421) の oubosaki_kinmuchi_juusho: 応募先の勤務地の住所

hrhr_ で始まる計算式の項目は HubSpot が作るので、ここでは書かない。
決まり (extract / job_address / pick) は依頼文 (2026-11-26) のとおり。
"""
from __future__ import annotations

import re
from typing import Optional

PROP_JOB_ADDRESS = "kinmuchi_juusho"                 # 求人票: 勤務地の住所（番地まで）
PROP_APPLICANT_JOB_ADDRESS = "oubosaki_kinmuchi_juusho"  # 応募: 応募先の勤務地の住所（地図用）
LISTING_READ_PROPS = ("kinmuchi_juusho", "qinwude", "todoufuken", "shikuchouson")

LINE = re.compile(r"(?m)^勤務地[：:][ \t　]*(\S.*?)[ \t　]*$")
POSTAL = re.compile(r"^〒?\s*[0-9０-９]{3}[-－ー‐][0-9０-９]{4}\s*")
DIGIT = re.compile(r"[0-9０-９]")


def extract(body: Optional[str]) -> Optional[str]:
    """AirWork の本文の「勤務地：」の行から住所を取り出す。取れなければ None."""
    m = LINE.search(body or "")
    if not m:
        return None
    value = POSTAL.sub("", m.group(1).strip()).strip()   # 先頭の郵便番号を外す
    if not value or re.match(r"^(アクセス|勤務時間|給与)[：:]", value):
        return None                                      # 行が空（次の行を拾った）
    kept = []
    for token in value.split():                          # 後ろの建物名は、番地を含む区切りまでで切る
        kept.append(token)
        if DIGIT.search(token):
            break
    return "".join(kept) or None


def store_address(pref: str, city: str, town: str, banchi: str) -> Optional[str]:
    """HRハッカーの店舗一覧の列から住所 (番地まで、建物名なし) を作る.

    町名も番地も無ければ None (都道府県＋市区町村は既存の項目で足りる)。
    店舗一覧の CSV は cp932 なので、cp932 に無いハイフン (₋ など) が「?」に化けて届く。
    数字にはさまれた「?」はハイフンに戻す。
    """
    parts = [re.sub(r"\s+", "", x or "") for x in (pref, city, town, banchi)]
    if not (parts[2] or parts[3]):
        return None
    s = "".join(parts)
    s = re.sub(r"(?<=[0-9０-９])[?？](?=[0-9０-９])", "-", s)
    return s or None


def job_address(listing: dict) -> str:
    """求人票の地図用の住所 (hrhr_job_address と同じ決まり)."""
    pref = listing.get("todoufuken") or ""
    if pref == "unset":
        pref = ""
    return ((listing.get("kinmuchi_juusho") or "").strip()
            or (listing.get("qinwude") or "").strip()
            or (pref + (listing.get("shikuchouson") or "")).strip())


def pick(listing_address: Optional[str], applicant_kinmuchi: Optional[str]) -> Optional[str]:
    """応募に入れる応募先の住所。求人票の住所と応募の「応募先勤務地」のうち長いほう."""
    a = (listing_address or "").strip()
    b = (applicant_kinmuchi or "").strip()
    if not a and not b:
        return None
    return a if len(a) >= len(b) else b


def changed(new: Optional[str], current: Optional[str]) -> bool:
    """新しい値があり、今の値と違うときだけ True (値が同じなら書かない)."""
    return bool(new) and (new or "").strip() != (current or "").strip()
