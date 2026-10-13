"""AirWork: エクスポートから消えた「公開中」求人を公開終了にする (2026-10-13) の回帰テスト.

HubSpot には一切アクセスしない。requests.post を偽の台帳に差し替えて検証する。
"""
from __future__ import annotations

import openpyxl
import pytest

from scripts.job_application_sync import airwork_import as awi
from scripts.job_application_sync import listing_stage as stage

LOGIN = "AAA111"
OTHER = "BBB222"
HEADER = list(awi.AW_XLSX_COLUMNS.keys())


def make_xlsx(path, rows):
    """rows: [(求人番号, 掲載状況)]. client_code は LOGIN."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADER)
    for jid, st in rows:
        d = {"求人番号(job_offer_id)": jid, "掲載状況(publish_status)": st,
             "クライアントコード(client_code)": LOGIN, "承認状況(approval_status)": "01",
             "職種名(title)": f"職種{jid}", "仕事内容(description)": "内容"}
        ws.append([d.get(h, "") for h in HEADER])
    wb.save(path)
    return str(path)


class FakeHubSpot:
    """LISTING の台帳。search / batch update / create だけ実装."""

    def __init__(self, listings):
        self.db = {}
        for i, (jid, login, status, stg) in enumerate(listings, start=1):
            props = {"id_airwork": jid, "airwork_account_login_id": login,
                     "kyuujin_status": status, "hs_pipeline_stage": stg,
                     "baitai_genjoukyou_airwork": "02" if status == "公開中" else "01"}
            self.db[str(i)] = props
        self.updates = []   # (id, props)

    def post(self, url, headers=None, json=None, timeout=None):
        outer = self

        class R:
            status_code = 200
            text = ""

            def __init__(s, payload):
                s._p = payload

            def json(s):
                return s._p

            def raise_for_status(s):
                pass

        if url.endswith("/search"):
            filters = json["filterGroups"][0]["filters"]
            res = []
            for hid, p in self.db.items():
                ok = True
                for f in filters:
                    v = p.get(f["propertyName"])
                    if f["operator"] == "IN":
                        ok &= v in f["values"]
                    elif f["operator"] == "EQ":
                        ok &= v == f["value"]
                    elif f["operator"] == "HAS_PROPERTY":
                        ok &= bool(v)
                if ok:
                    res.append({"id": hid, "properties": dict(p)})
            return R({"results": res})
        if url.endswith("/batch/update"):
            for inp in json["inputs"]:
                self.updates.append((inp["id"], inp["properties"]))
                self.db[inp["id"]].update(inp["properties"])
            return R({"results": json["inputs"], "numErrors": 0})
        if url.endswith("/batch/create"):
            return R({"results": [], "numErrors": 0})
        raise AssertionError(url)


@pytest.fixture
def hub(monkeypatch):
    def install(listings):
        h = FakeHubSpot(listings)
        monkeypatch.setattr(awi.requests, "post", h.post)
        monkeypatch.setattr(awi.time, "sleep", lambda *_: None)
        monkeypatch.setattr(awi, "LOG_DIR", awi.LOG_DIR)
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "dummy-not-used")
        monkeypatch.delenv("JAS_AW_END_MISSING", raising=False)
        monkeypatch.setattr("scripts.job_application_sync.notes.attach_template_notes",
                            lambda ids: (0, []))
        return h
    return install


def ended(h):
    return {h.db[i]["id_airwork"] for i, _ in h.updates
            if h.db[i]["kyuujin_status"] == "公開終了"}


def test_完全なエクスポートで消えた公開中は公開終了になる(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J3", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    x = make_xlsx(tmp_path / "aw_AAA111_1.xlsx", [("J1", "02")])
    s = awi.run(x, LOGIN, dry_run=False)
    assert s["ends_planned"] == 2 and s["ends_ok"] == 2
    assert ended(h) == {"J2", "J3"}
    p2 = next(p for i, p in h.updates if h.db[i]["id_airwork"] == "J2")
    assert p2["kyuujin_status"] == "公開終了"
    assert p2["baitai_genjoukyou_airwork"] == "01"
    assert p2["konkai_csv_kenshutsu_flag"] == "false"
    assert p2["hs_pipeline_stage"] == stage.STAGE_TEISHI
    assert p2["douki_moto_filename"] == "aw_AAA111_1.xlsx"
    assert "saishuu_csv_kenshutsu_bi" not in p2 and "hs_name" not in p2
    # 載っている J1 は公開中のまま
    assert h.db["1"]["kyuujin_status"] == "公開中"


def test_選考進行中のステージは動かさない(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_SENKOU)])
    awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert h.db["2"]["kyuujin_status"] == "公開終了"
    assert h.db["2"]["hs_pipeline_stage"] == stage.STAGE_SENKOU


def test_行数0のエクスポートでは何もしない(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    s = awi.run(make_xlsx(tmp_path / "a.xlsx", []), LOGIN, dry_run=False)
    assert s["ends_planned"] == 0 and h.updates == []


def test_パース失敗では何もしない(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not an xlsx")
    with pytest.raises(Exception):
        awi.run(str(bad), LOGIN, dry_run=False)
    assert h.updates == []


def test_limit指定や求人ID欠落では何もしない(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    x = make_xlsx(tmp_path / "a.xlsx", [("J1", "02"), ("J9", "02")])
    s = awi.run(x, LOGIN, dry_run=False, limit=1)
    assert s["ends_planned"] == 0
    assert ended(h) == set()
    x2 = make_xlsx(tmp_path / "b.xlsx", [("J1", "02"), ("", "02")])
    s2 = awi.run(x2, LOGIN, dry_run=False)
    assert s2["ends_planned"] == 0 and ended(h) == set()


def test_別口座と口座IDなしの求人は触らない(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("K1", OTHER, "公開中", stage.STAGE_BOSHUU),
             ("N1", "", "公開中", stage.STAGE_BOSHUU),
             ("N2", None, "公開中", stage.STAGE_BOSHUU)])
    awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert h.updates and all(h.db[i]["airwork_account_login_id"] == LOGIN
                             for i, _ in h.updates)
    for hid in ("2", "3", "4"):
        assert h.db[hid]["kyuujin_status"] == "公開中"
    # login_id 空で呼んでも何も終了にしない
    assert awi.export_is_complete(5, 5, 0, None, "") is False


def test_すでに公開終了の求人は対象外(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開終了", stage.STAGE_TEISHI)])
    s = awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert s["ends_planned"] == 0


def test_終了後に02で再出現すれば公開中に戻る(hub, tmp_path):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert h.db["2"]["kyuujin_status"] == "公開終了"
    assert h.db["2"]["hs_pipeline_stage"] == stage.STAGE_TEISHI
    awi.run(make_xlsx(tmp_path / "b.xlsx", [("J1", "02"), ("J2", "02")]),
            LOGIN, dry_run=False)
    assert h.db["2"]["kyuujin_status"] == "公開中"
    assert h.db["2"]["hs_pipeline_stage"] == stage.STAGE_BOSHUU
    assert h.db["2"]["baitai_genjoukyou_airwork"] == "02"
    assert h.db["2"]["konkai_csv_kenshutsu_flag"] == "true"


def test_環境変数で止められる(hub, tmp_path, monkeypatch):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    monkeypatch.setenv("JAS_AW_END_MISSING", "0")
    s = awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert s["ends_planned"] == 0 and ended(h) == set()


def test_検索失敗なら終了せず従来どおり(hub, tmp_path, monkeypatch):
    h = hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU),
             ("J2", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    def boom(login):
        raise RuntimeError("x")
    monkeypatch.setattr(awi, "find_published_listings_for_login", boom)
    s = awi.run(make_xlsx(tmp_path / "a.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert s["ends_planned"] == 0 and h.db["2"]["kyuujin_status"] == "公開中"
