# -*- coding: utf-8 -*-
"""一覧から消えた求人を、公開ページで 2 回確かめてから公開終了にする (2026-10-23 ユーザー決定 案A)。
復活した時 (エクスポートに再び公開中で載った) も踏まえる。通信はすべて偽物。"""
from __future__ import annotations

from scripts.job_application_sync import aw_end_confirm as C
from scripts.job_application_sync import aw_listing_images as I
from scripts.job_application_sync.fetchers import aw_orchestrator as orch

SITE = "https://arwrk.net/recruit/acme"


def _l(hid, job):
    return {"id": str(hid), "properties": {"id_airwork": str(job),
                                           "url_airwork": f"{SITE}/{job}/"}}


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield b"<html></html>"


class _Sess:
    def __init__(self, status_by_job, default=200):
        self.status, self.default, self.urls = status_by_job, default, []

    def get(self, url, **k):
        self.urls.append(url)
        job = url.rstrip("/").rsplit("/", 1)[-1]
        return _Resp(self.status.get(job, self.default))


def _check(sess, cands, controls):
    return C.check_pages(cands, controls, session=sess, sleep=lambda s: None, clock=lambda: 0.0)


def test_ネットが生きていて2回目も無い求人だけ公開終了にする(tmp_path):
    led = tmp_path / "led.json"
    cands, controls = [_l(1, 11), _l(2, 12), _l(3, 13)], [_l(9, 99)]
    sess = _Sess({"11": 404, "12": 404, "13": 200})
    ch = _check(sess, cands, controls)
    assert ch["net_ok"] and ch["gone"] == {"1", "2"} and ch["alive"] == {"3"}
    assert sess.urls[0] == f"{SITE}/99/", "最初にエクスポートに載っている求人でネットを確かめる"
    d = C.decide("L", ["1", "2", "3"], ch, now=1000.0, path=led)
    assert d["confirm"] == set() and d["first"] == 2, "1 回目は記録するだけ"

    # 6 時間後: まだ早い
    d = C.decide("L", ["1", "2", "3"], ch, now=1000.0 + 6 * 3600, path=led)
    assert d["confirm"] == set()
    # 次の夜: 求人 2 はページが開いた → 外す。求人 1 だけ公開終了
    ch2 = _check(_Sess({"11": 404, "12": 200}), cands[:2], controls)
    d = C.decide("L", ["1", "2"], ch2, now=1000.0 + 24 * 3600, path=led)
    assert d["confirm"] == {"1"}
    assert "2" not in C._read(led)["pending"]


def test_ほかのページも開けなければ何も決めない(tmp_path):
    led = tmp_path / "led.json"
    sess = _Sess({}, default=404)
    ch = _check(sess, [_l(1, 11)], [_l(9, 99)])
    assert ch["net_ok"] is False and ch["gone"] == set()
    assert sess.urls == [f"{SITE}/99/"], "ネットが確かめられなければ候補のページは見ない"
    C.decide("L", ["1"], ch, now=0.0, path=led)
    assert C._read(led)["pending"] == {}


def test_403や通信失敗は無いと決めない(tmp_path, monkeypatch):
    ch = _check(_Sess({"11": 403, "99": 200}), [_l(1, 11)], [_l(9, 99)])
    assert ch["gone"] == set() and ch["unknown"] == 1


def test_エクスポートに戻った求人は台帳から外れ_終了した求人の復活を数える(tmp_path):
    led = tmp_path / "led.json"
    ch = {"net_ok": True, "gone": {"1", "2"}, "alive": set(), "unknown": 0}
    C.decide("L", ["1", "2"], ch, now=0.0, path=led)
    # 求人 2 がエクスポートに戻った (候補でなくなった)
    C.decide("L", ["1"], {"net_ok": True, "gone": {"1"}, "alive": set()}, now=13 * 3600.0, path=led)
    assert set(C._read(led)["pending"]) == {"1"}
    C.mark_ended("L", ["1"], now=13 * 3600.0, path=led)
    assert "1" in C._read(led)["ended"] and C._read(led)["pending"] == {}
    # 求人 1 が再び公開中に → 数えて台帳から外す。関係ない求人は数えない
    assert C.note_reopened(["1", "7"], path=led) == 1
    assert C._read(led)["ended"] == {}


def test_dry_runでは台帳に書かない(tmp_path):
    led = tmp_path / "led.json"
    C.decide("L", ["1"], {"net_ok": True, "gone": {"1"}, "alive": set()}, now=0.0, path=led,
             dry_run=True)
    assert not led.exists()


def test_対象一覧に確認の結果と復活が出る():
    rows = orch._report_rows([
        {"login_id": "abc123", "company": "A社", "status": "ok",
         "result": {"ends_skipped_cap": 5, "ends_reopened": 1,
                    "end_confirm": {"net_ok": True, "gone": 3, "alive": 1, "unknown": 1,
                                    "first_seen": 2, "confirmed": 2}}},
        {"login_id": "def456", "company": "B社", "status": "ok",
         "result": {"ends_skipped_cap": 4, "end_confirm": {"net_ok": False}}}], "求人の巡回(full)")
    text = repr(rows)
    assert "次の夜にもう一度確かめて" in text and "2件を公開終了にしました" in text
    assert "再び公開中になったので公開中に戻しました" in text
    assert "ネットの不調の可能性" in text
    assert "abc123" not in text


def test_interactionで止まった社もその夜は試し直さない():
    e = ("ログイン後 dashboards に遷移せず: エアワークの利用登録・ログイン選択の画面です "
         "(login_id=x, last_url=https://ats.rct.airwork.net/interaction)")
    assert orch._is_login_stop(e)
    assert not orch._is_login_stop("timeout while downloading")



from scripts.job_application_sync.tests.test_aw_end_missing_20261013 import (  # noqa: E402,F401
    LOGIN, hub, make_xlsx,
)


def test_取り込み_上限超えでも確かめた求人だけ公開終了にし_台帳に残す(hub, monkeypatch, tmp_path):
    import time as _t

    from scripts.job_application_sync import airwork_import as awi
    from scripts.job_application_sync import listing_stage as stage

    h = hub([(f"J{i}", LOGIN, "公開中", stage.STAGE_BOSHUU) for i in range(1, 11)])
    ids = {v["id_airwork"]: k for k, v in h.db.items()}
    seen = {}

    def fake_check(cands, controls, site=""):
        seen.update(cands=sorted(c["id"] for c in cands), controls=[c["id"] for c in controls])
        return {"net_ok": True, "gone": {ids["J2"], ids["J3"]}, "alive": {ids["J4"]}, "unknown": 0}
    monkeypatch.setattr(C, "check_pages", fake_check)
    C.decide(LOGIN, [ids["J2"]], {"net_ok": True, "gone": {ids["J2"]}, "alive": set()},
             now=_t.time() - 24 * 3600)   # 前の夜に 1 回目を見た
    s = awi.run(make_xlsx(tmp_path / "aw_AAA111_1.xlsx", [("J1", "02")]), LOGIN, dry_run=False)
    assert seen["controls"] == [ids["J1"]] and len(seen["cands"]) == 9
    assert s["ends_ok"] == 1 and s["ends_skipped_cap"] == 8
    assert s["end_confirm"]["confirmed"] == 1 and s["end_confirm"]["first_seen"] == 1
    assert h.db[ids["J2"]]["kyuujin_status"] == "公開終了"
    assert h.db[ids["J3"]]["kyuujin_status"] == "公開中", "1 回目の J3 はまだ終了にしない"
    led = C._read(C.LEDGER_PATH)
    assert ids["J2"] in led["ended"] and ids["J3"] in led["pending"]

    # J2 が媒体で再び公開中に → 通常の更新で公開中に戻り、復活として数える
    s = awi.run(make_xlsx(tmp_path / "aw_AAA111_2.xlsx", [("J1", "02"), ("J2", "02")]), LOGIN,
                dry_run=False)
    assert h.db[ids["J2"]]["kyuujin_status"] == "公開中" and s["ends_reopened"] == 1
