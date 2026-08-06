# -*- coding: utf-8 -*-
"""clean_value / norm_key の回帰テスト (2026-08-06)。

なぜ書くか:
  この関数は「表記ゆれを吸収する」つもりで書かれたが、実際には**値を破壊**していた。
    - 汎用の重複除去 (.{1,4}?)\\1 が "55"(年齢の足切り) を "5" に潰した
    - HTMLの実体参照を解いておらず "&nbsp;対面" と "対面" が別値になった
  どちらも「変換したつもり」が「壊した」に化けたもので、実データに当てるまで
  誰も気づけなかった。以後は変えるたびにここで固定する。

実行: python -m pytest tests/test_rollup_clean_value.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.job_application_sync.rollup_memo_to_deal import (  # noqa: E402
    clean_value, extract_fields, norm_key, strip_html)


# --- 壊してはいけないもの (実データ由来) ------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("55", "55"),            # 年齢の足切り。旧実装は "5" に潰していた
    ("44", "44"),
    ("2020", "2020"),        # 西暦。旧実装は "20"
    ("1010", "1010"),
    ("３０", "30"),           # 全角→半角(NFKC)はしてよい
    ("~60歳", "~60歳"),
    ("50歳以上は足切り", "50歳以上は足切り"),
    ("大型自動車免許", "大型自動車免許"),
    ("あああ", "あああ"),      # 奇数回の繰り返しは触らない
])
def test_does_not_destroy_values(raw, want):
    assert clean_value(raw) == want


# --- 畳んでよいもの ---------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("なしなし", "なし"),
    ("無し無し", "無し"),
    ("特になし特になし", "特になし"),
    ("なし", "なし"),
])
def test_folds_known_duplicates(raw, want):
    assert clean_value(raw) == want


# --- ノイズ落とし -----------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    (":なし", "なし"),                       # 先頭コロン
    ("：対面", "対面"),
    ("&nbsp;対面", "対面"),                  # 実体参照 → NBSP → 半角空白
    ("&nbsp;顧客", "顧客"),
    ("対面 /", "対面"),                      # 選択肢を消し残した末尾
    ("履歴書／職務経歴書", "履歴書 / 職務経歴書"),
    ("履歴書 / 職務経歴書", "履歴書 / 職務経歴書"),
    ("  対面  ", "対面"),
    ("", ""),
    (None, ""),
])
def test_strips_noise(raw, want):
    assert clean_value(raw) == want


def test_nbsp_variants_share_a_key():
    """&nbsp; の有無で同値判定が割れないこと (実測135件で割れていた)。"""
    assert norm_key("&nbsp;対面") == norm_key("対面")
    assert norm_key("履歴書／職務経歴書") == norm_key("履歴書 / 職務経歴書")
    assert norm_key("~50") == norm_key("〜50")


def test_distinct_values_stay_distinct():
    """畳みすぎないこと。違う条件が同じキーになると条件が消える。"""
    assert norm_key("55") != norm_key("5")
    assert norm_key("~60歳") != norm_key("~55歳")
    assert norm_key("大型自動車免許") != norm_key("普通自動車第一種免許")


def test_strip_html_unescapes():
    assert "対面" in strip_html("<p>提出形式:&nbsp;対面</p>")
    assert "\n" in strip_html("A<br>B")


def test_extract_fields_reads_real_template():
    """現場が使っている入力テンプレートの形をそのまま読めること。"""
    body = strip_html(
        "📋 入力テンプレート（求人ごと）<br>"
        "■ 足切り基準（顧客非開示）<br>"
        "年齢:55<br>経験年数:<br>必須資格:<br>学歴NG:<br>"
        "前職業界NG:<br>前職企業NG:<br>その他:<br>"
        "■ 書類回収ルール<br>必要書類:履歴書 / 職務経歴書<br>"
        "回収タイミング:面接時持参<br>提出形式:&nbsp;対面<br>確認担当:顧客")
    f = extract_fields(body)
    assert f["年齢"] == "55", f"年齢が壊れた: {f.get('年齢')!r}"
    assert f["必要書類"] == "履歴書 / 職務経歴書"
    assert f["回収タイミング"] == "面接時持参"
    assert f["提出形式"] == "対面"
    assert f["確認担当"] == "顧客"
    # 空欄は「記入なし」であって値ではない
    assert "経験年数" not in f
    assert "必須資格" not in f


# --- 実物のHTML構造 ---------------------------------------------------------
REAL_HTML = (
    '<div dir="auto"><h2>📋 入力テンプレート（求人ごと）</h2><hr>'
    '<h3>■ 足切り基準（顧客非開示）</h3><ul>'
    '<li><p style="margin:0;"><strong>年齢</strong>:55歳まで</p></li>'
    '<li><p style="margin:0;"><strong>その他</strong>:なし</p></li></ul>'
    '<h3>■ 一次対応ヒアリング項目</h3><ul>'
    '<li><p style="margin:0;">① 保有資格:フォークリフト</p></li></ul>'
    '<h3>■ 書類回収ルール</h3><ul>'
    '<li><p style="margin:0;">必要書類: 履歴書 / 職務経歴書</p></li></ul>'
    '<h3>■ 優先順位ルール</h3><ul>'
    '<li><p style="margin:0;">採用優先順位: &nbsp; 中</p></li>'
    '<li><p style="margin:0;">急ぎ度: &nbsp;長期</p></li></ul>'
    '<h3>■ 顧客が重視するポイント（書類選考の評価軸）</h3>'
    '<p>転職回数、在籍回数、職種の一貫性</p>'
    '<h3>■ コンサル所感（社内向け詳細）</h3><p>なし</p></div>')


def test_strip_html_breaks_block_elements():
    """見出しと項目が1行に潰れないこと (潰れると項目名の照合が壊れる)。"""
    t = strip_html(REAL_HTML)
    for ln in t.split("\n"):
        s = ln.strip()
        assert not (s.startswith("■") and "年齢" in s), \
            f"見出しと項目が同じ行に潰れている: {s!r}"


def test_extract_covers_whole_template():
    """記入率99%の「急ぎ度」「採用優先順位」を含め、全ブロックを拾うこと。

    当初の11項目では、この2つが丸ごと落ちていた (2026-08-06 実測で発覚)。
    """
    f = extract_fields(strip_html(REAL_HTML))
    assert f["年齢"] == "55歳まで"
    assert f["保有資格"] == "フォークリフト"
    assert f["必要書類"] == "履歴書 / 職務経歴書"
    assert f["採用優先順位"] == "中", "採用優先順位が落ちている"
    assert f["急ぎ度"] == "長期", "急ぎ度が落ちている"
    # コロンを使わず見出し直後に本文が来る形式
    assert f["顧客が重視するポイント"] == "転職回数、在籍回数、職種の一貫性"


@pytest.mark.parametrize("v,want", [
    ("なし", True), ("特になし", True), ("→特になし", True),
    ("無し", True), ("-", True),
    ("55歳まで", False), ("中", False), ("フォークリフト", False),
])
def test_is_empty_value(v, want):
    from scripts.job_application_sync.rollup_memo_to_deal import is_empty_value
    assert is_empty_value(v) is want
