"""求人票の本文（AirWork）の組み立て (2026-11-24)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aw_honbun as H  # noqa: E402
import airwork_import as awi  # noqa: E402


def _row(**kw):
    base = {
        "subtitle": "見出しの例",
        "salary_form_jp": "月給", "salary_display_method_jp": "範囲を指定する",
        "minimum_salary": "220710", "maximum_salary": "269700",
        "fixed_overtime_type_jp": "なし",
        "description": "仕事の例\n二行目",
        "occupation_id_jp1": "職種の例",
        "personal": "未経験歓迎",
        "working_style_jp": "固定時間制",
        "working_hours_display_method_jp": "実働・みなし労働時間で記載",
        "working_hours_input_type_jp": "固定指定する",
        "working_hours_hour": "8", "working_hours_minute": "0",
        "average_working_days_display_method_jp": "範囲を指定する",
        "lowest_working_days": "20", "limit_working_days": "21",
        "holiday": "", "holiday_id_name": "年間休日120日以上,完全週休2日制",
        "social_insurance_health_jp": "有", "social_insurance_wp_jp": "有",
        "is_probationary_period_jp": "なし",
        "working_location_prefecture": "宮城県", "working_location_city_area": "仙台市青葉区",
        "working_location_building_name_omission_flg": "1", "working_location_building_name": "1-2-3",
        "working_location_access": "駅から徒歩5分",
        "contact_email": "x@example.com", "contact_phone": "000", "contact_name": "担当",
    }
    base.update(kw)
    return base


def test_yen():
    assert H.yen(220710) == "22万710円"
    assert H.yen(250000) == "25万円"
    assert H.yen(1100) == "1100円"


def test_header_keys():
    assert H.header_keys(["求人番号(job_offer_id)", "平均勤務日数下限（lowest_working_days)", "x"]) == \
        ["job_offer_id", "lowest_working_days", "x"]


def test_compose_items_and_order():
    t = H.compose_aw_honbun(_row())
    lines = t.split("\n")
    heads = [l.split("：")[0] for l in lines if l.split("：")[0] in H.HONBUN_ITEMS]
    assert heads == H.HONBUN_ITEMS
    assert "給与：月給 22万710円 〜 26万9700円" in lines
    assert "基本給：月給 22万710円 〜 26万9700円" in lines
    assert "固定残業代：なし" in lines
    assert "実働時間：1日あたり8時間" in lines
    assert "平均勤務日数：1ヶ月あたり20日 〜 21日" in lines
    assert "休日休暇：年間休日120日以上、完全週休2日制" in lines
    assert "試用・研修期間：なし" in lines
    # 建物名は省略の印があれば出さない
    assert "勤務地：宮城県仙台市青葉区" in lines
    # 複数行の値は「列名：」の次の行から
    i = lines.index("仕事内容：")
    assert lines[i + 1:i + 3] == ["仕事の例", "二行目"]
    # 問い合わせ先は入れない
    assert "x@example.com" not in t and "担当" not in t


def test_empty_item_keeps_label():
    t = H.compose_aw_honbun(_row(subtitle="", working_location_access=""))
    assert t.startswith("キャッチコピー：\n給与：")
    assert t.endswith("アクセス：")


def test_fixed_overtime_and_lower_bound():
    t = H.compose_aw_honbun(_row(
        salary_display_method_jp="下限を指定する", maximum_salary="",
        fixed_overtime_type_jp="あり（テンプレート入力）",
        fixed_overtime_display_method_jp="範囲を指定する",
        minimum_fixed_overtime_pay="40830", maximum_fixed_overtime_pay="63720",
        fixed_overtime_hours_display_method_jp="固定指定する", minimum_fixed_overtime_hour="20",
        is_pay_overtime_jp="固定残業時間を超過した場合は追加で残業手当を支給する"))
    assert "給与：月給 22万710円 〜" in t.split("\n")
    assert "固定残業代：あり（4万830円 〜 6万3720円、20時間分）" in t
    assert "基本給：" not in t


def test_total_hours_and_holiday_text_wins():
    t = H.compose_aw_honbun(_row(
        working_hours_display_method_jp="総労働時間で記載",
        total_working_hours_time="1", total_working_hours_unit_jp="ヶ月", total_working_hours_hour="172",
        holiday="土日祝"))
    assert "総労働時間：1ヶ月あたり172時間" in t
    assert "休日休暇：土日祝" in t


def test_no_columns_returns_none():
    assert H.compose_aw_honbun({"job_offer_id": "1"}) is None


def test_update_writes_only_when_changed():
    row = {"media_job_id": "1", "original_status": "02", "aw_honbun": "給与：月給 25万円"}
    p = awi.build_update_props(row, {H.PROP_AW_HONBUN: "給与：月給 25万円\r\n"}, "2026-11-24",
                               "2026-11-24T00:00:00Z", "f.xlsx")
    assert H.PROP_AW_HONBUN not in p
    p = awi.build_update_props(row, {H.PROP_AW_HONBUN: "給与：月給 24万円"}, "2026-11-24",
                               "2026-11-24T00:00:00Z", "f.xlsx")
    assert p[H.PROP_AW_HONBUN] == "給与：月給 25万円"
    # 本文が作れなかった (列が無い) ときは書かない
    p = awi.build_update_props({**row, "aw_honbun": ""}, {}, "2026-11-24", "2026-11-24T00:00:00Z", "f.xlsx")
    assert H.PROP_AW_HONBUN not in p


def test_create_includes_honbun():
    row = {"media_job_id": "1", "original_status": "02", "job_name": "x", "aw_honbun": "本文"}
    p = awi.build_create_props(row, "2026-11-24", "2026-11-24T00:00:00Z", "f.xlsx", "login")
    assert p[H.PROP_AW_HONBUN] == "本文"
