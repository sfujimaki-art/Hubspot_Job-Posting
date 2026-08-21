"""古い応募に一次対応の要否を付けないガードの回帰テスト (2026-08-07)。

## なぜ必要か

AWの応募CSVは「その顧客の全応募(最大1000件)」を返す。したがって1社の取得に
成功するたび、過去の応募がまとめて取り込まれる。

実測 (2026-08-07):
  日本製紙リキッドパッケージ 1社で 32件、コダマ運輸 1社で 13件が同時に流入。
  直近3時間の新規登録97件のうち **70件(72%) が応募日8/01より前の過去分**。

一方で「一次対応の要否」は取引の itijitaiou (true/false) をそのまま引き継ぐ
だけで、応募日を一切見ていなかった。itijitaiou=true の顧客を遡って取り込むと、
何ヶ月も前の応募が全部「必要」になり、BPOの未対応キューに一気に積み上がる。
現場は捌けないし、応募から日が経ちすぎていて架電しても意味がない
(現場の基準は「応募発生から30分以内に架電」)。

さらに、セッション不具合で失敗した203件が4〜24時間かけて順次再試行される。
1社成功するたびに同じ流入が起きるため、その前に止める必要があった。

## 方針 (ユーザー選択: 案B / 2026-08-07)

**応募日が N日以内のものだけ要否を付ける。それ以外は登録するが要否を付けない。**
N は JAS_ICHIJITAIOU_MAX_AGE_DAYS (既定7)。

- レコードは必ず作る = 記録は残る (取りこぼしではない)
- 要否が無い = 未対応キューに入らない
- 新しい応募は従来どおり要否が付く = 日常運用は変わらない

案A(通知と応募日が一致するものだけ)は却下。シート1の通知が万全とは限らず、
通知漏れの応募が二度と対応されなくなるため。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import applicant_import as ai


TODAY = "2026-08-07"


def _props(v: str = "必要") -> dict:
    return {"ouboshashimei": "山田 太郎", "ichijitaiounoumu": v}


# --------------------------------------------------------------------------
# 新しい応募 = 従来どおり要否が残る
# --------------------------------------------------------------------------
@pytest.mark.parametrize("apply_date", [
    "2026-08-07",   # 当日
    "2026-08-06",
    "2026-08-01",   # 6日前
    "2026-07-31",   # 7日前 = 境界(含む)
])
def test_直近の応募は要否を残す(apply_date):
    p = _props()
    assert ai.strip_ichijitaiou_if_stale(p, apply_date, today=TODAY) is False
    assert p["ichijitaiounoumu"] == "必要"


# --------------------------------------------------------------------------
# 古い応募 = 要否を落とす (レコードは残る)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("apply_date", [
    "2026-07-30",   # 8日前 = 境界の外
    "2026-07-01",
    "2026-02-24",   # 実際に流入した過去分の日付
])
def test_古い応募は要否を落とす(apply_date):
    p = _props()
    assert ai.strip_ichijitaiou_if_stale(p, apply_date, today=TODAY) is True
    assert "ichijitaiounoumu" not in p
    assert p["ouboshashimei"] == "山田 太郎", "他のプロパティは壊さない"


def test_不要でも古ければ落とす():
    """「不要」も落とす。付いていること自体が現場の判断対象になるため。"""
    p = _props("不要")
    assert ai.strip_ichijitaiou_if_stale(p, "2026-05-01", today=TODAY) is True
    assert "ichijitaiounoumu" not in p


# --------------------------------------------------------------------------
# 安全側: 判定できないときは落とさない
# --------------------------------------------------------------------------
@pytest.mark.parametrize("apply_date", ["", "2026/08/01", "不明", "20260801"])
def test_応募日が読めなければ落とさない(apply_date):
    """日付が読めないことを理由に対応対象から外さない(取りこぼし防止)。"""
    p = _props()
    assert ai.strip_ichijitaiou_if_stale(p, apply_date, today=TODAY) is False
    assert p["ichijitaiounoumu"] == "必要"


def test_要否が無いレコードは何もしない():
    p = {"ouboshashimei": "山田 太郎"}
    assert ai.strip_ichijitaiou_if_stale(p, "2026-01-01", today=TODAY) is False
    assert p == {"ouboshashimei": "山田 太郎"}


def test_未来日付は落とさない():
    """時差やCSVの表記で未来日になっても、対応対象から外さない。"""
    p = _props()
    assert ai.strip_ichijitaiou_if_stale(p, "2026-08-09", today=TODAY) is False
    assert p["ichijitaiounoumu"] == "必要"


# --------------------------------------------------------------------------
# 閾値は環境変数で変えられる
# --------------------------------------------------------------------------
def test_閾値を環境変数で変更できる(monkeypatch):
    monkeypatch.setattr(ai, "ICHIJITAIOU_MAX_AGE_DAYS", 30)
    p = _props()
    assert ai.strip_ichijitaiou_if_stale(p, "2026-07-15", today=TODAY) is False
    assert p["ichijitaiounoumu"] == "必要"
    p2 = _props()
    assert ai.strip_ichijitaiou_if_stale(p2, "2026-06-01", today=TODAY) is True


def test_既定値は7日():
    assert ai.ICHIJITAIOU_MAX_AGE_DAYS == 7
