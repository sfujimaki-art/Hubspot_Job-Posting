"""消えた掲載中求人を公開終了にする (2026-10-15) の回帰テスト。合成データのみ。HubSpot には触れない。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.job_application_sync import airwork_import as awi
from scripts.job_application_sync import end_vanished_listings as ev
from scripts.job_application_sync import hrhacker_import as hi
from scripts.job_application_sync import listing_stage as stage
from scripts.job_application_sync.tests.test_aw_end_missing_20261013 import (  # noqa: F401
    LOGIN, OTHER, hub, make_xlsx,
)

TODAY = date(2026, 10, 15)


def L(i, shop="S1", ago=0, **kw):
    p = {"id_hrhakkaa": f"J{i}", "id_shop_hrhakkaa": shop,
         "saishuu_csv_kenshutsu_bi": (TODAY - timedelta(days=ago)).isoformat(),
         "kyuujin_status": "公開中", "hs_pipeline_stage": stage.STAGE_BOSHUU}
    p.update(kw)
    return {"id": str(i), "properties": p}


# ---- HRH 計画 ---------------------------------------------------------------
def test_境界_2日前は残し3日前から終了():
    ls = [L(i, ago=0) for i in range(1, 8)] + [L(8, ago=2), L(9, ago=3), L(10, ago=30)]
    ends, skipped, _ = ev.plan_hrh_ends(ls, TODAY)
    assert sorted(l["id"] for l in ends) == ["10", "9"] and not skipped


def test_今回のCSVに載っている求人は古い日付でも終了にしない():
    ls = [L(i, ago=0) for i in range(1, 8)] + [L(9, ago=10), L(10, ago=10)]
    ends, _, _ = ev.plan_hrh_ends(ls, TODAY, csv_ids=["J9"])
    assert [l["id"] for l in ends] == ["10"]


def test_日付なしは触らず数だけ数える():
    ls = [L(i) for i in range(1, 8)] + [L(9, **{"saishuu_csv_kenshutsu_bi": ""})]
    ends, _, no_date = ev.plan_hrh_ends(ls, TODAY)
    assert ends == [] and no_date == 1


def test_上限20パーセント超の店舗は見送り_他店舗は進む():
    # S1: 10件中3件が消えた(30%) → 見送り。S2: 10件中2件(20%ちょうど) → 実行
    s1 = [L(i, "S1", ago=0) for i in range(1, 8)] + [L(i, "S1", ago=9) for i in range(8, 11)]
    s2 = [L(i, "S2", ago=0) for i in range(11, 19)] + [L(i, "S2", ago=9) for i in range(19, 21)]
    ends, skipped, _ = ev.plan_hrh_ends(s1 + s2, TODAY)
    assert sorted(int(l["id"]) for l in ends) == [19, 20]
    assert skipped == {"S1": (3, 10)}


def test_cap_境界():
    assert not ev.over_cap(2, 5)          # 下限以下
    assert not ev.over_cap(4, 20)         # ちょうど20%
    assert ev.over_cap(5, 20)             # 25%
    assert not ev.over_cap(0, 0)


def test_全部消えた店舗は見送り():
    ls = [L(i, "S1", ago=9) for i in range(1, 6)]
    ends, skipped, _ = ev.plan_hrh_ends(ls, TODAY)
    assert ends == [] and skipped == {"S1": (5, 5)}


# ---- 完全性ガード -----------------------------------------------------------
@pytest.mark.parametrize("total,valid,nid,limit,expect", [
    (100, 100, 0, None, True),
    (0, 0, 0, None, False),        # 0件 = 取得失敗と区別できない
    (100, 99, 1, None, False),     # ID欠け
    (100, 100, 0, 10, False),      # --limit で切った
])
def test_csv_is_complete(monkeypatch, total, valid, nid, limit, expect):
    monkeypatch.delenv("JAS_HRH_END_VANISHED", raising=False)
    assert ev.csv_is_complete(total, valid, nid, limit) is expect


def test_環境変数で止められる(monkeypatch):
    monkeypatch.setenv("JAS_HRH_END_VANISHED", "0")
    assert not ev.csv_is_complete(100, 100, 0, None)


# ---- HRH 書き込み内容 / 再登場 ----------------------------------------------
def test_HRH終了の中身():
    p = ev.build_hrh_end_props({"hs_pipeline_stage": stage.STAGE_BOSHUU}, "NOW", "src")
    assert p["kyuujin_status"] == "公開終了" and p["baitai_genjoukyou_hrhakkaa"] == "非公開"
    assert p["hs_pipeline_stage"] == stage.STAGE_TEISHI and p["konkai_csv_kenshutsu_flag"] == "false"
    assert "saishuu_csv_kenshutsu_bi" not in p


def test_人が動かすステージは上書きしない():
    p = ev.build_hrh_end_props({"hs_pipeline_stage": stage.STAGE_SENKOU}, "NOW", "src")
    assert "hs_pipeline_stage" not in p and p["kyuujin_status"] == "公開終了"


def test_HRH再登場で公開中に戻る():
    row = {"media_job_id": "J1", "original_status": "公開", "job_name": "x"}
    existing = {"kyuujin_status": "公開終了", "baitai_genjoukyou_hrhakkaa": "非公開",
                "hs_pipeline_stage": stage.STAGE_TEISHI}
    p = hi.build_update_props(row, existing, "2026-10-16", "NOW", "f.csv")
    assert p["kyuujin_status"] == "公開中" and p["baitai_genjoukyou_hrhakkaa"] == "公開"
    assert p["hs_pipeline_stage"] == stage.STAGE_BOSHUU
    assert p["saishuu_csv_kenshutsu_bi"] == "2026-10-16"


def test_AW再登場で掲載中に戻る():
    row = {"media_job_id": "A1", "original_status": "02", "job_name": "x"}
    existing = {"kyuujin_status": "公開終了", "hs_pipeline_stage": stage.STAGE_TEISHI}
    p = awi.build_update_props(row, existing, "2026-10-16", "NOW", "f.xlsx")
    assert p["kyuujin_status"] == "公開中" and p["baitai_genjoukyou_airwork"] == "02"
    assert p["hs_pipeline_stage"] == stage.STAGE_BOSHUU


# ---- run_hrh_end_vanished / hi.run の呼び出し条件 ---------------------------
def test_run_hrh_dryrunは書かない_actualは書く():
    ls = [L(i) for i in range(1, 8)] + [L(9, ago=9), L(10, ago=9)]
    wrote = []
    s = ev.run_hrh_end_vanished([], True, "NOW", "src", today=TODAY,
                                fetch=lambda: ls, write=lambda u: wrote.append(u) or (len(u), 0))
    assert s["hrh_ends_planned"] == 2 and not wrote
    s = ev.run_hrh_end_vanished([], False, "NOW", "src", today=TODAY,
                                fetch=lambda: ls, write=lambda u: wrote.append(u) or (len(u), 0))
    assert s["hrh_ends_ok"] == 2 and len(wrote[0]) == 2


@pytest.fixture
def hr_run(monkeypatch, tmp_path):
    calls = []
    rows = [{"media_job_id": f"J{i}", "original_status": "公開", "job_name": "x",
             "shop_id": "S1"} for i in range(1, 4)]
    monkeypatch.setattr(hi, "load_hr_csv", lambda p: list(rows))
    monkeypatch.setattr(hi, "LOG_DIR", tmp_path)
    monkeypatch.setattr(hi, "find_hubspot_jobs", lambda ids: {})
    monkeypatch.setattr(hi, "batch_update", lambda u: (len(u), 0, []))
    monkeypatch.setattr(hi, "batch_create", lambda c: (len(c), 0, [], {}))
    monkeypatch.setattr("scripts.job_application_sync.notes.attach_template_notes", lambda ids: (0, []))
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "dummy")
    monkeypatch.delenv("JAS_HRH_END_VANISHED", raising=False)
    monkeypatch.setattr(ev, "run_hrh_end_vanished",
                        lambda *a, **k: calls.append(1) or {"hrh_ends_planned": 0, "hrh_skipped_shops": 0})
    return calls, rows, monkeypatch


def test_HRH取込_完全なCSVで全成功なら終了処理が走る(hr_run):
    calls, _, _ = hr_run
    hi.run("x.csv", dry_run=False)
    assert calls == [1]


def test_HRH取込_limit付きは終了処理を走らせない(hr_run):
    calls, _, _ = hr_run
    hi.run("x.csv", dry_run=False, limit=2)
    assert calls == []


def test_HRH取込_ID欠けの行があれば走らせない(hr_run):
    calls, rows, _ = hr_run
    rows.append({"media_job_id": "", "original_status": "公開", "job_name": "x", "shop_id": "S1"})
    hi.run("x.csv", dry_run=False)
    assert calls == []


def test_HRH取込_書き込みに失敗があれば走らせない(hr_run):
    calls, _, mp = hr_run
    mp.setattr(hi, "batch_create", lambda c: (0, len(c), ["e"], {}))
    hi.run("x.csv", dry_run=False)
    assert calls == []


# ---- AirWork -----------------------------------------------------------------
def test_AW_上限超えの口座は公開終了を見送る(hub, tmp_path):
    h = hub([(f"J{i}", LOGIN, "公開中", stage.STAGE_BOSHUU) for i in range(1, 11)])
    x = make_xlsx(tmp_path / "aw_AAA111_1.xlsx", [("J1", "02")])
    s = awi.run(x, LOGIN, dry_run=False)
    assert s["ends_planned"] == 0 and s["ends_skipped_cap"] == 9
    assert not [1 for i, _ in h.updates if h.db[i]["kyuujin_status"] == "公開終了"]


def test_AW_上限内なら終了し_別口座は触らず_取得を台帳に残す(hub, tmp_path):
    h = hub([(f"J{i}", LOGIN, "公開中", stage.STAGE_BOSHUU) for i in range(1, 11)]
            + [("X1", OTHER, "公開中", stage.STAGE_BOSHUU)])
    x = make_xlsx(tmp_path / "aw_AAA111_1.xlsx", [(f"J{i}", "02") for i in range(1, 10)])
    s = awi.run(x, LOGIN, dry_run=False)
    assert s["ends_planned"] == 1 and s["ends_ok"] == 1
    other = next(p for p in h.db.values() if p["id_airwork"] == "X1")
    assert other["kyuujin_status"] == "公開中"
    assert ev.load_aw_ledger() == {ev.plog.file_tag(LOGIN): date.today()}


def test_AW_dryrunは台帳に書かない(hub, tmp_path):
    hub([("J1", LOGIN, "公開中", stage.STAGE_BOSHUU)])
    x = make_xlsx(tmp_path / "aw_AAA111_1.xlsx", [("J1", "02")])
    awi.run(x, LOGIN, dry_run=True)
    assert ev.load_aw_ledger() == {}


# ---- AW 是正の計画 ------------------------------------------------------------
def A(i, login, ago):
    return {"id": str(i), "properties": {
        "id_airwork": f"A{i}", "airwork_account_login_id": login,
        "saishuu_csv_kenshutsu_bi": (TODAY - timedelta(days=ago)).isoformat()}}


def test_AW是正_最近取得した口座の取得に載らなかった求人だけ終了():
    ls = [A(i, "acc1", 1) for i in range(1, 10)] + [A(10, "acc1", 20)]
    plan = ev.plan_aw_cleanup(ls, TODAY)
    assert [l["id"] for l in plan["ends"]] == ["10"] and plan["review"] == 0


def test_AW是正_最近取得されていない口座は要確認で触らない():
    ls = [A(i, "acc2", 8) for i in range(1, 6)] + [A(6, "acc2", 40)]
    plan = ev.plan_aw_cleanup(ls, TODAY)
    assert plan["ends"] == [] and plan["review"] == 6   # 境界: 7日以内のみ「最近」


def test_AW是正_7日ちょうどは最近():
    ls = [A(i, "acc3", 7) for i in range(1, 10)] + [A(10, "acc3", 30)]
    assert [l["id"] for l in ev.plan_aw_cleanup(ls, TODAY)["ends"]] == ["10"]


def test_AW是正_台帳があれば台帳の日付を使う():
    ls = [A(i, "acc4", 20) for i in range(1, 10)] + [A(10, "acc4", 40)]
    ledger = {ev.plog.file_tag("acc4"): TODAY - timedelta(days=2)}
    plan = ev.plan_aw_cleanup(ls, TODAY, ledger)
    assert len(plan["ends"]) == 10 or ev.over_cap(10, 10)  # 全件が台帳日より前 → 上限超えで見送り
    assert plan["ends"] == [] and list(plan["skipped"].values()) == [(10, 10)]


def test_AW是正_口座なし日付なしは変更しない():
    ls = [{"id": "1", "properties": {"id_airwork": "A1", "airwork_account_login_id": ""}}]
    ls += [A(i, "acc5", 1) for i in range(2, 10)]
    ls.append({"id": "99", "properties": {"id_airwork": "A99", "airwork_account_login_id": "acc5",
                                          "saishuu_csv_kenshutsu_bi": ""}})
    plan = ev.plan_aw_cleanup(ls, TODAY)
    assert plan["ends"] == [] and plan["review"] == 2


def test_AW是正_上限超えは口座ごとに見送り():
    ls = [A(i, "acc6", 1) for i in range(1, 6)] + [A(i, "acc6", 30) for i in range(6, 11)]
    plan = ev.plan_aw_cleanup(ls, TODAY)
    assert plan["ends"] == [] and list(plan["skipped"].values()) == [(5, 10)]
