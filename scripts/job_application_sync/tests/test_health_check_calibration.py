"""health_check の判定軸を固定する回帰テスト (2026-08-09 較正)。

## なぜ較正したか

health_check は5項目中3項目が**恒久的にNG**を出し続けており、しかも
終了コードは0なのでCIは success。誰も見なくなる状態だった。
今回の一連で「静かに壊れる故障」を何度も追いかけたが、その検知装置自体が
同じ状態になっていた。3項目を1つずつ実データで確かめた結果、
**本物の異常は1件だけ**で、残り2件は監視側の較正ミスだった。

| 項目 | 主張していた値 | 実測 |
|---|---|---|
| 10,000件上限に接近 | 顧客シートURL持ちの求人 9,644件=96% | 該当するsearchはもう存在しない(誤警報) |
| 直近3日の応募が未紐付け | 882件中155件 | 応募日基準なら0件(登録日で過去分を拾っていた) |
| 直近14日の求人が未紐付け | 4,987件中203件 | 応募が来ている公開中に限れば52件 |

較正後: 2/5 → 4/5 が正常。残る1件(52件)は実在する問題。

## 守る不変条件

1. 応募の紐付き判定は **yingmuri(応募日)** で見る。hs_createdate では見ない
2. 求人の紐付き判定は **応募が来ている公開中の求人**だけを母数にする
3. 上限監視は **実際に search でページングしているクエリ**だけを対象にする
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import health_check as hc


# --------------------------------------------------------------------------
# 1. 応募の紐付き判定は応募日で見る
# --------------------------------------------------------------------------
def test_応募の紐付き判定は応募日で検索する(monkeypatch):
    seen = {}

    def fake_search_all(obj, props, filters):
        seen["obj"] = obj
        seen["props"] = props
        seen["filters"] = filters
        return [{"id": "1"}, {"id": "2"}]

    monkeypatch.setattr(hc, "search_all", fake_search_all)
    monkeypatch.setattr(hc, "_assoc", lambda a, b, ids: {"1": ["L1"]})
    r = hc.check_recent_applications_linked()

    assert seen["obj"] == "0-421"
    names = {f["propertyName"] for f in seen["filters"]}
    assert names == {"yingmuri"}, "登録日(hs_createdate)で数えてはいけない"
    assert "yingmuri" in seen["props"]
    assert r["value"] == 1, "紐付いていない1件を数える"


def test_応募がゼロなら正常扱い(monkeypatch):
    monkeypatch.setattr(hc, "search_all", lambda *a, **k: [])
    r = hc.check_recent_applications_linked()
    assert r["value"] == 0 and r["want"] == 0


# --------------------------------------------------------------------------
# 2. 求人の紐付き判定は「応募が来ている公開中」だけ
# --------------------------------------------------------------------------
def _listing(i, status="公開中"):
    return {"id": i, "properties": {"hs_name": f"求人{i}", "kyuujin_status": status}}


def _setup_listings(monkeypatch, rows, deal_map, appt_map):
    monkeypatch.setattr(hc, "search_all", lambda *a, **k: rows)

    def fake_assoc(frm, to, ids):
        return deal_map if to == "0-3" else appt_map

    monkeypatch.setattr(hc, "_assoc", fake_assoc)


def test_応募が来ていない求人は母数に入れない(monkeypatch):
    """HRハッカーは他社の求人も取り込むため、全部を母数にすると永久にNGになる。"""
    rows = [_listing("A"), _listing("B"), _listing("C")]
    _setup_listings(monkeypatch, rows, deal_map={}, appt_map={"A": ["P1"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 1, "応募が来ているAだけを数える(B/Cは他社求人の可能性)"


def test_公開終了は母数に入れない(monkeypatch):
    rows = [_listing("A", "公開終了"), _listing("B")]
    _setup_listings(monkeypatch, rows, deal_map={},
                    appt_map={"A": ["P1"], "B": ["P2"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 1, "公開終了のAは除外、Bだけ数える"


def test_取引に紐付いていれば正常(monkeypatch):
    rows = [_listing("A"), _listing("B")]
    _setup_listings(monkeypatch, rows, deal_map={"A": ["D1"], "B": ["D2"]},
                    appt_map={"A": ["P1"], "B": ["P2"]})
    r = hc.check_recent_listings_linked()
    assert r["value"] == 0


def test_母数と作成件数の両方を報告する(monkeypatch):
    """「母数から除外した」ことが読み手に分かるようにする(集計軸の明示)。"""
    rows = [_listing(str(i)) for i in range(10)]
    _setup_listings(monkeypatch, rows, deal_map={}, appt_map={"0": ["P1"]})
    r = hc.check_recent_listings_linked()
    d = " ".join(r["detail"])
    assert "応募が来ている公開中の求人" in d
    assert "10" in d, "作成された求人の総数も併記する"


# --------------------------------------------------------------------------
# 3. 上限監視は実在するクエリだけ
# --------------------------------------------------------------------------
def test_上限監視の対象は実在するsearchだけ(monkeypatch):
    labels = []

    def fake_total(obj, filters):
        labels.append((obj, tuple(f["propertyName"] for f in filters)))
        return 100

    monkeypatch.setattr(hc, "_total", fake_total)
    hc.check_search_cap()
    props = {p for _, fs in labels for p in fs}
    assert "customer_sheet_url" not in props, (
        "drift は list API に移行済み。search していないものを監視しない")
    assert {"pipeline", "kanri_mail_address"} <= props


def test_上限に達したら異常として数える(monkeypatch):
    monkeypatch.setattr(hc, "_total", lambda o, f: hc.SEARCH_CAP)
    r = hc.check_search_cap()
    assert r["value"] == 2, "監視対象2つとも上限なので2件の異常"


def test_余裕があれば正常(monkeypatch):
    monkeypatch.setattr(hc, "_total", lambda o, f: 100)
    r = hc.check_search_cap()
    assert r["value"] == 0
