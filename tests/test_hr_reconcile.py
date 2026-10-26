"""hr_reconcile の判定 (架空データのみ)。"""
import json
from datetime import datetime, timedelta

from scripts.job_application_sync import hr_reconcile as hr
from scripts.job_application_sync.applicant_import import ApplicantRow

NOW = datetime(2026, 10, 26, 11, 0, tzinfo=hr.JST)


def row(hid, date="2026-10-24", email="a@example.com", job="J1"):
    return ApplicantRow(name="架空 太郎", kana="カクウ タロウ", phone="090-0000-0000",
                        email=email, apply_date=date, media="HRハッカー",
                        media_job_id=job, hr_applicant_id=hid)


def raw(hid, at, route="4", shop="S1"):
    return {"応募者id": hid, "応募日時": at, "応募経路": route, "店舗ID": shop,
            "名前": "架空 太郎", "電話番号": "090-0000-0000", "メールアドレス": "a@example.com"}


def test_classify_splits_in_hubspot_duplicate_new_and_missing():
    rows = [row("1"), row("2"), row("3", date="2026-10-26"), row("4"), row("")]
    raws = {"1": raw("1", "2026-10-24 10:00:00"), "2": raw("2", "2026-10-24 11:00:00"),
            "3": raw("3", "2026-10-26 10:30:00"), "4": raw("4", "2026-10-24 23:34:00")}
    dup = {"2"}
    res = hr.classify(rows, raws, existing_ids={"1"},
                      find_existing=lambda r: "999" if r.hr_applicant_id in dup else None,
                      now=NOW)
    assert (res["checked"], res["in_hubspot"], res["duplicate"], res["too_new"], res["no_id"]) \
        == (5, 1, 1, 1, 1)
    assert [r.hr_applicant_id for r in res["missing"]] == ["4"]


def test_too_new_boundary_is_60_minutes():
    raws = {"5": raw("5", (NOW - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S"))}
    res = hr.classify([row("5")], raws, set(), lambda r: None, NOW)
    assert len(res["missing"]) == 1      # ちょうど60分は取りこぼしとして数える


def test_describe_has_no_personal_data():
    raws = {"4": raw("4", "2026-10-24 23:34:00", route="15", shop="S9")}
    items = hr.describe([row("4")], raws)
    assert items == [{"applied": "2026-10-24 23:34", "route": "15", "shop_id": "S9",
                      "key": hr.short_key("4")}]
    text = json.dumps(items, ensure_ascii=False)
    for secret in ("架空", "090-0000-0000", "a@example.com", '"4"'):
        assert secret not in text


class _Resp:
    def __init__(self, code, js=None):
        self.status_code = code
        self._js = js or {}

    def json(self):
        return self._js


def test_fetch_existing_ids_chunks_pages_and_retries(monkeypatch):
    monkeypatch.setattr(hr.time, "sleep", lambda s: None)
    calls = []
    ids = [str(i) for i in range(150)]

    def post(body):
        calls.append(body)
        vals = body["filterGroups"][0]["filters"][0]["values"]
        assert len(vals) <= 100
        if len(calls) == 1:
            return _Resp(429)
        if vals[0] == "0" and "after" not in body:
            return _Resp(200, {"results": [{"properties": {"hr_oubosha_id": "0"}}],
                               "paging": {"next": {"after": "x"}}})
        if vals[0] == "0":
            return _Resp(200, {"results": [{"properties": {"hr_oubosha_id": "1"}}]})
        return _Resp(200, {"results": [{"properties": {"hr_oubosha_id": "120"}}]})

    assert hr.fetch_existing_ids("t", ids, post=post) == {"0", "1", "120"}
    assert len(calls) == 4     # 429 の再試行 1 + 1 束目 2 ページ + 2 束目 1


def test_read_raw_by_id_reads_shift_jis(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("応募者id,応募日時,応募経路,店舗ID\n7,2026-10-24 23:34:00,4,S1\n".encode("cp932"))
    got = hr.read_raw_by_id(p)
    assert got["7"]["応募経路"] == "4"
    assert hr.applied_at(got["7"]) == datetime(2026, 10, 24, 23, 34, tzinfo=hr.JST)


def _run_main(monkeypatch, tmp_path, argv, imported_ok=True):
    from scripts.job_application_sync import applicant_import as ai
    p = tmp_path / "a.csv"
    p.write_bytes(("応募者id,応募日時,応募経路,店舗ID\n"
                   "1,2026-10-24 10:00:00,4,S1\n4,2026-10-24 23:34:00,15,S2\n").encode("cp932"))
    store = {"1"}
    created = []

    class FakeCli:
        def __init__(self, token):
            pass

        def find_existing_appointment(self, *a, **k):
            return None

    def fake_import(rows, cli):
        out = []
        for r in rows:
            created.append(r.hr_applicant_id)
            if imported_ok:
                store.add(r.hr_applicant_id)
            out.append(type("R", (), {"status": "linked"})())
        return out

    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "t")
    monkeypatch.delenv("SLACK_APPLICANT_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr(ai, "load_applicants_csv", lambda path: [row("1"), row("4")])
    monkeypatch.setattr(ai, "RealHubSpotClient", FakeCli)
    monkeypatch.setattr(ai, "run_import", fake_import)
    monkeypatch.setattr(hr, "fetch_existing_ids", lambda token, ids, post=None: store & set(ids))
    sent = []
    monkeypatch.setattr(hr, "slack", sent.append)
    status = tmp_path / "s.json"
    assert hr.main(argv + ["--slack", "--csv", str(p), "--status-file", str(status)]) == 0
    st = json.loads(status.read_text(encoding="utf-8"))
    st["_slack"] = sent
    return st, created


def test_main_dry_run_reports_but_does_not_import(monkeypatch, tmp_path):
    st, created = _run_main(monkeypatch, tmp_path, ["--dry-run", "--import-missing"])
    assert (st["missing"], st["remaining"], created, st["_slack"]) == (1, 1, [], [])
    assert st["missing_items"][0]["route"] == "15"


def test_main_actual_imports_and_rechecks(monkeypatch, tmp_path):
    st, created = _run_main(monkeypatch, tmp_path, ["--actual", "--import-missing"])
    assert (st["missing"], st["added"], st["remaining"], created) == (1, 1, 0, ["4"])
    assert st["imported"] == {"status": {"linked": 1}}
    assert st["_slack"] == ["照合で 1 件を追加登録"]


def test_main_actual_still_missing_is_remaining(monkeypatch, tmp_path):
    st, _ = _run_main(monkeypatch, tmp_path, ["--actual", "--import-missing"], imported_ok=False)
    assert st["remaining"] == 1 and st["remaining_items"][0]["key"] == hr.short_key("4")
    assert "架空" not in json.dumps(st, ensure_ascii=False)
    assert st["_slack"] == []      # 残りの知らせはワークフローが Issue を立てるときだけ


def test_import_missing_writes_detail_page_fields_for_created_only(monkeypatch):
    from scripts.job_application_sync import applicant_import as ai
    from scripts.job_application_sync import hr_applicant_fields as hrf
    res = [type("R", (), {"status": "linked", "hr_applicant_id": "4", "appointment_id": "A4"})(),
           type("R", (), {"status": "error", "hr_applicant_id": "5", "appointment_id": None})()]
    monkeypatch.setattr(ai, "run_import", lambda rows, cli: res)
    monkeypatch.setattr(hrf, "active_page_fields", lambda known: {"memo": "hr_memo"})
    wrote = []
    monkeypatch.setattr(hrf, "apply_page_fields",
                        lambda cli, appt, parsed, fields: wrote.append(appt) or "written")
    cli = type("C", (), {"known_appointment_props": lambda self: {"hr_memo"}})()
    detail = type("D", (), {"parsed": {"4": {"memo": "x"}, "5": {"memo": "y"}}})()
    out = hr.import_missing([row("4"), row("5")], cli, detail)
    assert wrote == ["A4"]
    assert out == {"status": {"linked": 1, "error": 1}, "detail": {"written": 1, "skipped": 1}}


def test_zero_added_sends_no_slack(monkeypatch, tmp_path):
    from scripts.job_application_sync import applicant_import as ai
    p = tmp_path / "a.csv"
    p.write_bytes("応募者id,応募日時,応募経路,店舗ID\n1,2026-10-24 10:00:00,4,S1\n".encode("cp932"))
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "t")
    monkeypatch.setattr(ai, "load_applicants_csv", lambda path: [row("1")])
    monkeypatch.setattr(ai, "RealHubSpotClient", lambda token: None)
    monkeypatch.setattr(hr, "fetch_existing_ids", lambda token, ids, post=None: {"1"})
    sent = []
    monkeypatch.setattr(hr, "slack", sent.append)
    assert hr.main(["--actual", "--import-missing", "--slack", "--csv", str(p)]) == 0
    assert sent == []
