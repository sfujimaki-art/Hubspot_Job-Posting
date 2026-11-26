"""地図用の住所 (2026-11-26). 住所・名前はすべて架空.

1. AirWork の本文から勤務地の住所を取り出す (郵便番号・建物名を外す)
2. HRハッカーは店舗一覧の 都道府県＋市区町村＋町名＋番地 (建物名なし) を kinmuchi_juusho に入れる。値が同じなら書かない
3. 応募の oubosaki_kinmuchi_juusho は 求人票の住所と応募先勤務地の長いほう。計算式 (hrhr_*) は書かない
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

import hrhacker_import as hi  # noqa: E402
import map_address as M  # noqa: E402
from scripts.job_application_sync import applicant_import as ai  # noqa: E402

BR_HEADER = ("店舗id,企業id,店舗名,Indeed表示 社名・店舗名,郵便番号,都道府県,市区町村,町名,"
             "番地,建物名,電話番号,アクセス方法,最寄り駅,サービス状況\n")


def test_extract():
    body = "キャッチコピー：x\n勤務地：〒100-0001 東京都千代田区架空町1-2-3 架空ビル5F\nアクセス：駅から"
    assert M.extract(body) == "東京都千代田区架空町1-2-3"
    assert M.extract("勤務地：東京都 千代田区 架空町 1-2 ビル") == "東京都千代田区架空町1-2"
    assert M.extract("勤務地：\nアクセス：駅") is None
    assert M.extract("勤務地：アクセス：駅") is None
    assert M.extract("給与：1円") is None
    assert M.extract(None) is None


def test_store_address():
    assert M.store_address("東京都", "千代田区", "架空町", "1-2-3") == "東京都千代田区架空町1-2-3"
    assert M.store_address("東京都", "千代田区", "架空町", "") == "東京都千代田区架空町"
    assert M.store_address("東京都", "千代田区", "", "") is None
    assert M.store_address("北海道", "札幌市 北区", "架空", "1?2?3") == "北海道札幌市北区架空1-2-3"


def test_job_address_and_pick():
    assert M.job_address({"kinmuchi_juusho": " A1-2 ", "qinwude": "Q"}) == "A1-2"
    assert M.job_address({"qinwude": "Q"}) == "Q"
    assert M.job_address({"todoufuken": "東京都", "shikuchouson": "千代田区"}) == "東京都千代田区"
    assert M.job_address({"todoufuken": "unset", "shikuchouson": "千代田区"}) == "千代田区"
    assert M.job_address({}) == ""
    assert M.pick("東京都千代田区架空町1", "東京都千代田区") == "東京都千代田区架空町1"
    assert M.pick("東京都", "東京都千代田区") == "東京都千代田区"
    assert M.pick("求人AB", "応募CD") == "求人AB"
    assert M.pick("", None) is None


def _branches(tmp_path, lines):
    p = tmp_path / "b.csv"
    p.write_text(BR_HEADER + "\n".join(lines) + "\n", encoding="cp932")
    return hi.load_branch_locations(p)


def test_loader_keeps_old_shape_and_adds_addresses(tmp_path):
    br = _branches(tmp_path, [
        "10,1,店あ,,1000001,東京都,千代田区,架空町,1-2-3,架空ビル,,,,",
        "11,1,店い,,5300001,大阪府,大阪市北区,,,,,,,",
        "13,1,店え,,0600001,北海道,札幌市,架空,1,,,,,",
        "13,1,店え,,0600001,北海道,札幌市,架空,2,,,,,",    # 同じ店舗idで番地が違う -> 除外
    ])
    assert br == {"10": ("東京都", "千代田区"), "11": ("大阪府", "大阪市北区")}
    assert br.addresses == {"10": "東京都千代田区架空町1-2-3"}


def test_address_props_only_when_changed(tmp_path):
    br = _branches(tmp_path, ["10,1,店,,1000001,東京都,千代田区,架空町,1-2-3,,,,,"])
    row = {"shop_id": "10"}
    assert hi.address_props(row, {}, br) == {"kinmuchi_juusho": "東京都千代田区架空町1-2-3"}
    assert hi.address_props(row, {"kinmuchi_juusho": "東京都千代田区架空町1-2-3 "}, br) == {}
    assert hi.address_props(row, {"kinmuchi_juusho": "旧住所"}, br) == {
        "kinmuchi_juusho": "東京都千代田区架空町1-2-3"}
    assert hi.address_props({"shop_id": "99"}, {}, br) == {}
    assert hi.address_props(row, {}, None) == {}
    assert hi.address_props(row, {}, {"10": ("東京都", "千代田区")}) == {}  # 旧形式の辞書


def test_update_and_create_props_include_address(tmp_path):
    br = _branches(tmp_path, ["10,1,店,,1000001,東京都,千代田区,架空町,1-2-3,,,,,"])
    row = {"media_job_id": "1", "shop_id": "10", "original_status": "公開"}
    u = hi.build_update_props(row, {}, "2026-11-26", "2026-11-26T00:00:00Z", "f.csv", br)
    c = hi.build_create_props(row, "2026-11-26", "2026-11-26T00:00:00Z", "f.csv", br)
    for p in (u, c):
        assert p["kinmuchi_juusho"] == "東京都千代田区架空町1-2-3"
        assert not [k for k in p if k.startswith("hrhr_")]
        assert "qinwude" not in p


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._b = body

    def json(self):
        return self._b


def _oubosaki(monkeypatch, listing):
    cli = ai.RealHubSpotClient("dummy")
    urls = []

    def get(url, **k):
        urls.append(url)
        if "/0-420/" in url:
            return _Resp({"properties": listing})
        return _Resp({"properties": {}})
    monkeypatch.setattr(cli._requests, "get", get)
    monkeypatch.setattr(cli, "_listing_deal_group", lambda *a: ([], {}))
    props = cli.get_oubosaki_props("L1", "HRハッカー", "", "1")
    return props, urls


def test_oubosaki_address_uses_longer(monkeypatch):
    props, urls = _oubosaki(monkeypatch, {"qinwude": "東京都千代田区",
                                          "kinmuchi_juusho": "東京都千代田区架空町1-2-3"})
    assert "kinmuchi_juusho" in urls[0] and "todoufuken" in urls[0]
    assert props["oubosaki_kinmuchi"] == "東京都千代田区"          # 既存の項目は今までどおり
    assert props["oubosaki_kinmuchi_juusho"] == "東京都千代田区架空町1-2-3"
    assert not [k for k in props if k.startswith("hrhr_")]


def test_oubosaki_address_falls_back_and_skips_empty(monkeypatch):
    props, _ = _oubosaki(monkeypatch, {"todoufuken": "東京都", "shikuchouson": "千代田区"})
    assert props["oubosaki_kinmuchi_juusho"] == "東京都千代田区"
    props, _ = _oubosaki(monkeypatch, {})
    assert "oubosaki_kinmuchi_juusho" not in props
