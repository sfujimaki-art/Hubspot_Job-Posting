"""applicant_import.py ユニットテスト (WBS 1.11.9 B-1)。

最低4ケース要件:
  1. 媒体=HRハッカー で LISTING検索キー id_hrhakkaa が使われること
  2. 媒体=AirWork で LISTING検索キー id_airwork + airwork_account_login_id が両方使われること
  3. 求人未特定時にタイトル類似度マッチングを試みない (v0.2 §24-1, §24-2 ガード)
  4. 重複応募検出: 同一(media, 応募日, email優先/phoneフォールバック) で skip
     (2026-07-03 実測修正: denwaseikika は Search EQ でエラーになるため使用禁止)
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts.job_application_sync import applicant_import as ai


FIXTURE_CSV = Path(__file__).parent / "fixtures" / "sample_applicants.csv"


# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------
@pytest.fixture
def rows():
    return ai.load_applicants_csv(FIXTURE_CSV)


@pytest.fixture
def hr_row(rows):
    return next(r for r in rows if r.media == "HRハッカー" and r.media_job_id == "HR-1001")


@pytest.fixture
def aw_row(rows):
    return next(r for r in rows if r.media == "AirWork" and r.media_job_id == "AW-2002"
                and r.airwork_login_id == "login_user_a" and "鈴木" in r.name)


# ============================================================================
# CASE 1: HR → id_hrhakkaa 検索キー使用
# ============================================================================
def test_hr_uses_id_hrhakkaa(hr_row):
    """媒体=HRハッカー の場合 LISTING検索キー id_hrhakkaa が使われる。"""
    client = ai.DryRunClient(listings_hr={"HR-1001": "L-9001"})

    result = ai.process_applicant(hr_row, client)

    # 検索ログに id_hrhakkaa キーが含まれ、id_airwork は含まれない
    hr_searches = [s for s in client.search_log if s["op"] == "search_listing_hr"]
    aw_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    assert len(hr_searches) == 1
    assert hr_searches[0]["key"] == {"id_hrhakkaa": "HR-1001"}
    assert len(aw_searches) == 0  # HR行で AW 検索が走ってはいけない

    # LISTING ヒット → linked & Association 張られる
    assert result.status == "linked"
    assert result.listing_id == "L-9001"
    assert len(client.associations) == 1
    assert client.associations[0]["type_id"] == ai.ASSOC_TYPE_ID_APPT_TO_LISTING == 5


# COV-03: ②Noteコピー配線 (linkedのみ発火・unlinked/skipで非発火)
def test_linked_triggers_note_copy(hr_row):
    """求人特定(linked)時に client.copy_listing_note が呼ばれる。"""
    client = ai.DryRunClient(listings_hr={"HR-1001": "L-9001"})
    result = ai.process_applicant(hr_row, client)
    assert result.status == "linked"
    assert len(client.note_copies) == 1
    assert client.note_copies[0] == {"listing_id": "L-9001",
                                     "appointment_id": result.appointment_id}


def test_unlinked_does_not_trigger_note_copy(hr_row):
    """求人未特定(unlinked)では ②Noteコピーを呼ばない。"""
    client = ai.DryRunClient(listings_hr={})  # ヒットしない
    result = ai.process_applicant(hr_row, client)
    assert result.status == "unlinked"
    assert client.note_copies == []


# ============================================================================
# CASE 2: AW → id_airwork + airwork_account_login_id 両方使用
# ============================================================================
def test_aw_uses_id_airwork_and_login_id(aw_row):
    """媒体=AirWork の場合 LISTING検索キー id_airwork + airwork_account_login_id 両方使われる。"""
    client = ai.DryRunClient(listings_aw={("AW-2002", "login_user_a"): "L-9002"})

    result = ai.process_applicant(aw_row, client)

    aw_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    hr_searches = [s for s in client.search_log if s["op"] == "search_listing_hr"]
    assert len(aw_searches) == 1, "AW検索が1回呼ばれるべき"
    assert len(hr_searches) == 0, "AW行で HR検索が走ってはいけない"

    key = aw_searches[0]["key"]
    assert key["id_airwork"] == "AW-2002", "id_airwork が検索キーに含まれること"
    assert key["airwork_account_login_id"] == "login_user_a", \
        "airwork_account_login_id が検索キーに含まれること (AW LISTING複合ユニーク制約)"

    assert result.status == "linked"
    assert result.listing_id == "L-9002"
    assert client.associations[0]["type_id"] == 5


def _aw_row_no_login(job_id: str = "AW-2002") -> "ai.ApplicantRow":
    return ai.ApplicantRow(
        name="X", kana="", phone="0123", email="x@x", apply_date="2026-06-20",
        media="AirWork", media_job_id=job_id, airwork_login_id="",  # 欠落
        raw_lineno=99,
    )


def test_aw_without_login_id_single_hit_links():
    """AW login_id 無し: id_airwork 単独検索で1件ヒット → linked (AND検索は使わない)。"""
    client = ai.DryRunClient(listings_aw={("AW-2002", "login_user_a"): "L-9002"})
    result = ai.process_applicant(_aw_row_no_login(), client)

    and_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    single_searches = [s for s in client.search_log if s["op"] == "search_listing_aw_by_id"]
    assert len(and_searches) == 0, "login_id 無し時は複合キーAND検索を呼ばない"
    assert len(single_searches) == 1
    assert single_searches[0]["key"] == {"id_airwork": "AW-2002"}

    assert result.status == "linked"
    assert result.listing_id == "L-9002"
    assert len(client.associations) == 1


def test_aw_without_login_id_multiple_hits_ambiguous_unlinked():
    """AW login_id 無し: 複数ヒット → 誤紐付け絶対禁止 (v0.2 §24) で unlinked + ambiguous。"""
    client = ai.DryRunClient(listings_aw={
        ("AW-2002", "login_user_a"): "L-9002",
        ("AW-2002", "login_user_b"): "L-9003",  # 別顧客の同一 id_airwork
    })
    result = ai.process_applicant(_aw_row_no_login(), client)

    assert result.status == "unlinked"
    assert result.listing_id is None
    assert len(client.associations) == 0, "ambiguous 時に Association を張ってはいけない"
    assert "ambiguous: 2 listings matched" in result.message
    assert "login_id必須" in result.message
    # 未特定マーク (人間レビュー枠) は通常 unlinked と同様に入る
    assert result.appointment_properties.get("kokyakushiitotenkijoukyou") == "対象外"


def test_aw_without_login_id_zero_hit_unlinked():
    """AW login_id 無し: ヒット0 → unlinked。"""
    client = ai.DryRunClient(listings_aw={("AW-8888", "login_user_a"): "L-9002"})
    result = ai.process_applicant(_aw_row_no_login("AW-2002"), client)
    assert result.status == "unlinked"
    assert len(client.associations) == 0


def test_cli_login_id_fallback_uses_and_search():
    """--login-id (client_code) 指定時: CSV行の login_id 空でも AND 複合キー検索で linked。"""
    client = ai.DryRunClient(listings_aw={("AW-2002", "1658755"): "L-9002"})
    result = ai.process_applicant(_aw_row_no_login(), client, default_login_id="1658755")

    and_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    single_searches = [s for s in client.search_log if s["op"] == "search_listing_aw_by_id"]
    assert len(and_searches) == 1, "--login-id 指定時は複合キーAND検索を使う"
    assert and_searches[0]["key"] == {
        "id_airwork": "AW-2002", "airwork_account_login_id": "1658755"}
    assert len(single_searches) == 0, "--login-id 指定時に単独検索へフォールバックしない"

    assert result.status == "linked"
    assert result.listing_id == "L-9002"


def test_csv_login_id_takes_precedence_over_cli(aw_row):
    """CSV行に login_id がある場合は行側優先 (--login-id は上書きしない)。"""
    client = ai.DryRunClient(listings_aw={("AW-2002", "login_user_a"): "L-9002"})
    result = ai.process_applicant(aw_row, client, default_login_id="9999999")

    and_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    assert and_searches[0]["key"]["airwork_account_login_id"] == "login_user_a"
    assert result.status == "linked"


def test_aw_composite_miss_falls_back_to_id_airwork(aw_row):
    """複合キーがミスしても id_airwork が一意なら単独検索フォールバックで linked。

    実データ確定(2026-07-23): AW応募CSVは airwork_account_login_id 列が無く応募側login
    は bid(ログインID/メール)、LISTING側は client_code(数値)で表現がズレ複合キーが不一致。
    id_airwork は AirWork 全体で一意 → 単独+一意ガードで安全に紐付ける。
    """
    # LISTING の login は client_code '1584214'。応募側(aw_row)は 'login_user_a' で不一致。
    client = ai.DryRunClient(listings_aw={("AW-2002", "1584214"): "L-9002"})
    result = ai.process_applicant(aw_row, client)

    and_searches = [s for s in client.search_log if s["op"] == "search_listing_aw"]
    single_searches = [s for s in client.search_log if s["op"] == "search_listing_aw_by_id"]
    assert len(and_searches) == 1, "まず複合キーを試す"
    assert len(single_searches) == 1, "複合ミス→id_airwork単独へフォールバック"
    assert result.status == "linked"
    assert result.listing_id == "L-9002"
    assert client.associations[0]["type_id"] == 5


def test_split_jp_address():
    """AWの1フィールド住所を 都道府県/市区町村/以下住所 に3分割 (2026-07-27)。"""
    assert ai.split_jp_address("北海道札幌市豊平区豊平８条10丁目３－１－507") == \
        ("北海道", "札幌市", "豊平区豊平８条10丁目３－１－507")
    assert ai.split_jp_address("大阪府河内長野市松ケ丘中町1")[:2] == ("大阪府", "河内長野市")
    # 郡は郡ごと1単位
    assert ai.split_jp_address("北海道河東郡音更町木野大通1")[:2] == ("北海道", "河東郡音更町")
    # 都道府県なし → prefは空、市区町村は切り出す
    assert ai.split_jp_address("彦根市 竹ケ鼻町")[:2] == ("", "彦根市")
    assert ai.split_jp_address("") == ("", "", "")


def test_normalize_gender_and_age():
    assert ai.normalize_gender("男性") == "男"
    assert ai.normalize_gender("女性") == "女"
    assert ai.normalize_gender("回答しない") == ""   # 不明値は送信しない
    assert ai.compute_age("1990-07-28", "2026-07-27") == "35"  # 誕生日前日
    assert ai.compute_age("1990-07-27", "2026-07-27") == "36"  # 誕生日当日
    assert ai.compute_age("", "2026-07-27") == ""


def test_build_props_includes_attributes_and_owner(aw_row):
    """応募者属性/応募経路/応募開始/担当者がプロパティに載る (2026-07-27)。"""
    import dataclasses
    row = dataclasses.replace(
        aw_row, gender="男", birthdate="1990-01-02", age="36",
        postal="0600000", pref="北海道", city="札幌市", addr_rest="北1条1-1")
    props = ai.build_appointment_properties(row, linked=True, owner_id="999")
    assert props["seibetsu"] == "男"
    assert props["seinengappi"] == "1990-01-02"
    assert props["nenrei"] == "36"
    assert props["todoufuken"] == "北海道"
    assert props["shikuchouson"] == "札幌市"
    assert props["shikuchousonikajuusho"] == "北1条1-1"
    assert props["yingmujinglu"] == "job_board"
    assert props["hubspot_owner_id"] == "999"
    assert props["hs_appointment_start"].startswith(row.apply_date)


def test_duplicate_backfills_existing(aw_row):
    """dup時はスキップでなく既存レコードへ属性+担当者を補完する (2026-07-27)。"""
    import dataclasses
    row = dataclasses.replace(aw_row, gender="女", pref="東京都")
    client = ai.DryRunClient(
        listings_aw={("AW-2002", "login_user_a"): "L-9002"},
        existing_appts={("AirWork", row.apply_date, "email",
                         ai.normalize_email(row.email)): "APPT-1"},
    )
    client.listing_owners = {"L-9002": "777"}
    res = ai.process_applicant(row, client)
    assert res.status == "skip_duplicate"
    up = client.updated_appts[0]
    assert up["id"] == "APPT-1"
    assert up["properties"]["seibetsu"] == "女"
    assert up["properties"]["todoufuken"] == "東京都"
    assert up["properties"]["hubspot_owner_id"] == "777"


def test_duplicate_backfill_protects_existing_values(aw_row):
    """dup補完は空欄のみ埋める (2026-08-05 既存値保護に是正)。

    HR通知メール経路とAW xlsx経路で住所の分割精度が異なり、後着の雑な分割が
    良い値を上書きした実害(ダイリク2件)の再発防止。値が入っている項目は不変。
    """
    import dataclasses
    row = dataclasses.replace(aw_row, gender="女", pref="東京都",
                              city="千代田区丸の内1-1ビル405号")  # 雑な分割(後着)
    client = ai.DryRunClient(
        listings_aw={("AW-2002", "login_user_a"): "L-9002"},
        existing_appts={("AirWork", row.apply_date, "email",
                         ai.normalize_email(row.email)): "APPT-1"},
    )
    # 既存レコードには良い分割の市区町村と担当者が入っている
    client.existing_appt_props = {"APPT-1": {
        "shikuchouson": "千代田区", "hubspot_owner_id": "111"}}
    client.listing_owners = {"L-9002": "777"}
    res = ai.process_applicant(row, client)
    assert res.status == "skip_duplicate"
    up = client.updated_appts[0]["properties"]
    assert "shikuchouson" not in up          # 既存の良い値を潰さない
    assert "hubspot_owner_id" not in up      # 既存担当者も保護
    assert up["seibetsu"] == "女"            # 空欄は補完される
    assert up["todoufuken"] == "東京都"


def test_aw_composite_miss_ambiguous_stays_unlinked(aw_row):
    """複合ミス→id_airwork単独が複数ヒット時は §24(顧客横断誤紐付け禁止)で unlinked のまま。"""
    client = ai.DryRunClient(listings_aw={
        ("AW-2002", "1584214"): "L-9002",
        ("AW-2002", "9999999"): "L-9003",  # 同一id_airworkが別loginで複数(理論上の安全弁)
    })
    result = ai.process_applicant(aw_row, client)  # login_user_a で複合ミス→単独2件
    assert result.status == "unlinked"
    assert result.listing_id is None
    assert len(client.associations) == 0
    assert "ambiguous: 2 listings matched" in result.message


def test_hr_route_ignores_login_id(hr_row):
    """HR経路は --login-id を無視して従来通り id_hrhakkaa 検索のみ。"""
    client = ai.DryRunClient(listings_hr={"HR-1001": "L-9001"})
    result = ai.process_applicant(hr_row, client, default_login_id="1658755")

    hr_searches = [s for s in client.search_log if s["op"] == "search_listing_hr"]
    aw_ops = [s for s in client.search_log if s["op"].startswith("search_listing_aw")]
    assert len(hr_searches) == 1
    assert hr_searches[0]["key"] == {"id_hrhakkaa": "HR-1001"}
    assert len(aw_ops) == 0, "HR経路で AW 系検索が走ってはいけない"
    assert result.status == "linked"
    assert result.listing_id == "L-9001"


# ============================================================================
# CASE 3: 求人未特定時に類似度マッチング禁止 (v0.2 §24-1 §24-2)
# ============================================================================
def test_unlinked_does_not_invoke_similarity_match(rows):
    """求人未特定 (LISTING検索ヒットゼロ) でも、タイトル/本文類似度等で勝手に紐付けない。"""
    # 佐藤次郎 (HR-9999 = listings_hr に存在しない)
    sato = next(r for r in rows if "佐藤" in r.name)
    client = ai.DryRunClient(listings_hr={"HR-1001": "L-9001"})  # 9999は無い

    result = ai.process_applicant(sato, client)

    # 未特定でも APPOINTMENT は作成される (人間レビュー枠)
    assert result.status == "unlinked"
    assert result.appointment_id is not None
    # Association は張られない
    assert len(client.associations) == 0
    # 求人未特定マークが入る
    props = result.appointment_properties
    assert props.get("kokyakushiitotenkijoukyou") == "対象外"
    assert "求人未特定" in props.get("bikou_hiaringu", "")


def test_no_similarity_match_function_exists():
    """v0.2 §24-1 §24-2 ガード: 類似度マッチング関数が実装に存在しないこと。

    タイトル/本文類似度マッチングを将来うっかり追加してしまわないよう、
    関数名レベルで検出する。
    """
    src = inspect.getsource(ai)
    forbidden = ["title_similarity", "fuzzy_match", "name_similarity",
                 "similarity_match", "match_by_title", "match_by_name"]
    found = [n for n in forbidden if n in src]
    assert not found, f"§24ガード違反候補の関数が混入: {found}"


# ============================================================================
# CASE 4: 重複応募検出 (2026-07-03 dedup修正: denwaseikika廃止 / email優先 / yingmuri照合)
# ============================================================================
def test_duplicate_detection_skips_creation(hr_row):
    """同一(media, 応募日, メール) で既存 APPOINTMENT があれば skip。"""
    dup_key = (hr_row.media, hr_row.apply_date, "email", hr_row.email)
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={dup_key: "APPT-EXIST-7777"},
    )

    result = ai.process_applicant(hr_row, client)

    assert result.status == "skip_duplicate"
    assert result.appointment_id == "APPT-EXIST-7777"
    assert len(client.created_appts) == 0, "重複時は新規 APPOINTMENT を作らない"
    assert len(client.associations) == 0, "重複時は Association も張らない"


def _dedup_row(phone: str = "09011112222", email: str = "dup@example.com",
               apply_date: str = "2026-06-20") -> "ai.ApplicantRow":
    return ai.ApplicantRow(
        name="重複 検証", kana="", phone=phone, email=email, apply_date=apply_date,
        media="HRハッカー", media_job_id="HR-1001", raw_lineno=50,
    )


def test_dedup_email_takes_priority_over_phone():
    """email 非空時は email 照合のみ。phone一致キーが存在しても email 不一致なら作成に進む。"""
    row = _dedup_row(email="new-person@example.com")
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={
            # phone は一致するが、email があるので phone フォールバックは使われない
            ("HRハッカー", "2026-06-20", "phone", "090-1111-2222"): "APPT-PHONE-1",
            # email キーは別人
            ("HRハッカー", "2026-06-20", "email", "other@example.com"): "APPT-EMAIL-1",
        },
    )
    result = ai.process_applicant(row, client)
    assert result.status == "linked", "email不一致なら phone一致でも dedup しない"
    assert len(client.created_appts) == 1

    # email 一致キーがあれば skip
    client2 = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={
            ("HRハッカー", "2026-06-20", "email", "new-person@example.com"): "APPT-EMAIL-2",
        },
    )
    result2 = ai.process_applicant(row, client2)
    assert result2.status == "skip_duplicate"
    assert result2.appointment_id == "APPT-EMAIL-2"


def test_dedup_phone_fallback_when_email_empty():
    """email 空なら denwabangou (format_jp_phone 整形形式) でフォールバック照合。"""
    row = _dedup_row(email="")
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={
            # 格納値は format_jp_phone 整形済(+81 E.164) → その形式で照合される
            ("HRハッカー", "2026-06-20", "phone", "+819011112222"): "APPT-PHONE-2",
        },
    )
    result = ai.process_applicant(row, client)
    assert result.status == "skip_duplicate"
    assert result.appointment_id == "APPT-PHONE-2"
    assert len(client.created_appts) == 0


def test_dedup_impossible_when_both_phone_and_email_empty():
    """email も phone も空 → dedup 不能。既存が何であれ照合せず作成に進む。"""
    row = _dedup_row(phone="", email="")
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={
            ("HRハッカー", "2026-06-20", "email", ""): "APPT-EMPTY-1",
            ("HRハッカー", "2026-06-20", "phone", ""): "APPT-EMPTY-2",
        },
    )
    result = ai.process_applicant(row, client)
    assert result.status == "linked", "識別子ゼロ行は dedup せず作成する"
    assert len(client.created_appts) == 1


def test_dedup_different_apply_date_not_deduped():
    """同一人物でも応募日 (yingmuri) が違えば別応募 → dedup しない。"""
    row = _dedup_row(apply_date="2026-06-25")
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},
        existing_appts={
            # 同一 media/email だが応募日が別
            ("HRハッカー", "2026-06-20", "email", "dup@example.com"): "APPT-OLD-DATE",
        },
    )
    result = ai.process_applicant(row, client)
    assert result.status == "linked", "応募日違いは別応募として作成する"
    assert len(client.created_appts) == 1


# ============================================================================
# 補助テスト: CSV読込 + 正規化 + summary
# ============================================================================
def test_csv_load_normalizes(rows):
    assert len(rows) == 5
    yamada = rows[0]
    assert yamada.media == "HRハッカー"
    assert yamada.phone == "09011112222"  # ハイフン除去
    assert yamada.email == "yamada@example.com"
    assert yamada.apply_date == "2026-06-20"


# ============================================================================
# 実媒体CSV (生ヘッダ) → 汎用スキーマ マッピング層
# ============================================================================
HR_RAW_HEADER = (
    "応募者id,応募求人先,選考ステータス,応募経路,名前,名前フリガナ,性別,生年月日,"
    "現在の職業,電話番号,メールアドレス,郵便番号,都道府県,市区町村,丁目,番地,建物名,"
    "連絡希望日,連絡方法,見学会希望有無,見学会希望日,希望面接形態,学歴,勤務可能期間,"
    "自由項目1,自由項目2,自由項目3,面接予定日,選考理由,応募日時,更新日,店舗ID,店舗名"
)


def _hr_raw_line(i: int) -> str:
    return (
        f"A{i},1165324{i},選考中,Web,応募 太郎{i},オウボ タロウ{i},男性,1990-01-01,"
        f"会社員,090-1111-222{i},hr{i}@example.com,000-0000,東京都,新宿区,1,2,,"
        f",,無,,対面,大卒,即日,,,,,,2026-07-0{(i % 7) + 1} 09:06:05,2026-07-03,144653{i},店舗{i}"
    )


def _write_hr_raw_csv(tmp_path, n: int = 5):
    p = tmp_path / "hr_raw.csv"
    body = "\n".join([HR_RAW_HEADER] + [_hr_raw_line(i) for i in range(1, n + 1)]) + "\n"
    p.write_bytes(body.encode("cp932"))
    return p


AW_RAW_HEADER_COLS = [
    "応募ID", "応募者名", "ふりがな", "生年月日", "年齢", "郵便番号", "住所",
    "電話番号", "メールアドレス", "性別", "応募経路", "応募日時", "応募求人ID",
    "希望勤務地", "対応状況",
]


def _write_aw_raw_csv(tmp_path, n: int = 5):
    """AW実CSV同様: UTF-8 BOM + 全セル二重引用符。"""
    import csv as _csv

    p = tmp_path / "aw_raw.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f, quoting=_csv.QUOTE_ALL)
        w.writerow(AW_RAW_HEADER_COLS)
        for i in range(1, n + 1):
            w.writerow([
                f"AWID{i}", f"応募 花子{i}", f"おうぼ はなこ{i}", "1995-05-05", "31",
                "111-1111", "北海道札幌市", f"080-3333-444{i}", f"aw{i}@example.com",
                "女性", "AirWORK", f"2026/06/1{i % 10} 11:07:06", f"1042711{i}",
                "札幌", "未対応",
            ])
    return p


def test_detect_media_from_header_pure():
    assert ai.detect_media_from_header(["応募者id", "応募求人先", "名前"]) == "HRハッカー"
    assert ai.detect_media_from_header(["応募ID", "応募求人ID", "応募者名"]) == "AirWORK"
    # 汎用ヘッダ / 未知ヘッダ → None (従来処理継続)
    assert ai.detect_media_from_header(ai.REQUIRED_CSV_COLS) is None
    assert ai.detect_media_from_header(["foo", "bar"]) is None
    # 片方だけでは判定しない
    assert ai.detect_media_from_header(["応募者id", "名前"]) is None
    assert ai.detect_media_from_header(["応募ID", "応募者名"]) is None


def test_hr_raw_header_csv_normalized(tmp_path):
    """HR生ヘッダ (cp932) → 汎用行に正規化。媒体求人IDは応募求人先由来。"""
    rows = ai.load_applicants_csv(_write_hr_raw_csv(tmp_path, 5))
    assert len(rows) == 5
    r = rows[0]
    assert r.media == "HRハッカー"
    assert r.media_job_id == "11653241", "媒体求人IDは応募求人先由来であること"
    assert r.name == "応募 太郎1"
    assert r.kana == "オウボ タロウ1"
    assert r.phone == "09011112221"  # ハイフン除去
    assert r.email == "hr1@example.com"
    assert r.apply_date == "2026-07-02"  # 応募日時 (時刻付き) → YYYY-MM-DD
    assert r.airwork_login_id == ""


def test_hr_raw_rows_process_with_id_hrhakkaa(tmp_path):
    """HR生ヘッダ由来の行が id_hrhakkaa 検索で linked になる (後段パイプライン無改変)。"""
    rows = ai.load_applicants_csv(_write_hr_raw_csv(tmp_path, 1))
    client = ai.DryRunClient(listings_hr={"11653241": "L-1"})
    result = ai.process_applicant(rows[0], client)
    assert result.status == "linked"
    assert client.search_log[0]["key"] == {"id_hrhakkaa": "11653241"}


def test_aw_raw_header_csv_normalized(tmp_path):
    """AW生ヘッダ (utf-8 BOM + 引用符付き) → 媒体求人IDは応募求人ID由来。"""
    rows = ai.load_applicants_csv(_write_aw_raw_csv(tmp_path, 5))
    assert len(rows) == 5
    r = rows[0]
    assert r.media == "AirWork", "媒体名固定値 AirWORK が enum 正規値 AirWork に正規化されること"
    assert r.media_job_id == "10427111", "媒体求人IDは応募求人ID由来であること"
    assert r.name == "応募 花子1"
    assert r.kana == "おうぼ はなこ1"
    assert r.phone == "08033334441"
    assert r.email == "aw1@example.com"
    assert r.apply_date == "2026-06-11"  # 応募日時 (スラッシュ+時刻付き) → YYYY-MM-DD
    assert r.airwork_login_id == ""  # AW生CSVに login_id 列なし → --login-id フォールバック前提


def test_aw_raw_rows_use_login_id_fallback(tmp_path):
    """AW生ヘッダ由来行 + default_login_id で複合キー検索が使われる。"""
    rows = ai.load_applicants_csv(_write_aw_raw_csv(tmp_path, 1))
    client = ai.DryRunClient(listings_aw={("10427111", "1658755"): "L-2"})
    result = ai.process_applicant(rows[0], client, default_login_id="1658755")
    assert result.status == "linked"
    assert client.search_log[0]["key"] == {
        "id_airwork": "10427111", "airwork_account_login_id": "1658755"}


def test_generic_header_unchanged(rows):
    """汎用ヘッダは従来通り読める (マッピング層追加後の回帰確認)。"""
    assert len(rows) == 5
    assert {r.media for r in rows} >= {"HRハッカー", "AirWork"}


def test_unknown_header_still_raises_value_error(tmp_path):
    """HR/AW/汎用のどれでもないヘッダ → 従来の ValueError。"""
    p = tmp_path / "unknown.csv"
    p.write_text("氏名,連絡先\nX,Y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必須列が欠落"):
        ai.load_applicants_csv(p)


def test_run_import_aggregates_results(rows):
    client = ai.DryRunClient(
        listings_hr={"HR-1001": "L-9001"},  # 山田 OK / 佐藤 NG
        listings_aw={("AW-2002", "login_user_a"): "L-9002"},  # 鈴木 + 田中 OK
        # Indeed (高橋) は媒体ロジック未確立 → unlinked
    )
    results = ai.run_import(rows, client)
    s = ai.summarize(results)
    assert s["total"] == 5
    assert s["linked"] == 3  # 山田/鈴木/田中
    assert s["unlinked"] == 2  # 佐藤/高橋
    assert s["skip_duplicate"] == 0
    assert s["error"] == 0
