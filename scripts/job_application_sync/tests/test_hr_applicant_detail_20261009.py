# -*- coding: utf-8 -*-
"""HR応募者の追加項目 (CSV 2段階目 + 詳細ページ) のテスト (2026-10-09)。

ネットワークなし。HubSpot は DryRunClient、ブラウザは偽の get_page。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import hr_applicant_fields as hrf
from scripts.job_application_sync.applicant_sync import Ledger
from scripts.job_application_sync.fetchers import hr_applicant_fetcher as hf
from scripts.job_application_sync.tests.test_applicant_import import (
    HR_RAW_HEADER, _write_hr_raw_csv,
)

FIX = Path(__file__).parent / "fixtures"
DETAIL_HTML = (FIX / "hr_applicant_detail.html").read_text(encoding="utf-8")
LOGIN_HTML = (FIX / "hr_login_page.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIX / "hr_detail_empty.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 詳細ページの解析
def test_詳細ページから質問文と回答を取る():
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    assert d["qa"] == [
        ("面接可能日時（3候補）", "10/20 午前"),
        ("メールアドレス（indeed メール以外）", "taro.sub@example.com"),
        ("自由項目3", "経験は3年です。\n土日も可能です。"),   # 質問文が空 → 自由項目N
    ]
    assert d["memo"] == "折り返し希望。\n夕方以降に電話。"
    assert d["selection_history"] == [
        ("2026-10-01 10:00", "未対応", ""),
        ("2026-10-02 15:30", "面接調整中", "日程を相談中"),
    ]


def test_学歴や選考理由の欄はqaやmemoに混ざらない():
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    flat = str((d["qa"], d["memo"], d["selection_history"]))
    assert "○○大学" not in flat and "本文に出ない選考理由" not in flat


def test_空の回答と空の履歴は飛ばす():
    d = hrf.parse_applicant_detail(EMPTY_HTML)
    assert d["qa"] == [] and d["memo"] == "" and d["selection_history"] == []
    assert d["fields"] == [{"name": "free_text_1", "label": "質問A", "value": ""},
                           {"name": "memo", "label": "メモ", "value": ""}]
    assert hrf.format_page_values(d) == {}
    assert hrf.format_all_fields(d) == ""


def test_ログイン画面は専用の例外():
    with pytest.raises(hrf.HrLoginPageError):
        hrf.parse_applicant_detail(LOGIN_HTML)


def test_想定外のページはNone():
    assert hrf.parse_applicant_detail("<html><body>Not Found</body></html>") is None
    assert hrf.parse_applicant_detail("") is None


def test_書き込み用テキストの整形():
    v = hrf.format_page_values(hrf.parse_applicant_detail(DETAIL_HTML))
    assert v["qa"] == (
        "面接可能日時（3候補）：10/20 午前\n"
        "メールアドレス（indeed メール以外）：taro.sub@example.com\n"
        "自由項目3：経験は3年です。\n土日も可能です。")
    assert v["memo"] == "折り返し希望。\n夕方以降に電話。"
    assert v["selection_history"] == (
        "2026-10-01 10:00／未対応\n2026-10-02 15:30／面接調整中／日程を相談中")


EXPECTED_FIELDS = [
    ("name", "氏名", "山田太郎"),
    ("name_kana", "フリガナ", "ヤマダタロウ"),
    ("birthday", "生年月日", "1990-01-01"),
    ("sex_id", "性別", "女性"),                       # radio: checked の label
    ("interview_at", "面接日時", ""),
    ("route_id", "応募経路", "求人ボックス"),          # select: selected の text
    ("free_text_1", "面接可能日時（3候補）", "10/20 午前"),
    ("free_text_2", "メールアドレス（indeed メール以外）", "taro.sub@example.com"),
    ("free_text_3", "", "経験は3年です。\n土日も可能です。"),
    ("school_career", "学歴", "○○大学 卒業"),
    ("memo", "メモ （社内用）", "折り返し希望。\n夕方以降に電話。"),
    ("occupation_id", "現在の職業", ""),               # selected なし / 「選択してください」
    ("work_period", "勤務可能期間", "3ヶ月以上"),
    ("contact_desired_date", "連絡希望日", "平日夕方"),
    ("tour_desired_date", "見学会希望日", ""),
    ("tel", "電話番号", "090-0000-0000"),
    ("email", "メールアドレス", "taro@example.com"),
    ("contact_method", "連絡方法", "電話"),
    ("zip", "郵便番号", "000-0000"),
    ("prefecture_id", "都道府県", "東京都"),
    ("city", "市区町村", "新宿区"),
    ("town", "町域", ""),
    ("address_line", "番地", "1-2-3"),
    ("building", "建物", ""),
    ("is_tour_desired", "見学会希望", "なし"),
    ("is_interview_desired", "希望面接形態", "オンライン"),
    ("selection_id", "選考ステータス", "面接調整中"),
    ("cause", "選考理由", "本文に出ない選考理由"),
]


def test_全項目fieldsをページ順に取る():
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    assert d["fields"] == [{"name": n, "label": lb, "value": v}
                           for n, lb, v in EXPECTED_FIELDS]
    names = [f["name"] for f in d["fields"]]
    # hidden / _method / csrf / 送信ボタン / name の無い検索欄は入らない
    for skipped in ("_method", "branch_id", "_csrf_token"):
        assert skipped not in names
    assert "検索語" not in str(d["fields"])
    # qa は従来どおり
    assert len(d["qa"]) == 3


def test_radioとcheckboxとselectの読み方():
    html = """<form action="https://hr-hacker.com/admin/applicants/edit/1">
    <ul><li class="box_fill_list_item"><div class="box_fill_list_item_left"><p class="name">希望</p></div>
    <div><input type="checkbox" name="a[]" id="a1" value="1" checked><label for="a1">朝</label>
    <input type="checkbox" name="a[]" id="a2" value="2"><label for="a2">昼</label>
    <label><input type="checkbox" name="a[]" id="a3" value="3" checked> 夜</label></div></li>
    <li class="box_fill_list_item"><div class="box_fill_list_item_left"><p class="name">区分</p></div>
    <div><input type="radio" name="k" value="1">甲<input type="radio" name="k" value="2" checked>乙</div></li>
    <li class="box_fill_list_item"><div class="box_fill_list_item_left"><p class="name">未選択</p></div>
    <div><select name="s"><option value="">選択してください</option><option value="1">X</option></select></div></li>
    </form>"""
    d = hrf.parse_applicant_detail(html)
    assert d["fields"] == [
        {"name": "a[]", "label": "希望", "value": "朝、夜"},
        {"name": "k", "label": "区分", "value": "乙"},
        {"name": "s", "label": "未選択", "value": ""},
    ]


def test_全項目の書き込み用テキスト():
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    assert hrf.format_all_fields(d) == (
        "氏名：山田太郎\n"
        "フリガナ：ヤマダタロウ\n"
        "生年月日：1990-01-01\n"
        "性別：女性\n"
        "応募経路：求人ボックス\n"
        "面接可能日時（3候補）：10/20 午前\n"
        "メールアドレス（indeed メール以外）：taro.sub@example.com\n"
        "free_text_3：経験は3年です。\n"
        "　土日も可能です。\n"
        "学歴：○○大学 卒業\n"
        "メモ （社内用）：折り返し希望。\n"
        "　夕方以降に電話。\n"
        "勤務可能期間：3ヶ月以上\n"
        "連絡希望日：平日夕方\n"
        "電話番号：090-0000-0000\n"
        "メールアドレス：taro@example.com\n"
        "連絡方法：電話\n"
        "郵便番号：000-0000\n"
        "都道府県：東京都\n"
        "市区町村：新宿区\n"
        "番地：1-2-3\n"
        "見学会希望：なし\n"
        "希望面接形態：オンライン\n"
        "選考ステータス：面接調整中\n"
        "選考理由：本文に出ない選考理由")
    assert hrf.format_page_values(d)["all_fields"] == hrf.format_all_fields(d)


def test_all_fieldsを対応先に向けるとまとめて1プロパティ(monkeypatch):
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "all_fields", ("hr_all_test", "fill_empty"))
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    assert hrf.active_page_fields({"hr_all_test"}) == {"all_fields": "hr_all_test"}
    props = hrf.page_props_from_parsed(d, {"all_fields": "hr_all_test"})
    assert props == {"hr_all_test": hrf.format_all_fields(d)}


def test_メールの回答はmeeruadoresuと混ぜない(monkeypatch):
    """CSVのメール(meeruadoresu)と、自由項目の回答は別物。qaにだけ入る。"""
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "qa", ("hr_qa_test", "fill_empty"))
    d = hrf.parse_applicant_detail(DETAIL_HTML)
    props = hrf.page_props_from_parsed(d, {"qa": "hr_qa_test"})
    assert set(props) == {"hr_qa_test"}
    assert "meeruadoresu" not in props
    assert "メールアドレス（indeed メール以外）：taro.sub@example.com" in props["hr_qa_test"]


# ---------------------------------------------------------------- 対応表
CSV_EXPECTED = {
    "応募者id": "hr_oubosha_id", "応募経路": "hr_oubo_keiyu",
    "現在の職業": "hr_genzai_shokugyou", "連絡方法": "hr_renraku_houhou",
    "見学会希望有無": "hr_kengaku_kibou", "見学会希望日": "hr_kengaku_kibou_bi",
    "希望面接形態": "hr_mensetsu_keitai", "勤務可能期間": "hr_kinmu_kanou_kikan",
    "選考ステータス": "hr_senkou_status", "面接予定日": "hr_mensetsu_yotei",
    "選考理由": "hr_senkou_riyuu",
}
PAGE_EXPECTED = {
    "qa": "hr_shitsumon_kaitou", "memo": "hr_memo",
    "selection_history": "hr_senkou_rireki", "all_fields": "hr_oubosha_shousai_zen",
}


def test_対応表():
    assert hrf.CSV_FIELD_MAP["学歴"] == ("gakureki", "fill_empty")
    assert hrf.CSV_FIELD_MAP["連絡希望日"] == ("renrakukanouyoubijikantai", "fill_empty")
    for col, prop in CSV_EXPECTED.items():
        assert hrf.CSV_FIELD_MAP[col] == (prop, "hr_copy"), col
    for col in ("更新日", "店舗ID", "店舗名", "自由項目1", "自由項目2", "自由項目3"):
        assert col not in hrf.CSV_FIELD_MAP
    assert set(hrf.CSV_FIELD_MAP) == set(CSV_EXPECTED) | {"学歴", "連絡希望日"}
    # 勤務可能期間は hr_kinmu_kanou_kikan だけ。学歴は gakureki だけ (重複させない)
    props = [p for p, _ in hrf.CSV_FIELD_MAP.values()]
    assert len(props) == len(set(props))
    assert hrf.PAGE_FIELD_MAP == {k: (v, "fill_empty") for k, v in PAGE_EXPECTED.items()}


def test_存在しないプロパティは落とす():
    kept, dropped = hrf.filter_known_props(
        {"gakureki": "大卒", "nai_prop": "x"}, {"gakureki", "other"})
    assert kept == {"gakureki": "大卒"} and dropped == ["nai_prop"]
    # 一覧が取れなかった (None) ときは全部落とす
    assert hrf.filter_known_props({"gakureki": "大卒"}, None) == ({}, ["gakureki"])


def test_詳細ページを取る価値のある項目():
    assert hrf.active_page_fields({"gakureki"}) == {}          # 対応先が実在しない → 空
    assert hrf.active_page_fields(None) == {}
    # 4項目とも実在するなら4項目すべて有効
    assert hrf.active_page_fields(set(PAGE_EXPECTED.values())) == PAGE_EXPECTED


def test_対応先が実在するときだけ詳細項目が有効(monkeypatch):
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "memo", ("hr_memo_test", "fill_empty"))
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "qa", ("hr_qa_missing", "fill_empty"))
    assert hrf.active_page_fields({"hr_memo_test"}) == {"memo": "hr_memo_test"}
    assert hrf.active_page_fields(set()) == {}


# ---------------------------------------------------------------- 書き込み方針
def test_方針_fill_emptyは空のときだけ_hr_copyは違うとき(monkeypatch):
    monkeypatch.setitem(hrf.CSV_FIELD_MAP, "現在の職業", ("hr_job_test", "hr_copy"))
    wanted = {"gakureki": "大卒", "renrakukanouyoubijikantai": "夕方",
              "hr_job_test": "会社員"}
    cur = {"gakureki": "高卒(人が入力)", "renrakukanouyoubijikantai": "",
           "hr_job_test": "学生"}
    assert hrf.plan_property_updates(wanted, cur) == {
        "renrakukanouyoubijikantai": "夕方", "hr_job_test": "会社員"}
    # hr_copy は同じ値なら更新しない
    assert hrf.plan_property_updates({"hr_job_test": "会社員"},
                                     {"hr_job_test": "会社員"}) == {}


def test_更新時_実際の対応表の方針():
    wanted = {"hr_senkou_status": "面接済", "hr_mensetsu_yotei": "2026-10-25",
              "hr_oubo_keiyu": "", "gakureki": "大卒",
              "renrakukanouyoubijikantai": "夕方", "hr_memo": "新メモ"}
    cur = {"hr_senkou_status": "未対応", "hr_mensetsu_yotei": "2026-10-25",
           "hr_oubo_keiyu": "Indeed", "gakureki": "高卒(人が入力)",
           "renrakukanouyoubijikantai": "", "hr_memo": "人のメモ"}
    # hr_copy: 違えば更新 / 同じ・空は書かない。fill_empty: 空のときだけ
    assert hrf.plan_property_updates(wanted, cur) == {
        "hr_senkou_status": "面接済", "renrakukanouyoubijikantai": "夕方"}


def test_空の値は書かない(monkeypatch):
    monkeypatch.setitem(hrf.CSV_FIELD_MAP, "現在の職業", ("hr_job_test", "hr_copy"))
    assert hrf.plan_property_updates(
        {"hr_job_test": "", "gakureki": "  "}, {"hr_job_test": "学生"}) == {}


# ---------------------------------------------------------------- CSV (1段階目)
def _csv_with(tmp_path, gakureki="大卒", kibou="夕方以降"):
    """HR生CSV1行 (学歴=23列目, 連絡希望日=18列目)。"""
    cols = HR_RAW_HEADER.split(",")
    vals = {c: "" for c in cols}
    vals.update({"応募者id": "900001", "応募求人先": "HR-7001", "選考ステータス": "未対応",
                 "名前": "山田太郎", "名前フリガナ": "ヤマダタロウ", "性別": "男性",
                 "生年月日": "1990-01-01", "電話番号": "090-0000-0000",
                 "メールアドレス": "taro@example.com", "郵便番号": "000-0000",
                 "都道府県": "東京都", "市区町村": "新宿区", "学歴": gakureki,
                 "連絡希望日": kibou, "応募日時": "2026-10-01 09:00:00"})
    p = tmp_path / "hr.csv"
    p.write_bytes((HR_RAW_HEADER + "\n" + ",".join(vals[c] for c in cols) + "\n")
                  .encode("cp932"))
    return p


def test_CSVから追加項目と応募者idを読む(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    assert row.hr_applicant_id == "900001"
    # 学歴・連絡希望日 + 応募者idなど HR コピー11項目の一部 (_csv_with が入れた分)
    assert row.extra == {"gakureki": "大卒", "renrakukanouyoubijikantai": "夕方以降",
                         "hr_oubosha_id": "900001", "hr_senkou_status": "未対応"}


def test_CSV全項目が対応プロパティに入る(tmp_path):
    """実際の対応表で、CSV由来の13プロパティが具体値で出る。"""
    cols = HR_RAW_HEADER.split(",")
    vals = {c: "" for c in cols}
    vals.update({
        "応募者id": "900002", "応募求人先": "HR-7001", "応募経路": "Indeed",
        "名前": "山田太郎", "現在の職業": "会社員", "連絡方法": "電話",
        "見学会希望有無": "あり", "見学会希望日": "2026-10-20",
        "希望面接形態": "対面", "学歴": "大卒", "勤務可能期間": "3か月以上",
        "連絡希望日": "夕方以降", "選考ステータス": "面接調整中",
        "面接予定日": "2026-10-25", "選考理由": "経験者のため",
        "応募日時": "2026-10-01 09:00:00",
        "更新日": "2026-10-02", "店舗ID": "S1", "店舗名": "新宿店",
        "自由項目1": "回答1"})
    p = tmp_path / "hr_all.csv"
    p.write_bytes((HR_RAW_HEADER + "\n" + ",".join(vals[c] for c in cols) + "\n")
                  .encode("cp932"))
    [row] = ai.load_applicants_csv(p)
    assert row.extra == {
        "hr_oubosha_id": "900002", "hr_oubo_keiyu": "Indeed",
        "hr_genzai_shokugyou": "会社員", "hr_renraku_houhou": "電話",
        "hr_kengaku_kibou": "あり", "hr_kengaku_kibou_bi": "2026-10-20",
        "hr_mensetsu_keitai": "対面", "hr_kinmu_kanou_kikan": "3か月以上",
        "hr_senkou_status": "面接調整中", "hr_mensetsu_yotei": "2026-10-25",
        "hr_senkou_riyuu": "経験者のため",
        "gakureki": "大卒", "renrakukanouyoubijikantai": "夕方以降"}
    assert len(row.extra) == 13


def test_CSVの空欄は追加項目に入らない(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path, gakureki="", kibou=""))
    # 空欄の学歴・連絡希望日は入らない (応募者id・選考ステータスは値があるので残る)
    assert row.extra == {"hr_oubosha_id": "900001", "hr_senkou_status": "未対応"}


def test_追加項目を差し込んだ作成プロパティ(tmp_path, monkeypatch):
    monkeypatch.setitem(hrf.CSV_FIELD_MAP, "現在の職業", ("hr_job_test", "hr_copy"))
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    row.extra["hr_job_test"] = "会社員"
    props = ai.build_appointment_properties(row, linked=True)
    assert props["gakureki"] == "大卒"
    assert props["renrakukanouyoubijikantai"] == "夕方以降"
    assert props["hr_job_test"] == "会社員"
    assert props["meeruadoresu"] == "taro@example.com"
    assert "応募者id" not in props


def test_生成時_実在しないプロパティは書かず結果に記録(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    cl = ai.DryRunClient(listings_hr={"HR-7001": "L1"})
    cl.known_props = {"gakureki"}            # renrakukanouyoubijikantai は無い
    res = ai.process_applicant(row, cl)
    created = cl.created_appts[0]
    created = created.get("properties", created)
    assert created["gakureki"] == "大卒"
    assert "renrakukanouyoubijikantai" not in created
    assert res.dropped_props == ["hr_oubosha_id", "hr_senkou_status",
                                 "renrakukanouyoubijikantai"]
    assert res.hr_applicant_id == "900001"


def test_プロパティ一覧が取れないときは追加項目を全部書かない(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    cl = ai.DryRunClient(listings_hr={"HR-7001": "L1"})      # known_props 未設定=None
    res = ai.process_applicant(row, cl)
    created = cl.created_appts[0]
    created = created.get("properties", created)
    assert "gakureki" not in created and "renrakukanouyoubijikantai" not in created
    assert res.status == "linked"


def _dup_client(existing_props):
    cl = ai.DryRunClient(listings_hr={"HR-7001": "L1"})
    cl.known_props = {"gakureki", "renrakukanouyoubijikantai", "hr_job_test"}
    cl.find_existing_appointment = lambda *a, **k: "A1"
    cl.existing_appt_props = {"A1": existing_props}
    return cl


def test_重複時_fill_emptyは既存値を守り空なら埋める(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    cl = _dup_client({"gakureki": "人が直した学歴", "renrakukanouyoubijikantai": ""})
    res = ai.process_applicant(row, cl)
    assert res.status == "skip_duplicate"
    [u] = [x for x in cl.updated_appts if x["id"] == "A1"]
    assert u["properties"]["renrakukanouyoubijikantai"] == "夕方以降"
    assert "gakureki" not in u["properties"]


def test_重複時_hr_copyは違えば更新_同じなら更新しない(tmp_path, monkeypatch):
    monkeypatch.setitem(hrf.CSV_FIELD_MAP, "現在の職業", ("hr_job_test", "hr_copy"))
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    row.extra["hr_job_test"] = "会社員"
    cl = _dup_client({"gakureki": "x", "renrakukanouyoubijikantai": "y",
                      "hr_job_test": "学生"})
    ai.process_applicant(row, cl)
    [u] = cl.updated_appts
    assert u["properties"]["hr_job_test"] == "会社員"
    assert "gakureki" not in u["properties"]

    cl2 = _dup_client({"gakureki": "x", "renrakukanouyoubijikantai": "y",
                       "hr_job_test": "会社員"})
    ai.process_applicant(row, cl2)
    written = {k for u in getattr(cl2, "updated_appts", []) for k in u["properties"]}
    assert not written & {"hr_job_test", "gakureki", "renrakukanouyoubijikantai"}


def test_重複時_現在値が読めなければ何も書かない(tmp_path):
    [row] = ai.load_applicants_csv(_csv_with(tmp_path))
    cl = _dup_client({})
    cl.strict_read_fails = True
    ai.process_applicant(row, cl)
    assert not getattr(cl, "updated_appts", [])


# ---------------------------------------------------------------- 詳細ページの書込
def test_詳細ページ_実際の対応表で4プロパティ(tmp_path):
    parsed = hrf.parse_applicant_detail(DETAIL_HTML)
    fields = hrf.active_page_fields(set(PAGE_EXPECTED.values()))
    props = hrf.page_props_from_parsed(parsed, fields)
    vals = hrf.format_page_values(parsed)
    assert props == {
        "hr_shitsumon_kaitou": vals["qa"], "hr_memo": "折り返し希望。\n夕方以降に電話。",
        "hr_senkou_rireki": vals["selection_history"],
        "hr_oubosha_shousai_zen": vals["all_fields"]}
    assert all(props.values()) and "メールアドレス（indeed メール以外）：" in props["hr_shitsumon_kaitou"]
    # 4つとも fill_empty: HubSpot が空なら書く、値があれば書かない
    cl = ai.DryRunClient()
    cl.existing_appt_props = {"A1": {k: "" for k in props}}
    assert hrf.apply_page_fields(cl, "A1", parsed, fields) == "written"
    assert cl.updated_appts == [{"id": "A1", "properties": props}]
    cl2 = ai.DryRunClient()
    cl2.existing_appt_props = {"A1": {**{k: "" for k in props}, "hr_memo": "人のメモ"}}
    assert hrf.apply_page_fields(cl2, "A1", parsed, fields) == "written"
    assert "hr_memo" not in cl2.updated_appts[0]["properties"]


def test_詳細ページ_fill_emptyで書く(monkeypatch):
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "memo", ("hr_memo_test", "fill_empty"))
    parsed = hrf.parse_applicant_detail(DETAIL_HTML)
    fields = {"memo": "hr_memo_test"}
    cl = ai.DryRunClient()
    cl.existing_appt_props = {"A1": {"hr_memo_test": ""}}
    assert hrf.apply_page_fields(cl, "A1", parsed, fields) == "written"
    assert cl.updated_appts == [{"id": "A1", "properties": {
        "hr_memo_test": "折り返し希望。\n夕方以降に電話。"}}]
    # 既に値があれば書かない
    cl2 = ai.DryRunClient()
    cl2.existing_appt_props = {"A1": {"hr_memo_test": "人が書いたメモ"}}
    assert hrf.apply_page_fields(cl2, "A1", parsed, fields) == "nothing"
    assert not getattr(cl2, "updated_appts", [])
    # 読めなければ error で書かない
    cl3 = ai.DryRunClient()
    cl3.strict_read_fails = True
    assert hrf.apply_page_fields(cl3, "A1", parsed, fields) == "error"


# ---------------------------------------------------------------- 取得対象の選び方
def test_台帳で完了済みと諦めを除き上限で切る(tmp_path):
    led = Ledger(path=tmp_path / "l.json")
    hrf.record_detail_done(led, "1")
    for _ in range(3):
        hrf.record_detail_failure(led, "2")
    hrf.record_detail_failure(led, "3")            # 1回失敗 → まだ対象
    picked, done, gave_up = hrf.select_detail_ids(
        led, ["1", "2", "3", "4", "5", "6", "4"], max_n=2)
    assert picked == ["3", "4"]
    assert (done, gave_up) == (1, 1)
    assert led.meta("hr_detail", "3")["attempts"] == 1
    assert led.meta("hr_detail", "2")["attempts"] == 3
    # 保存して読み直しても残る
    led.save()
    assert Ledger(path=tmp_path / "l.json").meta("hr_detail", "1")["detail_status"] == "done"


# ---------------------------------------------------------------- 取得ループ
class FakeBrowser:
    """get_page の偽物。pages: {id: (status, html)}。呼ばれたURLと時刻を残す。"""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def get(self, url, timeout_ms):
        hr_id = url.rsplit("/", 1)[1]
        self.calls.append(hr_id)
        r = self.pages[hr_id]
        if isinstance(r, Exception):
            raise r
        return r


def _run(coro):
    return asyncio.run(coro)


def _fake_time():
    t = {"now": 0.0}
    sleeps = []

    async def sleeper(s):
        sleeps.append(s)
        t["now"] += s
    return t, sleeps, sleeper, (lambda: t["now"])


def test_取得ループ_間隔と上限():
    b = FakeBrowser({str(i): (200, DETAIL_HTML) for i in range(10)})
    t, sleeps, sleeper, clock = _fake_time()
    r = _run(hf.fetch_details(b.get, [str(i) for i in range(10)], max_n=3,
                              sleep_s=0.2, sleeper=sleeper, clock=clock))
    assert b.calls == ["0", "1", "2"]               # 上限3件
    assert sleeps == [1.0, 1.0]                      # 指定が0.2でも1秒未満にはしない
    assert sorted(r.parsed) == ["0", "1", "2"] and r.failed == [] and r.stopped == ""


def test_取得ループ_失敗は未取得で空として扱わない():
    b = FakeBrowser({"1": (500, "err"), "2": asyncio.TimeoutError(),
                     "3": (200, DETAIL_HTML), "4": (200, "<html>?</html>")})
    t, sleeps, sleeper, clock = _fake_time()
    r = _run(hf.fetch_details(b.get, ["1", "2", "3", "4"], sleeper=sleeper, clock=clock))
    assert list(r.parsed) == ["3"]
    assert r.failed == ["1", "2", "4"]


def test_取得ループ_時間切れで残りは未着手():
    b = FakeBrowser({str(i): (200, DETAIL_HTML) for i in range(6)})
    t, sleeps, sleeper, clock = _fake_time()
    r = _run(hf.fetch_details(b.get, [str(i) for i in range(6)], budget_s=2.5,
                              sleeper=sleeper, clock=clock))
    assert sorted(r.parsed) == ["0", "1", "2"]      # 0s,1s,2s で取り、3s で打ち切り
    assert r.not_reached == ["3", "4", "5"] and r.stopped == "budget"


def test_取得ループ_ログイン画面なら張り直して同じ人を取り直す():
    first = FakeBrowser({"1": (200, LOGIN_HTML), "2": (200, LOGIN_HTML)})
    second = FakeBrowser({"1": (200, DETAIL_HTML), "2": (200, DETAIL_HTML)})
    n = {"relogin": 0}

    async def relogin():
        n["relogin"] += 1
        return second.get
    t, sleeps, sleeper, clock = _fake_time()
    r = _run(hf.fetch_details(first.get, ["1", "2"], relogin=relogin,
                              sleeper=sleeper, clock=clock))
    assert n["relogin"] == 1 and r.relogins == 1
    assert first.calls == ["1"] and second.calls == ["1", "2"]
    assert sorted(r.parsed) == ["1", "2"] and r.stopped == ""


def test_取得ループ_張り直してもログイン画面なら打ち切り():
    first = FakeBrowser({"1": (200, LOGIN_HTML), "2": (200, LOGIN_HTML)})
    second = FakeBrowser({"1": (200, LOGIN_HTML), "2": (200, LOGIN_HTML)})

    async def relogin():
        return second.get
    t, sleeps, sleeper, clock = _fake_time()
    r = _run(hf.fetch_details(first.get, ["1", "2"], relogin=relogin,
                              sleeper=sleeper, clock=clock))
    assert r.parsed == {} and r.failed == []
    assert r.not_reached == ["1", "2"] and r.stopped == "login"
    assert second.calls == ["1"]                     # 張り直しは1回だけ


def test_不正な応募者idはURLにしない():
    with pytest.raises(ValueError):
        hf.detail_url("1/../../admin")
    assert hf.detail_url("900001") == "https://hr-hacker.com/admin/applicants/edit/900001"


# ---------------------------------------------------------------- sync 側の組み立て
def test_詳細取得の対象選び_新しい応募から(tmp_path):
    from scripts.job_application_sync import applicant_sync as sync
    p = _write_hr_raw_csv(tmp_path, n=5)             # 応募者id A1..A5 (応募日はまちまち)
    led = Ledger(path=tmp_path / "l.json")
    hrf.record_detail_done(led, "A1")
    stats: dict = {}
    ids = sync._select_hr_detail_ids(p, led, stats)
    assert "A1" not in ids and len(ids) == 4
    assert stats["done_before"] == 1 and stats["selected"] == 4


def test_対応先なしなら詳細ページは取らない_ブラウザ呼び出しなし(tmp_path, monkeypatch):
    """PAGE_FIELD_MAP が全部 None のとき: select が None のまま取得関数へ渡る。"""
    for k, (_p, pol) in list(hrf.PAGE_FIELD_MAP.items()):
        monkeypatch.setitem(hrf.PAGE_FIELD_MAP, k, (None, pol))
    from scripts.job_application_sync import applicant_sync as sync
    p = _write_hr_raw_csv(tmp_path, n=2)
    seen = {}

    async def fake_fetch(out_dir, df, dt, select_ids):
        seen["select"] = select_ids
        return p, hf.DetailResult()

    class FakeCli(ai.DryRunClient):
        pass

    cli = FakeCli()
    cli.known_props = {"gakureki", "renrakukanouyoubijikantai"}
    monkeypatch.setattr(sync, "_fetch_hr_csv_with_details", fake_fetch)
    monkeypatch.setattr(ai, "RealHubSpotClient", lambda token: cli)
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    res = sync.process_hr_batch([], tmp_path, dry_run=False,
                                date_from="2026-10-01", date_to="2026-10-03",
                                ledger=Ledger(path=tmp_path / "l.json"))
    assert seen["select"] is None
    assert res["ok"] is True


# ---------------------------------------------------------------- 65,536文字の上限
def test_clip_70000文字は上限以内で注記付き():
    out, hit = hrf.clip_for_hubspot("あ" * 70000)
    assert hit is True
    assert len(out) <= 65536
    assert out.endswith("\n（以下省略。全文は HRハッカーの応募者詳細で確認）")
    assert out.startswith("あ" * 100)


def test_clip_ちょうど65536文字と短い値はそのまま():
    v = "い" * 65536
    assert hrf.clip_for_hubspot(v) == (v, False)
    assert hrf.clip_for_hubspot("短い") == ("短い", False)
    out, hit = hrf.clip_for_hubspot("う" * 65537)
    assert hit and len(out) == 65536


def test_詳細ページの長すぎる値は切って送る(monkeypatch):
    monkeypatch.setitem(hrf.PAGE_FIELD_MAP, "memo", ("hr_memo_test", "fill_empty"))
    hrf.take_clipped()
    parsed = {"memo": "ア" * 70000}
    cl = ai.DryRunClient()
    cl.existing_appt_props = {"A1": {"hr_memo_test": ""}}
    assert hrf.apply_page_fields(cl, "A1", parsed, {"memo": "hr_memo_test"}) == "written"
    sent = cl.updated_appts[0]["properties"]["hr_memo_test"]
    assert len(sent) <= 65536 and sent.endswith("応募者詳細で確認）")
    assert hrf.take_clipped() == ["hr_memo_test"]


def test_CSVの長すぎる値も切る():
    hrf.take_clipped()
    col = next(c for c, (p, _) in hrf.CSV_FIELD_MAP.items() if p)
    prop = hrf.CSV_FIELD_MAP[col][0]
    out = hrf.csv_extra_from_raw({col: "x" * 70000})
    assert len(out[prop]) <= 65536
    assert hrf.take_clipped() == [prop]
