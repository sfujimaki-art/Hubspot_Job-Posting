# -*- coding: utf-8 -*-
"""customer_sheet_sync の回帰テスト (ネットワーク不要の純関数+ガード)。

実施済みの手動検証(機能E2E 16項目/逆証明10項目/サニタイズ4000件/逆流8項目/
親実証3項目)のうち、ネットワーク無しで再現できる部分を恒久資産化する。
実行: python -m pytest tests/test_customer_sheet_sync.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.job_application_sync import customer_sheet_sync as css  # noqa: E402


# ── 電話番号 (顧客が発信する番号: 0保持・国内形式) ──────────────────────
@pytest.mark.parametrize("src,exp", [
    ("+819093666454", "09093666454"),
    ("+818012345678", "08012345678"),
    ("09093396829", "09093396829"),      # 既に国内形式なら不変
    ("", ""),
    (None, ""),
])
def test_to_domestic_phone(src, exp):
    assert css.to_domestic_phone(src) == exp


# ── 郵便番号 (〒NNN-NNNN、不正桁は原文のまま=勝手に補正しない) ─────────
@pytest.mark.parametrize("src,exp", [
    ("3210904", "〒321-0904"),
    ("901-1414", "〒901-1414"),
    ("300123", "300123"),                # 6桁(媒体データ不備)は原文
    ("", ""),
])
def test_format_postal(src, exp):
    assert css.format_postal(src) == exp


# ── サニタイズ (媒体由来の汚れ: 実データ4,000件走査で確認したパターン) ──
def test_clean_text_removes_ctrl_newline_multispace():
    assert css.clean_text("改行\nを含む 名前") == "改行 を含む 名前"
    assert css.clean_text("  前後空白  ") == "前後空白"
    assert css.clean_text("高梨　 功") == "高梨 功"
    assert css.clean_text("a\x00b\x1fc") == "abc"


def test_clean_text_name_keeps_original_width():
    # 氏名は原文尊重: 半角カナを勝手に全角化しない
    assert css.clean_text("ﾇｴ ﾆ ﾄﾝ") == "ﾇｴ ﾆ ﾄﾝ"


@pytest.mark.parametrize("src,exp", [
    ("ビレッジハウス本町1号棟 ２０３号屋", "ビレッジハウス本町1号棟 203号屋"),
    ("1ｰ14ｰ6", "1-14-6"),                 # 半角長音符→番地ハイフン
    ("姶良市西餅田2312ｰ30シャーメゾン古田101号",
     "姶良市西餅田2312-30シャーメゾン古田101号"),  # 建物名の長音符は保持
    ("中区金山５丁目１２－22  エステムコート602号　（国：日本）",
     "中区金山5丁目12-22 エステムコート602号"),    # 全角数字+国名ノイズ
    ("シャーメゾン", "シャーメゾン"),
    ("東京都港区台場1-1-2-2707", "東京都港区台場1-1-2-2707"),
])
def test_clean_address(src, exp):
    assert css.clean_address(src) == exp


# ── build_row (行の整形) ────────────────────────────────────────
def _props(**kw):
    base = {"hs_object_id": "574458985235", "yingmuri": "2026-08-03",
            "oubobaitaimei": "AirWork"}
    base.update(kw)
    return base


def test_build_row_media_not_transferred():
    # 応募媒体は転記しない (2026-08-03 ユーザー指定)。列自体を持たない
    r = css.build_row(_props(oubobaitaimei="AirWork"))
    assert "応募発生媒体" not in r and "媒体" not in r
    assert "AirWork" not in r.values()


def test_build_row_no_column_left_for_customer():
    # No列は顧客採番なので書かない
    assert "No" not in css.build_row(_props())


def test_build_row_kyoten_and_gender():
    r = css.build_row(_props(oubosaki_kinmuchi="広島県呉市", seibetsu="女性"))
    assert r["応募拠点"] == "広島県呉市" and r["性別"] == "女性"


def test_build_row_address_joins_with_postal():
    r = css.build_row(_props(yuubinbangou="1000001", todoufuken="東京都",
                             shikuchouson="千代田区",
                             shikuchousonikajuusho="千代田1-1"))
    assert r["住所"] == "〒100-0001 東京都 千代田区 千代田1-1"


def test_build_row_missing_values_stay_blank():
    r = css.build_row(_props())
    assert r["電話番号"] == "" and r["住所"] == "" and r["氏名"] == ""


def test_management_columns_at_right_edge():
    # G5: 管理列をL列に置くと既存タブの「状況」列(L〜S)と衝突するためT/U固定
    assert css.COLUMNS.index(css.KEY_COL) == 19        # T列
    assert css.COLUMNS.index(css.SYNCED_COL) == 20     # U列


def test_age_written_as_number():
    # S6: 文字列だと顧客のソートで "10" < "9" になる
    assert css.build_row(_props(nenrei="22"))["年齢"] == 22
    assert isinstance(css.build_row(_props(nenrei="22"))["年齢"], int)
    assert css.build_row(_props())["年齢"] == ""       # 欠損は空文字


def test_a1_helper_quotes_tab_name():
    # S4/G14: タブ名は常にシングルクォートで包む
    assert css._a1("A1").startswith("'") and "'!" in css._a1("A1")


def test_build_row_synced_at_is_jst():
    # GitHub Actions(UTC)でも顧客に見せる時刻はJST
    from datetime import datetime
    r = css.build_row(_props())
    jst_now = datetime.now(css._JST).strftime("%Y-%m-%d %H:")
    assert r[css.SYNCED_COL].startswith(jst_now[:11])


# ── ガード (逆証明で発見した弱点の再発防止) ─────────────────────────
def test_cutoff_empty_rejected():
    # A4: cutoff空は全件遡及事故 → 必須
    with pytest.raises(ValueError):
        css.fetch_applicants("X" * 44, "")


def test_cutoff_offset_format_rejected():
    # C1: +09:00等は文字列比較が壊れる → UTC(Z)のみ
    with pytest.raises(ValueError):
        css.fetch_applicants("X" * 44, "2026-08-03T00:00:00+09:00")


def test_short_sheet_id_rejected():
    # A5: 短いIDはCONTAINS_TOKENで別顧客を拾う ('docs'で9,230件ヒット実測)
    with pytest.raises(ValueError):
        css.fetch_applicants("docs", "2026-08-03T00:00:00Z")


def test_allowlist_empty_means_nothing_allowed(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SHEET_ALLOW", "")
    assert css.allowed_sheets() == set()


def test_allowlist_parses_commas_and_spaces(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SHEET_ALLOW", " id-A , id-B  id-C ")
    assert css.allowed_sheets() == {"id-A", "id-B", "id-C"}


def test_sync_rejects_unallowed_sheet(monkeypatch):
    # A1: 許可リスト外への書込は完全一致で拒否
    monkeypatch.setenv("CUSTOMER_SHEET_ALLOW", "Y" * 44)
    with pytest.raises(PermissionError):
        css.sync("X" * 44, "2026-08-03T00:00:00Z", dry_run=False)
