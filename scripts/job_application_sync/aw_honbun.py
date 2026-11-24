"""AirWork の XLSX の 1 行から「求人票の本文（AirWork）」を作る (2026-11-24).

依頼書: recruit-media-pipeline docs/requests/2026-11-23_airwork-honbun.md §3-2。
HubSpot 求人票 (0-420) の aw_kyuujinhyou_honbun に入れる全文。
HR ハッカーの hrh_kyuujinhyou_honbun と同じ「列名：値」の形で、列名と順番は決まっている。
空の項目も「列名：」の行は残す。値が複数行なら「列名：」の次の行から値。

公開ページ (__NEXT_DATA__ の recruitData) と同じ書き方に寄せる:
  給与         … 「月給 22万710円 〜 26万9700円」
  勤務時間     … 「実働時間：1日あたり8時間」「平均勤務日数：1ヶ月あたり20日 〜 21日」
  休日休暇     … 補足が空なら特徴タグを「、」でつなぐ

問い合わせ先 (contact_*) と会社情報は使わない。
XLSX の列は見出し「日本語名(英語名)」の英語名で引く (日本語名の表記揺れに強くするため)。
"""
from __future__ import annotations

import re
from typing import Optional

PROP_AW_HONBUN = "aw_kyuujinhyou_honbun"

# 本文の項目 (順番どおり)
HONBUN_ITEMS = [
    "キャッチコピー", "給与", "給与の詳細", "仕事内容", "職種", "求める人材",
    "勤務時間", "休日休暇", "社会保険・福利厚生", "試用・研修期間", "勤務地", "アクセス",
]

# 本文を作るのに使う XLSX の列 (英語名)。返信に列名を書くときもこれを使う。
USED_COLUMNS = [
    "subtitle",
    "salary_form_jp", "salary_display_method_jp", "minimum_salary", "maximum_salary",
    "fixed_overtime_type_jp", "fixed_overtime_hours_display_method_jp",
    "minimum_fixed_overtime_hour", "minimum_fixed_overtime_minute",
    "maximum_fixed_overtime_hour", "maximum_fixed_overtime_minute",
    "fixed_overtime_display_method_jp", "minimum_fixed_overtime_pay", "maximum_fixed_overtime_pay",
    "is_pay_overtime_jp",
    "commuting_allowance_type_jp", "commuting_allowance_display_method_jp",
    "lowest_commuting_allowance", "limit_commuting_allowance",
    "others_allowance_type_jp", "others_allowance_display_method_jp",
    "lowest_others_allowance", "limit_others_allowance",
    "salary_allowance_id_name", "salary_supplement", "salary_example",
    "description",
    "occupation_id_jp1",
    "personal", "skill_id_name", "description_id_name", "license_id_name",
    "working_style_jp", "working_style_others_jp",
    "working_hours_display_method_jp", "working_hours_input_type_jp",
    "total_working_hours_time", "total_working_hours_unit_jp",
    "total_working_hours_hour", "total_working_hours_minute",
    "minimum_total_working_hours_hour", "minimum_total_working_hours_minute",
    "maximum_total_working_hours_hour", "maximum_total_working_hours_minute",
    "working_hours_hour", "working_hours_minute",
    "minimum_working_hours_hour", "minimum_working_hours_minute",
    "maximum_working_hours_hour", "maximum_working_hours_minute",
    "average_working_days_display_method_jp", "lowest_working_days", "limit_working_days",
    "working_hours_id_name", "working_time_supplement",
    "holiday", "holiday_id_name",
    "social_insurance_health_jp", "social_insurance_wp_jp", "social_insurance_ei_jp",
    "social_insurance_wc_jp", "welfare_id_name", "welfare",
    "is_probationary_period_jp", "probationary_time", "probationary_salary_jp",
    "probationary_salary_form_jp", "probationary_salary_display_method_jp",
    "probationary_minimum_salary", "probationary_maximum_salary",
    "probationary_period_supplement",
    "working_location_prefecture", "working_location_city_area",
    "working_location_building_name_omission_flg", "working_location_building_name",
    "working_location_access",
]

_EN_RE = re.compile(r"[\(（]([A-Za-z0-9_]+)[\)）]\s*$")


def header_keys(headers: list[str]) -> list[str]:
    """見出し「日本語名(英語名)」から英語名を取り出す。取れない見出しは元の文字列。"""
    out = []
    for h in headers:
        m = _EN_RE.search(h or "")
        out.append(m.group(1) if m else (h or ""))
    return out


def _s(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace("\r\n", "\n").replace("\r", "\n").strip()


def _int(v: object) -> Optional[int]:
    s = _s(v).replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def yen(n: Optional[int]) -> str:
    """220710 → 「22万710円」、250000 → 「25万円」、1100 → 「1100円」。"""
    if n is None:
        return ""
    if n >= 10000:
        man, rest = divmod(n, 10000)
        return f"{man}万{rest}円" if rest else f"{man}万円"
    return f"{n}円"


def _range(method: str, lo: str, hi: str) -> str:
    """表示方法 (範囲を指定する / 下限を指定する / 固定指定する) で「A 〜 B」「A 〜」「A」。"""
    if not lo and not hi:
        return ""
    if "範囲" in method and lo and hi:
        return f"{lo} 〜 {hi}"
    if "下限" in method and lo:
        return f"{lo} 〜"
    return lo or hi


def _hm(h: object, m: object) -> str:
    hh, mm = _int(h), _int(m)
    if hh is None and not mm:
        return ""
    t = f"{hh or 0}時間"
    return t + (f"{mm}分" if mm else "")


def _tags(v: object) -> str:
    return "、".join(t.strip() for t in _s(v).split(",") if t.strip())


def _salary(r: dict, prefix: str = "") -> str:
    form = _s(r.get(f"{prefix}salary_form_jp"))
    amount = _range(_s(r.get(f"{prefix}salary_display_method_jp")),
                    yen(_int(r.get(f"{prefix}minimum_salary"))),
                    yen(_int(r.get(f"{prefix}maximum_salary"))))
    return " ".join(x for x in [form, amount] if x)


def _allowance(r: dict, kind: str, label: str) -> str:
    typ = _s(r.get(f"{kind}_allowance_type_jp"))
    if not typ:
        return ""
    if typ.startswith("なし"):
        return f"{label}：なし"
    amount = _range(_s(r.get(f"{kind}_allowance_display_method_jp")),
                    yen(_int(r.get(f"lowest_{kind}_allowance"))),
                    yen(_int(r.get(f"limit_{kind}_allowance"))))
    return f"{label}：あり" + (f"（{amount}）" if amount else "")


def _salary_detail(r: dict) -> str:
    lines = []
    fot = _s(r.get("fixed_overtime_type_jp"))
    if fot.startswith("なし"):
        # 固定残業代が無ければ、給与の額がそのまま基本給
        base = _salary(r)
        if base:
            lines.append(f"基本給：{base}")
        lines.append("固定残業代：なし")
    elif fot:
        pay = _range(_s(r.get("fixed_overtime_display_method_jp")),
                     yen(_int(r.get("minimum_fixed_overtime_pay"))),
                     yen(_int(r.get("maximum_fixed_overtime_pay"))))
        hours = _range(_s(r.get("fixed_overtime_hours_display_method_jp")),
                       _hm(r.get("minimum_fixed_overtime_hour"), r.get("minimum_fixed_overtime_minute")),
                       _hm(r.get("maximum_fixed_overtime_hour"), r.get("maximum_fixed_overtime_minute")))
        detail = "、".join(x for x in [pay, (f"{hours}分" if hours.endswith("間") else f"{hours}相当") if hours else ""] if x)
        lines.append("固定残業代：あり" + (f"（{detail}）" if detail else ""))
        extra = _s(r.get("is_pay_overtime_jp"))
        if extra:
            lines.append(extra)
    for kind, label in [("commuting", "交通費"), ("others", "その他手当")]:
        a = _allowance(r, kind, label)
        if a:
            lines.append(a)
    tags = _tags(r.get("salary_allowance_id_name"))
    if tags:
        lines.append(f"給与の特徴：{tags}")
    for k in ("salary_supplement", "salary_example"):
        v = _s(r.get(k))
        if v:
            lines.append(v)
    return "\n".join(lines)


def _work_hours(r: dict) -> str:
    lines = []
    style = _s(r.get("working_style_jp"))
    other = _s(r.get("working_style_others_jp"))
    if style or other:
        lines.append("勤務形態：" + "／".join(x for x in [style, other] if x))
    method = _s(r.get("working_hours_display_method_jp"))
    rng = "範囲を指定する" if "範囲" in _s(r.get("working_hours_input_type_jp")) else "固定指定する"
    if "総労働" in method:
        n = _s(r.get("total_working_hours_time")) or "1"
        unit = _s(r.get("total_working_hours_unit_jp"))
        if "範囲" in rng:
            v = _range(rng, _hm(r.get("minimum_total_working_hours_hour"), r.get("minimum_total_working_hours_minute")),
                       _hm(r.get("maximum_total_working_hours_hour"), r.get("maximum_total_working_hours_minute")))
        else:
            v = _hm(r.get("total_working_hours_hour"), r.get("total_working_hours_minute"))
        if v:
            lines.append(f"総労働時間：{n}{unit}あたり{v}")
    elif method:
        if "範囲" in rng:
            v = _range(rng, _hm(r.get("minimum_working_hours_hour"), r.get("minimum_working_hours_minute")),
                       _hm(r.get("maximum_working_hours_hour"), r.get("maximum_working_hours_minute")))
        else:
            v = _hm(r.get("working_hours_hour"), r.get("working_hours_minute"))
        if v:
            lines.append(f"実働時間：1日あたり{v}")
    days = _range(_s(r.get("average_working_days_display_method_jp")),
                  f"{_int(r.get('lowest_working_days'))}日" if _int(r.get("lowest_working_days")) is not None else "",
                  f"{_int(r.get('limit_working_days'))}日" if _int(r.get("limit_working_days")) is not None else "")
    if days:
        lines.append(f"平均勤務日数：1ヶ月あたり{days}")
    tags = _tags(r.get("working_hours_id_name"))
    if tags:
        lines.append(f"勤務時間の特徴：{tags}")
    v = _s(r.get("working_time_supplement"))
    if v:
        lines.append(v)
    return "\n".join(lines)


def _holiday(r: dict) -> str:
    return _s(r.get("holiday")) or _tags(r.get("holiday_id_name"))


def _treatment(r: dict) -> str:
    lines = []
    ins = [(lab, _s(r.get(k))) for lab, k in [
        ("健康保険", "social_insurance_health_jp"), ("厚生年金保険", "social_insurance_wp_jp"),
        ("雇用保険", "social_insurance_ei_jp"), ("労災保険", "social_insurance_wc_jp")]]
    if any(v for _, v in ins):
        lines.append("社会保険：" + "、".join(f"{lab} {v}" for lab, v in ins if v))
    tags = _tags(r.get("welfare_id_name"))
    if tags:
        lines.append(f"福利厚生：{tags}")
    v = _s(r.get("welfare"))
    if v:
        lines.append(v)
    return "\n".join(lines)


def _trial(r: dict) -> str:
    has = _s(r.get("is_probationary_period_jp"))
    if not has:
        return ""
    if has.startswith("なし"):
        return "なし"
    lines = ["あり"]
    t = _s(r.get("probationary_time"))
    if t:
        lines.append(f"期間：{t}")
    cond = _s(r.get("probationary_salary_jp"))
    if cond:
        sal = _salary(r, "probationary_") if "異なる" in cond else ""
        lines.append(f"給与：{cond}" + (f"（{sal}）" if sal else ""))
    v = _s(r.get("probationary_period_supplement"))
    if v:
        lines.append(v)
    return "\n".join(lines)


def _address(r: dict) -> str:
    parts = [_s(r.get("working_location_prefecture")), _s(r.get("working_location_city_area"))]
    if _s(r.get("working_location_building_name_omission_flg")) != "1":
        parts.append(_s(r.get("working_location_building_name")))
    return "".join(parts)


def _personal(r: dict) -> str:
    lines = []
    v = _s(r.get("personal"))
    if v:
        lines.append(v)
    for k, lab in [("skill_id_name", "スキル"), ("description_id_name", "経験"), ("license_id_name", "資格")]:
        t = _tags(r.get(k))
        if t:
            lines.append(f"{lab}：{t}")
    return "\n".join(lines)


def compose_aw_honbun(r: dict) -> Optional[str]:
    """英語名をキーにした 1 行 → 本文。本文に使う列が XLSX に 1 つも無ければ None (書かない)。"""
    if not any(k in r for k in ("subtitle", "salary_form_jp", "holiday", "personal")):
        return None
    values = [
        _s(r.get("subtitle")),
        _salary(r),
        _salary_detail(r),
        _s(r.get("description")),
        _s(r.get("occupation_id_jp1")),
        _personal(r),
        _work_hours(r),
        _holiday(r),
        _treatment(r),
        _trial(r),
        _address(r),
        _s(r.get("working_location_access")),
    ]
    blocks = []
    for name, v in zip(HONBUN_ITEMS, values):
        blocks.append(f"{name}：\n{v}" if "\n" in v else f"{name}：{v}")
    return "\n".join(blocks)


def normalize(v: object) -> str:
    """前回の値と比べるための形 (改行の違い・前後の空白を無視)。"""
    return _s(v)
