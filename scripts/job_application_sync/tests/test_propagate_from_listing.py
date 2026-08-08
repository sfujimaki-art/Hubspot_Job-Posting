"""求人→応募 の継承項目を後から埋めるスイープの回帰テスト (2026-08-08)。

## なぜ要るか

継承（一次対応の要否・担当者）は**応募の作成時にしか入らない**。応募が先に登録され、
あとから求人(LISTING)側に値が入っても、応募へは伝播しない。
`relink()` は `kokyakushiitotenkijoukyou=="対象外"` の応募しか拾わないため、
既に求人へ紐付いている応募は永久に空のまま残っていた。

実測（応募日 2026-08-02以降 652件）:

```
一次対応の要否が空  179件(27%) → うち 48件 は求人側に値がある = 伝播すれば埋まる
担当者が空          151件(23%) → うち 15件 は求人側に値がある
```

残り（131件 / 136件）は求人側も空で、これは取引側の入力マター（別問題・保留中）。

## 守る不変条件

顧客データを後から書き換える処理なので、安全側の条件を固定する。

1. **応募側に既に値があるなら触らない**（人が入れた値を上書きしない）
2. 求人側が空なら何もしない
3. 一次対応の要否は作成時と同じ7日ガードを通す（古い応募をBPOの未対応キューに積まない）
4. 求人が複数紐付く場合は、値を持つ最初のものを使う
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import applicant_sync as asy


TODAY_OK = "2026-08-08"      # 実行日から7日以内という前提のテストでは新しい日付を使う


def _appt(ichi=None, owner=None, yingmuri=""):
    d = {}
    if ichi is not None:
        d["ichijitaiounoumu"] = ichi
    if owner is not None:
        d["hubspot_owner_id"] = owner
    if yingmuri:
        d["yingmuri"] = yingmuri
    return d


def _listing(ichi=None, owner=None):
    d = {}
    if ichi is not None:
        d["ichijitaiounoumu_deforuto"] = ichi
    if owner is not None:
        d["hubspot_owner_id"] = owner
    return d


def _today():
    from datetime import date
    return date.today().isoformat()


# --------------------------------------------------------------------------
# 1. 空欄が埋まる
# --------------------------------------------------------------------------
def test_要否が空なら求人側の値で埋める():
    p = asy._propagate_props(_appt(yingmuri=_today()), [_listing(ichi="必要")])
    assert p == {"ichijitaiounoumu": "必要"}


def test_担当者が空なら求人側の値で埋める():
    p = asy._propagate_props(_appt(yingmuri=_today()), [_listing(owner="12345")])
    assert p == {"hubspot_owner_id": "12345"}


def test_両方空なら両方埋める():
    p = asy._propagate_props(_appt(yingmuri=_today()),
                             [_listing(ichi="不要", owner="999")])
    assert p == {"ichijitaiounoumu": "不要", "hubspot_owner_id": "999"}


# --------------------------------------------------------------------------
# 2. 既存値を絶対に上書きしない
# --------------------------------------------------------------------------
def test_応募側に値があれば上書きしない():
    p = asy._propagate_props(
        _appt(ichi="不要", owner="111", yingmuri=_today()),
        [_listing(ichi="必要", owner="999")])
    assert p == {}, "人が入れた値を機械が書き換えてはいけない"


def test_片方だけ埋まっている場合は空いている方だけ埋める():
    p = asy._propagate_props(_appt(ichi="必要", yingmuri=_today()),
                             [_listing(ichi="不要", owner="999")])
    assert p == {"hubspot_owner_id": "999"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_空白文字だけなら空とみなす(blank):
    p = asy._propagate_props(_appt(ichi=blank, yingmuri=_today()),
                             [_listing(ichi="必要")])
    assert p == {"ichijitaiounoumu": "必要"}


# --------------------------------------------------------------------------
# 3. 求人側が空なら何もしない
# --------------------------------------------------------------------------
def test_求人側も空なら何もしない():
    assert asy._propagate_props(_appt(yingmuri=_today()), [_listing()]) == {}


def test_求人が紐付いていなければ何もしない():
    assert asy._propagate_props(_appt(yingmuri=_today()), []) == {}


def test_要否が想定外の値なら入れない():
    """選択肢は 必要/不要 のみ。未知の値を横流ししない。"""
    p = asy._propagate_props(_appt(yingmuri=_today()), [_listing(ichi="未設定")])
    assert "ichijitaiounoumu" not in p


# --------------------------------------------------------------------------
# 4. 古い応募には要否を付けない（作成時と同じガード）
# --------------------------------------------------------------------------
def test_古い応募には要否を付けない():
    p = asy._propagate_props(_appt(yingmuri="2026-01-15"),
                             [_listing(ichi="必要", owner="999")])
    assert "ichijitaiounoumu" not in p, "古い応募をBPOの未対応キューに積まない"
    assert p == {"hubspot_owner_id": "999"}, "担当者は日付に関係なく埋めてよい"


def test_応募日が空なら要否を落とさない():
    """日付が読めないことを理由に対応対象から外さない（安全側）。"""
    p = asy._propagate_props(_appt(yingmuri=""), [_listing(ichi="必要")])
    assert p == {"ichijitaiounoumu": "必要"}


# --------------------------------------------------------------------------
# 5. 複数求人が紐付く場合
# --------------------------------------------------------------------------
def test_複数求人なら値を持つ最初のものを使う():
    p = asy._propagate_props(
        _appt(yingmuri=_today()),
        [_listing(), _listing(ichi="必要"), _listing(ichi="不要")])
    assert p["ichijitaiounoumu"] == "必要"
