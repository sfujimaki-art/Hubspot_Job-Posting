"""AW 応募の取り込みの高速化 (2026-10-13) の検証。HubSpot・AirWork には一切アクセスしない。

- aw_prefetch: 社ごとにまとめて読んだ結果で、従来の 1 行ずつの検索と同じ答えを返す
- aw_pace: ページの移動は、並列のスレッドから来ても 1 秒以上あく
- applicant_sync: 1 行でもエラーがあれば ok=False (台帳を DONE にしない)
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync import aw_prefetch as P
from scripts.job_application_sync.fetchers import aw_pace


class _Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code, self.headers, self.text = body, code, {}, ""

    def json(self):
        return self._b


def _client(listings=(), appts=(), fail=False):
    """Search の応答を固定した PrefetchingHubSpotClient。送った本文を記録する。"""
    cli = P.PrefetchingHubSpotClient("t")
    sent = []

    def fake_post(path, body):
        sent.append((path, body))
        if fail:
            return _Resp({}, 429)
        if "/0-420/" in path:
            vals = body["filterGroups"][0]["filters"][0]["values"]
            res = [{"id": lid, "properties": {"id_airwork": j,
                                              "airwork_account_login_id": lg,
                                              "hubspot_owner_id": "O" + lid}}
                   for lid, j, lg in listings if j in vals]
        else:
            vals = body["filterGroups"][0]["filters"][1]["values"]
            res = [{"id": aid, "properties": p} for aid, p in appts
                   if p.get("oubokyuujinmemo") in vals]
        return _Resp({"total": len(res), "results": res})
    cli._post = fake_post
    return cli, sent


def _appt(jid, date="2026-10-01", email="", phone="", media="AirWork", **kw):
    return {"oubobaitaimei": media, "oubokyuujinmemo": jid, "yingmuri": date,
            "meeruadoresu": email, "denwabangou": phone, **kw}


# ---- 求人 -------------------------------------------------------------------

def test_求人はログインID一致を優先し_IDだけの検索は全件を返す():
    cli, _ = _client(listings=[("L1", "J1", "other"), ("L2", "J1", "me")])
    cli.prefetch_listings(["J1"])
    assert cli.search_listing_aw("J1", "me") == "L2"
    assert cli.search_listing_aw("J1", "nobody") is None
    assert sorted(cli.search_listing_aw_by_id("J1")) == ["L1", "L2"]   # 2件 → 呼び出し側で曖昧
    assert cli.get_listing_owner("L2") == "OL2"


def test_HubSpotに無い求人IDを欠落として返す():
    cli, _ = _client(listings=[("L1", "J1", "me")])
    cli.prefetch_listings(["J1", "J2"])
    assert cli.missing_listings(["J1", "J2"]) == ["J2"]


def test_取り込みで作った求人は検索の反映を待たずに使える():
    cli, _ = _client()
    cli.prefetch_listings(["J9"])
    assert cli.search_listing_aw("J9", "me") is None
    cli.add_listings({"J9": "L9"}, "me")
    assert cli.search_listing_aw("J9", "me") == "L9"


def test_まとめて読めなかったら従来の1行ずつの検索に戻る(monkeypatch):
    cli, _ = _client(fail=True)
    assert cli.prefetch_listings(["J1"]) is False
    assert cli.missing_listings(["J1"]) is None
    called = []
    monkeypatch.setattr(ai.RealHubSpotClient, "search_listing_aw",
                        lambda self, j, lg: called.append(j) or "LX")
    assert cli.search_listing_aw("J1", "me") == "LX" and called == ["J1"]


# ---- 重複の判定 (従来の検索と同じ条件) -------------------------------------------

def _dedup_client():
    return _client(appts=[
        ("A1", _appt("J1", email="Taro@Example.com")),
        ("A2", _appt("J1", phone="+819012345678")),
        ("A3", _appt("J2", date="2026-09-30", email="b@x.jp")),
        ("A4", _appt("J3", email="c@x.jp", media="HRハッカー")),
    ])


def test_メールは大文字小文字を区別せず_電話は数字で比べる():
    cli, _ = _dedup_client()
    cli.prefetch_appointments("AirWork", ["J1", "J2", "J3"])
    f = cli.find_existing_appointment
    assert f("AirWork", "J1", "", "taro@example.com ", "2026-10-01") == "A1"
    assert f("AirWork", "J1", "090-1234-5678", "", "2026-10-01") == "A2"
    assert f("AirWork", "J1", "", "other@x.jp", "2026-10-01") is None


def test_応募日と求人IDと媒体が違えば重複にしない():
    cli, _ = _dedup_client()
    cli.prefetch_appointments("AirWork", ["J1", "J2", "J3"])
    f = cli.find_existing_appointment
    assert f("AirWork", "J2", "", "b@x.jp", "2026-10-01") is None      # 応募日が違う
    assert f("AirWork", "J2", "", "b@x.jp", "2026-09-30") == "A3"
    assert f("AirWork", "J2", "", "b@x.jp", "") == "A3"                # 日付なしは従来どおり条件に入れない
    assert f("AirWork", "J1", "", "b@x.jp", "2026-09-30") is None      # 求人IDが違う
    assert f("AirWork", "J3", "", "c@x.jp", "2026-10-01") is None      # 媒体が違う


def test_メールも電話も無ければ重複判定しない():
    cli, _ = _dedup_client()
    cli.prefetch_appointments("AirWork", ["J1"])
    assert cli.find_existing_appointment("AirWork", "J1", "", "", "2026-10-01") is None


def test_読んでいない求人IDの応募は従来の検索に回す(monkeypatch):
    cli, _ = _dedup_client()
    cli.prefetch_appointments("AirWork", ["J1"])
    called = []
    monkeypatch.setattr(ai.RealHubSpotClient, "find_existing_appointment",
                        lambda self, *a, **k: called.append(a) or "AX")
    assert cli.find_existing_appointment("AirWork", "J9", "", "z@x.jp", "2026-10-01") == "AX"
    assert cli.find_existing_appointment("AirWork", "", "", "z@x.jp", "2026-10-01") == "AX"
    assert len(called) == 2


def test_この実行で作った応募は次の行の重複判定に入る(monkeypatch):
    cli, _ = _dedup_client()
    cli.prefetch_appointments("AirWork", ["J5"])
    monkeypatch.setattr(ai.RealHubSpotClient, "create_appointment", lambda self, p: "N1")
    cli.create_appointment(_appt("J5", email="new@x.jp"))
    assert cli.find_existing_appointment("AirWork", "J5", "", "new@x.jp", "2026-10-01") == "N1"


def test_重複時の補完は読んだ値で判定し_読めない項目は問い合わせる(monkeypatch):
    cli, _ = _client(appts=[("A1", _appt("J1", email="a@x.jp", seibetsu="男性"))])
    cli.prefetch_appointments("AirWork", ["J1"])
    got = cli.get_appointment_props("A1", ["seibetsu", "nenrei"])
    assert got == {"seibetsu": "男性", "nenrei": None}
    monkeypatch.setattr(ai.RealHubSpotClient, "get_appointment_props",
                        lambda self, i, props: {"x": "1"})
    assert cli.get_appointment_props("A1", ["x"]) == {"x": "1"}


def test_一万件を超える検索は手元で答えない():
    cli = P.PrefetchingHubSpotClient("t")
    cli._post = lambda path, body: _Resp({"total": 10001, "results": []})
    assert cli.prefetch_appointments("AirWork", ["J1"]) is False
    assert "J1" not in cli._appt_covered


# ---- 従来の検索の失敗の扱い (二重登録・上書きを防ぐ) ----------------------------

def test_重複の検索に失敗したら作らずにエラーにする():
    cli = ai.RealHubSpotClient("t")
    cli._post = lambda path, body: _Resp({}, 429)
    try:
        cli.find_existing_appointment("AirWork", "J1", "", "a@x.jp", "2026-10-01")
        raise AssertionError("例外にならなかった")
    except RuntimeError:
        pass


def test_現在値が読めないときは補完で上書きしない():
    calls = []

    class C:
        def search_listing_aw(self, *a): return "L1"
        def find_existing_appointment(self, *a, **k): return "A1"
        def get_listing_owner(self, lid): return None
        def get_appointment_props(self, i, p): return None     # 読めない
        def update_appointment(self, i, p): calls.append(p)

    row = ai.ApplicantRow(name="n", kana="", phone="", email="a@x.jp",
                          apply_date="2026-10-01", media="AirWork", media_job_id="J1",
                          raw_lineno=2, gender="男性")
    r = ai._process_applicant(row, C(), default_login_id="me")
    assert r.status == "skip_duplicate" and calls == []


# ---- ページアクセスの間隔 ---------------------------------------------------------

def test_並列のスレッドからでもページの移動は1秒以上あく(tmp_path):
    path = tmp_path / "pace.txt"
    stamps = []

    def go():
        aw_pace.wait_turn(min_interval=1.0, path=path)
        stamps.append(time.time())
    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 3 and all(g >= 0.99 for g in gaps), gaps


# ---- 部分的なエラーは DONE にしない ------------------------------------------------

def test_1行でもエラーがあれば_okにしない(monkeypatch):
    async def fake_fetch(bid, pw, out_dir):
        return Path("x.csv")
    monkeypatch.setattr(S, "_fetch_aw_csv", fake_fetch)
    monkeypatch.setattr(ai, "load_applicants_csv",
                        lambda p: [SimpleNamespace(media_job_id="J1", media="AirWork")] * 3)
    monkeypatch.setattr(ai, "append_apply_time_log", lambda p: 0)

    class _NoPrefetch:
        stats: dict = {}
        def prefetch_listings(self, j): return False
        def prefetch_appointments(self, m, j): return False
    monkeypatch.setattr(S, "_new_aw_client", lambda token: _NoPrefetch())

    def pr(st):
        return ai.ProcessResult(status=st, applicant_key="k", media="AirWork",
                                media_job_id="J1")
    monkeypatch.setattr(ai, "run_import", lambda rows, cli, default_login_id="":
                        [pr("skip_duplicate"), pr("linked"), pr("error")])
    r = S._process_aw_account_raw("A社", ["id"], "pw", Path("."), dry_run=False,
                                  allow_acquire=False, known_absent=set())
    assert r["ok"] is False and r["error_rows"] == 1
