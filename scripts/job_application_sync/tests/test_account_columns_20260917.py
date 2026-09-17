# -*- coding: utf-8 -*-
"""顧客管理シートの列解決が現場のヘッダ編集に耐えるか (2026-09-17)。

## なぜ要るか

このシートのヘッダは現場が編集する。ヘッダ名の完全一致で列を引いていたため、
**同じ型の本番停止が2回起きている**:

  2026-08-24  C列に「企業名」が付く          → 会社名列が候補から外れ全滅
  2026-09-17  「AirWorkID」に注記が付く      → aid が引けず19連続失敗
              (「AirWorkID（旧式の為入力しない！！）」)

2回目は応募取込が1時間25分止まった。落ちた場所は突合より前なので、
**応募は1件も処理されなかった**。

## ここで守ること

1. 括弧の注記が付いても引ける (注記は運用の都合で増減する)
2. 前方一致で拾うとき、**似た名前の別列を横取りしない**
   (「AirWorkID」と「企業AirWorkID」は別の列)
3. 補助キー1本が引けないだけで**基幹処理を止めない**。ただし黙らない
4. 突合に要る列が引けないときは**従来どおり止める**
   (位置依存へ黙って落とすより、止めて人に見せるほうが安全)
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync.applicant_queue import (
    _A_OPTIONAL, _resolve_account_columns)

# 2026-09-17 の本番シートの実ヘッダ (実ログから)
REAL_HEADER = [
    "", "企業番号", "権限共有", "企業名", "HS名", "担当コンサル",
    "engageアカウント", "クローズ", "リクロジアドレス", "PW",
    "AirWorkID（旧式の為入力しない！！）", "AirワークPW",
    "企業AirWorkID", "企業AirworkPW", "エイリアスアドレス",
    "エイリアスパス", "engageID",
]
ROW = ["", "1", "FALSE", "株式会社サンプル", "", "", "", "", "a@x.jp", "",
       "AW001", "", "B001", "", "al@x.jp", "", ""]


def test_注記付きヘッダでも引ける():
    """★2026-09-17 の停止そのもの。これが通らないと本番が5分毎に落ちる."""
    c = _resolve_account_columns(REAL_HEADER, [ROW])
    assert c["aid"] == 10, "「AirWorkID（旧式の為入力しない！！）」を引けること"


def test_似た名前の別列を横取りしない():
    """★前方一致を入れた副作用の確認。aid と bid は別の列."""
    c = _resolve_account_columns(REAL_HEADER, [ROW])
    assert c["bid"] == 12, "企業AirWorkID は別列"
    assert c["aid"] != c["bid"]


def test_実ヘッダで必須列が全て引ける():
    c = _resolve_account_columns(REAL_HEADER, [ROW])
    assert c["closed"] == 7 and c["reclog"] == 8
    assert c["bpw"] == 13 and c["alias"] == 14
    assert c["comp"] == 3, "会社名はヘッダ名で引ける (2026-08-24 の対処)"


def test_注記が半角括弧でも引ける():
    h = list(REAL_HEADER)
    h[10] = "AirWorkID(旧式)"
    assert _resolve_account_columns(h, [ROW])["aid"] == 10


def test_補助キーが無くても止まらない(capsys):
    """★aid は突合の第4優先。1本欠けただけで基幹処理を止めるのは釣り合わない。
    ただし黙って捨てず、どの列が引けないかを出す."""
    h = [x for i, x in enumerate(REAL_HEADER) if i != 10]
    r = [x for i, x in enumerate(ROW) if i != 10]
    c = _resolve_account_columns(h, [r])
    assert c["aid"] is None
    assert "aid" in capsys.readouterr().out, "引けなかったことを黙らない"


def test_突合に要る列が無ければ止める():
    """リクロジアドレスは突合の第1優先。これが無いのは設定事故なので止める."""
    h = [x for i, x in enumerate(REAL_HEADER) if i != 8]
    r = [x for i, x in enumerate(ROW) if i != 8]
    with pytest.raises(RuntimeError, match="reclog"):
        _resolve_account_columns(h, [r])


def test_任意扱いはaidだけ():
    """★安易に増やさない。増やすほど「動いているのに突合できない」が増える."""
    assert _A_OPTIONAL == frozenset({"aid"})


def test_注記が先頭に付いても引ける():
    """★逆証明A: 注記除去と前方一致は二重防御で、片方ずつ壊すと素通りしていた。
    注記が**先頭**だと前方一致では拾えない。注記除去が効いていることを見る."""
    h = list(REAL_HEADER)
    h[10] = "（旧式）AirWorkID"
    assert _resolve_account_columns(h, [ROW])["aid"] == 10


def test_前方一致だけでも引ける():
    """★逆証明B: 注記除去を殺しても前方一致で拾えること."""
    h = list(REAL_HEADER)
    h[10] = "AirWorkIDは旧式なので入力しない"   # 括弧が無いので除去では拾えない
    assert _resolve_account_columns(h, [ROW])["aid"] == 10


def test_補助列が無い状態でbuildが落ちない(monkeypatch):
    """★逆証明F: index=None を添字に渡すと TypeError。
    「任意列にした」だけでは足りず、読む側にガードが要る。
    列解決だけのテストでは捕まらないので build() を通す."""
    import scripts.job_application_sync.applicant_queue as aq

    h = [x for i, x in enumerate(REAL_HEADER) if i != 10]
    r = [x for i, x in enumerate(ROW) if i != 10]

    class _WS:
        def get_all_values(self):
            return [h, r]

    class _Book:
        sheet1 = _WS()

    class _GC:
        def open_by_key(self, _k):
            return _Book()

    monkeypatch.setattr(aq, "_sheets_client", lambda *a, **k: _GC())
    monkeypatch.setattr(aq.al, "sheet_retry", lambda fn, *a, **k: fn())
    res = aq.AccountResolver().build()          # ここで落ちないこと
    assert res.cols["aid"] is None
    assert res.idx_reclog, "他のキーでの索引は作られている"
