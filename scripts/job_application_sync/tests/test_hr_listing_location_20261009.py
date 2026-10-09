"""勤務地 (都道府県/市区町村) を店舗一覧から LISTING へ補完する (2026-10-09). 架空データのみ.

守る不変条件:
1. 店舗一覧の読込は47都道府県の行だけ採用し、同じ店舗idで値が食い違う店舗は捨てる
2. HubSpot が空の項目だけ書く。人が入れた値は上書きしない。店舗一覧に無い店舗は何も書かない
3. 更新/新規の両方に入る。--copy-only は勤務地も埋めるが新規作成しない
4. 店舗一覧の取得に失敗しても取込は勤務地なしで続く
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

import hrhacker_import as hi  # noqa: E402
import hr_watcher as hw  # noqa: E402

BR_HEADER = ("店舗id,企業id,店舗名,Indeed表示 社名・店舗名,郵便番号,都道府県,市区町村,町名,"
             "番地,建物名,電話番号,アクセス方法,最寄り駅,サービス状況\n")


def write_branches(tmp_path, lines):
    p = tmp_path / "b.csv"
    p.write_text(BR_HEADER + "\n".join(lines) + "\n", encoding="cp932")
    return p


def test_loader_skips_invalid_prefecture_and_drops_conflicts(tmp_path):
    p = write_branches(tmp_path, [
        "10,1,店あ,,1000001,東京都,千代田区,,,,,,,",
        "11,1,店い,,5300001,大阪府,,,,,,,,",            # 市区町村が空 -> None
        "12,1,店う,,0000000,不明県,どこか,,,,,,,",       # 都道府県が不正 -> 除外
        "13,1,店え,,0600001,北海道,札幌市,,,,,,,",
        "13,1,店え,,0600001,北海道,旭川市,,,,,,,",      # 同じ店舗idで値が違う -> 除外
        "14,1,店お,,8100001,福岡県,福岡市,,,,,,,",
        "14,1,店お,,8100001,福岡県,福岡市,,,,,,,",      # 同じ値の重複は残す
    ])
    got = hi.load_branch_locations(p)
    assert got == {"10": ("東京都", "千代田区"), "11": ("大阪府", None),
                   "14": ("福岡県", "福岡市")}


def test_location_props_fills_only_empty_never_overwrites():
    br = {"9": ("東京都", "千代田区")}
    row = {"shop_id": "9"}
    assert hi.location_props(row, {}, br) == {"todoufuken": "東京都", "shikuchouson": "千代田区"}
    # 都道府県は人が入力済み -> 市区町村だけ
    assert hi.location_props(row, {"todoufuken": "大阪府"}, br) == {"shikuchouson": "千代田区"}
    # 両方入力済み -> 何も書かない
    assert hi.location_props(row, {"todoufuken": "大阪府", "shikuchouson": "北区"}, br) == {}
    # 空白だけは空として扱う
    assert hi.location_props(row, {"todoufuken": "  "}, br)["todoufuken"] == "東京都"
    # 店舗一覧に無い店舗 / 機能オフ -> 何も書かない
    assert hi.location_props({"shop_id": "99"}, {}, br) == {}
    assert hi.location_props(row, {}, None) == {}
    # 市区町村が無い店舗は都道府県だけ
    assert hi.location_props(row, {}, {"9": ("大阪府", None)}) == {"todoufuken": "大阪府"}


ROW = {"media_job_id": "1", "shop_id": "9", "job_name": "A", "original_status": "公開",
       "start_date": "", "end_date": ""}


def test_build_update_and_create_include_location():
    br = {"9": ("愛知県", "名古屋市")}
    up = hi.build_update_props(ROW, {"todoufuken": "大阪府"}, "2026-10-09", "now", "f.csv", br)
    assert "todoufuken" not in up and up["shikuchouson"] == "名古屋市"
    cr = hi.build_create_props(ROW, "2026-10-09", "now", "f.csv", br)
    assert cr["todoufuken"] == "愛知県" and cr["shikuchouson"] == "名古屋市"
    # 機能オフなら従来と同じ (勤務地のキーが出ない)
    assert "todoufuken" not in hi.build_update_props(ROW, {}, "d", "n", "f")
    assert "todoufuken" not in hi.build_create_props(ROW, "d", "n", "f")


def test_find_hubspot_jobs_reads_location_props(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        headers = {}

        def json(self):
            return {"results": []}

        def raise_for_status(self):
            pass
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    monkeypatch.setattr(hi.requests, "post",
                        lambda url, **kw: seen.setdefault("b", kw["json"]) and R())
    hi.find_hubspot_jobs(["1"])
    assert {"todoufuken", "shikuchouson"} <= set(seen["b"]["properties"])


def test_copy_only_fills_location_and_never_creates(monkeypatch, tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("求人id,店舗id,案件名,仕事内容,画像1,画像2,画像3,公開\n"
                 "1,9,あ,い,,,,公開\n2,77,う,え,,,,公開\n", encoding="shift_jis")
    rows = hi.load_hr_csv(p)
    same = {hi.PROP_COPY_BODY: rows[0]["copy_body"], hi.PROP_COPY_IMAGES: rows[0]["copy_images"]}
    same2 = {hi.PROP_COPY_BODY: rows[1]["copy_body"], hi.PROP_COPY_IMAGES: rows[1]["copy_images"]}
    existing = {"1": {"id": "h1", "properties": {"id_hrhakkaa": "1", **same}},   # 本文は同じ・勤務地が空
                "2": {"id": "h2", "properties": {"id_hrhakkaa": "2", **same2}}}  # 店舗一覧に無い
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    monkeypatch.setattr(hi, "find_hubspot_jobs", lambda ids: existing)
    captured = {}
    monkeypatch.setattr(hi, "batch_update", lambda u: captured.setdefault("u", u) and (len(u), 0, []))
    monkeypatch.setattr(hi, "batch_create", lambda c: pytest.fail("作成してはいけない"))
    res = hi.run_copy_only(str(p), dry_run=False, branch_locations={"9": ("東京都", "港区")})
    assert res["location_filled"] == 1 and res["shop_not_in_branches"] == 1
    assert res["unchanged"] == 1 and res["updates_planned"] == 1
    assert captured["u"] == [{"id": "h1", "properties": {"todoufuken": "東京都", "shikuchouson": "港区"}}]


def test_watcher_continues_without_location_when_branch_fetch_fails(tmp_path):
    rows = [{"media_job_id": "1", "shop_id": "9", "job_name": "A", "original_status": "公開",
             "start_date": "", "end_date": "", "copy_body": "b", "copy_images": "i"}]
    called = {}

    def boom():
        raise TimeoutError("download")

    def run_fn(path, dry_run, **kw):
        called["kw"] = kw
        return {"updates_ng": 0, "creates_ng": 0}
    res = hw.run(csv_path="x.csv", dry_run=False, skip_aw=True, snapshot_dir=tmp_path,
                 load_csv_fn=lambda p: rows, hrhacker_run_fn=run_fn, branches_fetcher=boom)
    assert called["kw"] == {}                      # 勤務地なしで取込が走った
    assert res["branches_error"] == "TimeoutError"
    assert "snapshot_path" in res                  # 取込成功扱いで控えも進む


def test_watcher_passes_branch_locations_when_fetch_succeeds(tmp_path):
    rows = [{"media_job_id": "1", "shop_id": "10", "job_name": "A", "original_status": "公開",
             "start_date": "", "end_date": "", "copy_body": "b", "copy_images": "i"}]
    bp = write_branches(tmp_path, ["10,1,店あ,,1000001,東京都,千代田区,,,,,,,"])
    called = {}

    def run_fn(path, dry_run, **kw):
        called["kw"] = kw
        return {"updates_ng": 0, "creates_ng": 0}
    hw.run(csv_path="x.csv", dry_run=False, skip_aw=True, snapshot_dir=tmp_path / "s",
           load_csv_fn=lambda p: rows, hrhacker_run_fn=run_fn, branches_fetcher=lambda: bp)
    assert called["kw"] == {"branch_locations": {"10": ("東京都", "千代田区")}}
