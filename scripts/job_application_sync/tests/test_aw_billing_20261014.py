"""AirWork 有料広告実績 (週次) の取得・取込の回帰テスト (2026-10-14).

合成データのみ。AirWork にも HubSpot にもアクセスしない。
"""
from __future__ import annotations

import json
import zipfile
from datetime import date

import pytest

from scripts.job_application_sync import airwork_billing_import as imp
from scripts.job_application_sync.fetchers import aw_billing_fetcher as bf
from scripts.job_application_sync.fetchers import aw_orchestrator as orch
from scripts.job_application_sync.fetchers.aw_csv_fetcher import login_file_tag

LOGIN_A = "acct-a@example.invalid"
LOGIN_B = "acct-b@example.invalid"
HEADER = ("抽出期間,キャンペーン,求人タイトル,参照番号,表示回数,クリック率（CTR）,クリック数,"
          "応募数,利用金額,クリック単価（CPC）")


def csv_text(rows):
    return "﻿" + HEADER + "\n" + "\n".join(rows) + "\n"


def make_zip(root, week, login, rows, manifest=True):
    d = root / week
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"billing_{login_file_tag(login)}_{week}.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("all_x.csv", csv_text(rows))
        z.writestr("sponsor_x.csv", csv_text(rows))
        z.writestr("organic_x.csv", csv_text(["2025/05/05 - 2025/05/11,c,t,ZZZ,1,0,1,1,0,0"]))
    if manifest:
        bf.record_manifest(root, login)
    return path


# ---- 期間・週 -----------------------------------------------------------------------------
def test_last_full_week_is_previous_monday_to_sunday():
    assert bf.last_full_week(date(2026, 10, 14)) == date(2026, 10, 5)   # 水
    assert bf.last_full_week(date(2026, 10, 12)) == date(2026, 10, 5)   # 月
    assert bf.last_full_week(date(2026, 10, 11)) == date(2026, 9, 28)   # 日 (今週はまだ未完了)


def test_weeks_between_backfill_excludes_incomplete_week():
    ws = bf.weeks_between(date(2025, 5, 2), None, today=date(2026, 10, 14))
    assert ws[0] == date(2025, 4, 28)            # 5/2 を含む週の月曜
    assert ws[-1] == date(2026, 10, 5)
    assert all(w.weekday() == 0 for w in ws)
    assert len(ws) == len(set(ws))
    assert (ws[-1] - ws[0]).days // 7 + 1 == len(ws)


def test_weeks_between_to_mid_week_drops_that_week():
    ws = bf.weeks_between(date(2026, 9, 7), date(2026, 9, 23), today=date(2026, 10, 14))
    assert ws == [date(2026, 9, 7), date(2026, 9, 14)]
    ws = bf.weeks_between(date(2026, 9, 7), date(2026, 9, 27), today=date(2026, 10, 14))
    assert ws == [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21)]


def test_period_input_format_has_no_zero_padding_and_roundtrips():
    s = bf.format_period_input(date(2025, 5, 5), date(2025, 5, 11))
    assert s == "2025/5/5 - 2025/5/11"
    assert bf.parse_period_input(s) == (date(2025, 5, 5), date(2025, 5, 11))
    assert bf.parse_period_input("") is None


def test_parse_zip_period():
    assert bf.parse_zip_period("Job_12345_20250505-20250511_20251014120000.zip") == (
        date(2025, 5, 5), date(2025, 5, 11))
    assert bf.parse_zip_period("other.zip") is None
    assert bf.parse_zip_period("Job_1_20250511-20250505_x.zip") is None


def test_saved_zip_name_has_no_login_id():
    p = bf.saved_zip_path("/tmp/x", LOGIN_A, date(2025, 5, 5))
    assert LOGIN_A not in str(p) and "acct" not in str(p)
    assert login_file_tag(LOGIN_A) in str(p)


def test_manifest_roundtrip(tmp_path):
    bf.record_manifest(tmp_path, LOGIN_A)
    bf.record_manifest(tmp_path, LOGIN_B)
    assert bf.load_manifest(tmp_path) == {login_file_tag(LOGIN_A): LOGIN_A,
                                          login_file_tag(LOGIN_B): LOGIN_B}


# ---- オーケストレータ ------------------------------------------------------------------------
def test_resolve_billing_weeks():
    today = date(2026, 10, 14)
    assert orch.resolve_billing_weeks(today=today) == [date(2026, 10, 5)]
    assert orch.resolve_billing_weeks("2025-05-05", today=today) == [date(2025, 5, 5)]
    with pytest.raises(SystemExit):
        orch.resolve_billing_weeks("2025-05-06", today=today)        # 火曜
    with pytest.raises(SystemExit):
        orch.resolve_billing_weeks("2025-05-05", "2025-05-01", today=today)
    ws = orch.resolve_billing_weeks(frm="2026-09-28", today=today)
    assert ws == [date(2026, 9, 28), date(2026, 10, 5)]


def test_billing_status_priority():
    w = date(2026, 10, 5)
    assert orch._billing_status([(w, "saved", None), (w, "error", "x")], "full") == "error"
    assert orch._billing_status([(w, "not_ready", "x")], "collect") == "not_ready"
    assert orch._billing_status([(w, "queued", None)], "create") == "queued"
    assert orch._billing_status([(w, "empty", None), (w, "saved", None)], "full") == "ok"


# ---- CSV 解析 ------------------------------------------------------------------------------
def test_parse_number_decimals_and_blanks():
    assert imp.parse_number("50018.0") == 50018
    assert imp.parse_number("0.0684") == 0.0684
    assert imp.parse_number("1,234") == 1234
    assert imp.parse_number("") is None
    assert imp.parse_number("-") is None
    assert imp.parse_number("abc") is None


def test_parse_period_variants():
    assert imp.parse_period("2025/05/05 - 2025/05/11") == ("2025-05-05", "2025-05-11")
    assert imp.parse_period("2025/5/5～2025/5/11") == ("2025-05-05", "2025-05-11")
    assert imp.parse_period("") is None
    assert imp.parse_period("2025/05/11 - 2025/05/05") is None


def test_read_sponsor_csv_sums_same_ref_and_week_and_keeps_blank_as_none():
    text = csv_text([
        "2025/05/05 - 2025/05/11,c1,t,100,1000,0.01,10,1,5000.0,500.0",
        "2025/05/05 - 2025/05/11,c2,t,100,500,0.02,5,2,2500.5,500",
        "2025/05/05 - 2025/05/11,c1,t,200,0,,,,,",
    ])
    got = imp.read_sponsor_csv(text.lstrip("﻿"), None)
    a = got[("100", "2025-05-05")]
    assert (a["impressions"], a["clicks"], a["applications"]) == (1500, 15, 3)
    assert a["cost_yen"] == 7500.5
    b = got[("200", "2025-05-05")]
    assert b["impressions"] == 0 and b["cost_yen"] is None and b["clicks"] is None


def test_read_sponsor_csv_uses_fallback_week_when_period_unreadable():
    text = csv_text(["不明,c,t,100,10,0,1,0,100,10"]).lstrip("﻿")
    got = imp.read_sponsor_csv(text, ("2025-05-05", "2025-05-11"))
    assert ("100", "2025-05-05") in got
    assert imp.read_sponsor_csv(text, None) == {}


def test_load_dir_reads_bom_zip_sponsor_only_and_drops_zero_weeks(tmp_path):
    make_zip(tmp_path, "2025-05-05", LOGIN_A, [
        "2025/05/05 - 2025/05/11,c,t,100,1000,0,10,1,5000.0,0",
        "2025/05/05 - 2025/05/11,c,t,300,0,,0,0,0.0,",
    ])
    data, stats = imp.load_dir(tmp_path)
    assert list(data) == [(LOGIN_A, "100")]          # organic の ZZZ は読まない / 実績 0 の 300 は落ちる
    assert data[(LOGIN_A, "100")]["2025-05-05"]["cost_yen"] == 5000
    assert stats["files"] == 1


def test_load_dir_skips_unknown_account(tmp_path):
    make_zip(tmp_path, "2025-05-05", LOGIN_A, ["2025/05/05 - 2025/05/11,c,t,100,1,0,0,0,1,0"],
             manifest=False)
    data, stats = imp.load_dir(tmp_path)
    assert data == {} and stats["unknown_account"] == 1


# ---- 突合 (口座 + id) ----------------------------------------------------------------------
def rec(rid, ref, login, perf=None):
    return {"id": rid, "properties": {"id_airwork": ref, "airwork_account_login_id": login,
                                      "airwork_weekly_perf": perf}}


def test_same_id_in_two_accounts_does_not_cross_match():
    records = [rec("1", "100", LOGIN_A), rec("2", "100", LOGIN_B), rec("3", "100", "other")]
    found = imp.match_records(records, [(LOGIN_A, "100")])
    assert list(found) == [(LOGIN_A, "100")]
    assert [r["id"] for r in found[(LOGIN_A, "100")]] == ["1"]
    assert imp.match_records(records, [("none", "100")]) == {}


class FakeHub:
    def __init__(self, records):
        self.records, self.written, self.searched = records, [], []

    def find_by_refs(self, refs):
        self.searched.append(list(refs))
        return [r for r in self.records if r["properties"]["id_airwork"] in refs]

    def batch_update(self, inputs):
        self.written.extend(inputs)
        return True


def test_run_writes_only_matching_account_and_is_idempotent(tmp_path):
    make_zip(tmp_path, "2025-05-05", LOGIN_A, ["2025/05/05 - 2025/05/11,c,t,100,1000,0,10,2,6000.0,0"])
    make_zip(tmp_path, "2025-05-05", LOGIN_B, ["2025/05/05 - 2025/05/11,c,t,100,50,0,1,0,300.0,0"])
    hub = FakeHub([rec("1", "100", LOGIN_A), rec("2", "100", LOGIN_B), rec("9", "999", LOGIN_A)])
    out = imp.run(hub, tmp_path, apply=True)
    assert out["matched"] == 2 and out["unmatched"] == 0 and out["written"] == 2
    by_id = {w["id"]: w["properties"] for w in hub.written}
    assert by_id["1"]["airwork_cost_total"] == 6000 and by_id["2"]["airwork_cost_total"] == 300
    assert "9" not in by_id
    # 書いた値を読み戻して再実行 → 変化なし
    for rid, props in by_id.items():
        for r in hub.records:
            if r["id"] == rid:
                r["properties"].update({k: str(v) for k, v in props.items()})
    hub.written.clear()
    again = imp.run(hub, tmp_path, apply=True)
    assert again["skipped_same"] == 2 and again["written"] == 0 and hub.written == []


def test_run_dry_run_does_not_write_and_limit(tmp_path):
    make_zip(tmp_path, "2025-05-05", LOGIN_A, [
        "2025/05/05 - 2025/05/11,c,t,100,1000,0,10,2,6000.0,0",
        "2025/05/05 - 2025/05/11,c,t,101,1000,0,10,2,6000.0,0"])
    hub = FakeHub([rec("1", "100", LOGIN_A), rec("2", "101", LOGIN_A)])
    out = imp.run(hub, tmp_path, apply=False, limit=1)
    assert out["mode"] == "dry-run" and out["to_write"] == 1 and out["written"] == 0
    assert hub.written == []


def test_ambiguous_key_is_not_written(tmp_path):
    make_zip(tmp_path, "2025-05-05", LOGIN_A, ["2025/05/05 - 2025/05/11,c,t,100,1000,0,10,2,6000.0,0"])
    hub = FakeHub([rec("1", "100", LOGIN_A), rec("2", "100", LOGIN_A)])
    out = imp.run(hub, tmp_path, apply=True)
    assert out["errors"] == {"ambiguous_key": 1} and hub.written == []


# ---- マージ / 要約 -------------------------------------------------------------------------
def row(start, cost, apps=0, imps=10):
    end = date.fromisoformat(start).toordinal() + 6
    return {"period_start": start, "period_end": date.fromordinal(end).isoformat(),
            "impressions": imps, "clicks": 1, "cost_yen": cost, "applications": apps}


def test_merge_replaces_same_week_and_sorts():
    existing = {"rows": [row("2025-05-12", 100), row("2025-05-05", 50)]}
    merged = imp.merge_rows(existing, [row("2025-05-12", 999), row("2025-05-19", 7)])
    assert [r["period_start"] for r in merged] == ["2025-05-05", "2025-05-12", "2025-05-19"]
    assert merged[1]["cost_yen"] == 999 and merged[0]["cost_yen"] == 50


def test_merge_with_broken_existing_rebuilds():
    assert imp.parse_existing("not json") is None
    assert imp.parse_existing('{"rows": 1}') is None
    assert imp.merge_rows(None, [row("2025-05-05", 1)]) == [row("2025-05-05", 1)]


def test_summarize_last4_weeks_window_and_cpa():
    rows = [row("2025-05-05", 1000, 1), row("2025-05-12", 2000, 0),
            row("2025-05-19", 3000, 2), row("2025-05-26", 4000, 1), row("2025-06-02", 5000, 0)]
    s = imp.summarize(rows)
    # 最新 6/2 を含む 4 週 = 5/12,5/19,5/26,6/2 (5/5 は外れる)
    assert s["airwork_cost_last4w"] == 14000
    assert s["airwork_applications_last4w"] == 3
    assert s["airwork_cpa_last4w"] == round(14000 / 3)
    assert s["airwork_cost_last_week"] == 5000
    assert s["airwork_last_week_start"] == "2025-06-02"
    assert s["airwork_cost_total"] == 15000


def test_summarize_cpa_empty_when_no_applications():
    assert imp.summarize([row("2025-05-05", 1000, 0)])["airwork_cpa_last4w"] == ""
    assert imp.summarize([]) is None


def test_collection_json_shape_and_source():
    wanted = imp.plan_listing("100", {}, [row("2025-05-05", 10, 1)], "2026-10-14T00:00:00Z")
    data = json.loads(wanted["airwork_weekly_perf"])
    assert data["schema_version"] == 1 and data["source"] == "airwork" and data["job_id"] == "100"
    assert data["rows"][0]["cost_yen"] == 10


# ---- 有料広告が無かった週に 0 を書く (2026-10-19) ------------------------------------------
class OpenHub(FakeHub):
    def __init__(self, records, open_records):
        super().__init__(records)
        self.open_records, self.open_logins = open_records, []

    def open_listings(self, logins):
        self.open_logins.append(list(logins))
        return [r for r in self.open_records
                if r["properties"]["airwork_account_login_id"] in logins]


def orec(rid, ref, login, created="2025-01-01T00:00:00Z", perf=None):
    r = rec(rid, ref, login, perf)
    r["properties"].update({"kyuujin_status": "公開中", "hs_createdate": created})
    return r


def test_zero_fill_marks_open_listings_without_paid_ads(tmp_path):
    # 口座 A: 5/05 の週は ZIP あり (求人 100 だけ有料)。口座 B: 5/05 の週は「実績なし」
    make_zip(tmp_path, "2025-05-05", LOGIN_A, ["2025/05/05 - 2025/05/11,c,t,100,1000,0,10,2,6000.0,0"])
    paid = rec("1", "100", LOGIN_A)
    open_ = [orec("1", "100", LOGIN_A), orec("2", "101", LOGIN_A), orec("3", "200", LOGIN_B),
             orec("4", "201", LOGIN_B, created="2025-06-01T00:00:00Z")]   # 週のあとに作った求人
    hub = OpenHub([paid], open_)
    out = imp.run(hub, tmp_path, apply=True, zero_fill=True,
                  empty_weeks={LOGIN_B: {"2025-05-05"}})
    by_id = {w["id"]: w["properties"] for w in hub.written}
    assert set(by_id) == {"1", "2", "3"}            # 4 は作る前の週なので書かない
    assert by_id["1"]["airwork_cost_last_week"] == 6000   # 実績は 0 で上書きしない
    for rid in ("2", "3"):
        assert by_id[rid]["airwork_cost_last_week"] == 0
        assert by_id[rid]["airwork_applications_last4w"] == 0
        assert by_id[rid]["airwork_last_week_start"] == "2025-05-05"
        perf = json.loads(by_id[rid]["airwork_weekly_perf"])
        assert perf["rows"][0]["no_paid_ads"] is True and perf["rows"][0]["cost_yen"] == 0
    assert out["zero_filled_to_write"] == 2
    # 書いた値を読み戻して再実行 → 変化なし (毎回書かない)
    for r in hub.open_records + hub.records:
        if r["id"] in by_id:
            r["properties"].update({k: str(v) for k, v in by_id[r["id"]].items()})
    hub.written.clear()
    again = imp.run(hub, tmp_path, apply=True, zero_fill=True,
                    empty_weeks={LOGIN_B: {"2025-05-05"}})
    assert hub.written == [] and again["to_write"] == 0


def test_zero_fill_does_not_overwrite_an_existing_paid_week(tmp_path):
    """ZIP が消えていても、前に書いた実績の週を 0 にしない。"""
    existing = json.dumps({"schema_version": 1, "source": "airwork", "job_id": "300",
                           "captured_at": "x", "rows": [row("2025-05-05", 900)]})
    hub = OpenHub([], [orec("5", "300", LOGIN_B, perf=existing)])
    imp.run(hub, tmp_path, apply=True, zero_fill=True, empty_weeks={LOGIN_B: {"2025-05-05"}})
    # (要約が空だったので要約だけ書く。週の値は 900 のまま)
    assert [w["properties"]["airwork_cost_last_week"] for w in hub.written] == [900]
    assert json.loads(hub.written[0]["properties"]["airwork_weekly_perf"])["rows"][0]["cost_yen"] == 900


def test_zero_fill_is_off_by_default_and_empty_file_loader(tmp_path):
    hub = OpenHub([], [orec("3", "200", LOGIN_B)])
    imp.run(hub, tmp_path, apply=True, empty_weeks={LOGIN_B: {"2025-05-05"}})
    assert hub.written == [] and hub.open_logins == []
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({LOGIN_B: ["2025-05-05", "bad"]}), encoding="utf-8")
    assert imp.load_empty_weeks(p) == {LOGIN_B: {"2025-05-05"}}
    assert imp.load_empty_weeks(tmp_path / "none.json") == {}
