"""応募取り込みのエラーを減らす修正 (2026-10-08) の回帰テスト.

A. 認証なしの社は1runの枠を使わず、シートの認証値が変わるまで再試行しない。
   前回失敗した社は処理順を後ろへ回す。
B. 会社名の書き方の違い (区切りの「_」・法人格の位置・末尾の「御中」) を吸収する。
   事業所名の違いは吸収しない。
C. 媒体の求人一覧に無い求人への応募は、未紐付けと別の欄で数える。
   同じ求人IDのために自己修復 (求人の全件取得) を繰り返さない。
D. 回収runの台帳を、定期runの台帳に updated の新しい方で混ぜる。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_queue as aq
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync.applicant_queue import (
    AccountResolver, QueueItem, _norm, _norm_company)


# ---- A. 認証なしの判定 -------------------------------------------------------

@pytest.mark.parametrize("v", ["", "  ", "ー", "-", "―", "‐", "−", "－", "ｰ", " ー ", "　―"])
def test_空白とダッシュ類だけは認証なし(v):
    assert S.is_blank_cred(v)
    assert not S.has_aw_auth([v], "pw12345")
    assert not S.has_aw_auth(["id12345"], v)


def test_IDとPWがあれば認証あり():
    assert S.has_aw_auth(["ー", "id12345"], "pw12345")     # 2つ目のIDが使える
    assert not S.has_aw_auth([], "pw12345")
    assert not S.is_blank_cred("a-b")                       # ダッシュを含むだけの値は空でない


def test_PWの前後の空白は変えずに指紋が変わる():
    # PW の空白除去はしない。値が変われば指紋も変わる
    assert S.auth_fingerprint(["id"], "pw") != S.auth_fingerprint(["id"], "pw ")
    assert S.auth_fingerprint(["ー"], "ー") == S.auth_fingerprint(["ー"], "ー")
    assert "pw" not in S.auth_fingerprint(["id"], "pw")


def test_取得処理はダッシュ類のIDでログインを試さない(monkeypatch):
    called = []

    async def fake_fetch(bid, pw, out_dir):
        called.append(bid)
        raise RuntimeError("should not be called")
    monkeypatch.setattr(S, "_fetch_aw_csv", fake_fetch)
    r = S._process_aw_account_raw("A社", ["―", "‐", " "], "x", Path("."), dry_run=False)
    assert called == [] and r["ok"] is False and r["error"] == "有効なB系IDなし"


# ---- A. run() の枠と順番 ------------------------------------------------------

def _aw_item(rid: str, company: str) -> QueueItem:
    return QueueItem(row_id=rid, media_type="Airワーク", login_id=f"{rid}@x.jp",
                     company=company, login_ids=[f"{rid}@x.jp"])


class _FakeResolver:
    def __init__(self, accounts: dict) -> None:
        self.accounts = accounts            # 会社名 -> (b_ids, b_pw)

    def build(self):
        return self

    def resolve(self, it):
        if it.company not in self.accounts:
            return None
        bids, pw = self.accounts[it.company]
        return aq.ResolvedAccount(company=it.company, b_ids=list(bids), b_pw=pw,
                                  closed=False, matched_by="test")


_ORIG_LOCK = S.Lock


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """run() をシート・媒体・HubSpot なしで回す。台帳は run をまたいで同じものを使う。"""
    state = SimpleNamespace(items=[], accounts={}, calls=[], slack=[], ok={},
                            ledger=S.Ledger(tmp_path / "ledger.json"))
    monkeypatch.setattr(S, "Lock", lambda: _ORIG_LOCK(tmp_path / "lock"))
    monkeypatch.setattr(S, "Ledger", lambda merge_from="": state.ledger)
    monkeypatch.setattr(aq, "read_new_items_from_sheet1",
                        lambda **k: list(state.items))
    monkeypatch.setattr(aq, "AccountResolver", lambda: _FakeResolver(state.accounts))

    def fake_process(company, b_ids, b_pw, out_dir, dry_run=True,
                     allow_acquire=False, known_absent=None):
        state.calls.append(company)
        ok = state.ok.get(company, True)
        return {"ok": ok, "linked": 0, "unlinked": 0, "no_listing_old": 0, "dup": 0,
                "jobs_fetched": 0, "error": "" if ok else "login_failed", "login_id": ""}
    monkeypatch.setattr(S, "process_aw_account", fake_process)
    monkeypatch.setattr(S, "slack_notify",
                        lambda message="", dry_run=False: state.slack.append(message))
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path / "sess")
    return state


def _run(limit=None):
    return S.run(dry_run=True, source="sheet1", media_filter="AW",
                 hr_cutoff_iso="2026-08-01", limit_accounts=limit)


def test_認証なしの社は枠を使わずログインもしない(harness):
    harness.items = [_aw_item("r1", "認証なし社"), _aw_item("r2", "B社"), _aw_item("r3", "C社")]
    harness.accounts = {"認証なし社": (["ー"], "ー"), "B社": (["b"], "pw"), "C社": (["c"], "pw")}
    s = _run(limit=2)
    assert harness.calls == ["B社", "C社"]           # 認証なしが1枠を取らない
    assert s["no_auth"] == 1 and s["accounts"] == 2
    assert harness.ledger.status("r1") == "NEW"       # 試行回数も増やさない
    assert len([m for m in harness.slack if "認証なし社" in m]) == 1


def test_認証なしの通知は値が変わるまで繰り返さない(harness):
    harness.items = [_aw_item("r1", "認証なし社")]
    harness.accounts = {"認証なし社": ([""], "")}
    _run()
    _run()
    assert len([m for m in harness.slack if "認証なし社" in m]) == 1
    harness.accounts = {"認証なし社": (["ー"], "")}   # 値が変わった (まだ認証なし)
    _run()
    assert len([m for m in harness.slack if "認証なし社" in m]) == 2
    assert harness.calls == []


def test_認証が入ったら次の回でログインする(harness):
    harness.items = [_aw_item("r1", "A社")]
    harness.accounts = {"A社": (["―"], "―")}
    _run()
    assert harness.calls == []
    harness.accounts = {"A社": (["a-login"], "secret")}
    s = _run()
    assert harness.calls == ["A社"] and s["no_auth"] == 0
    assert harness.ledger.status("r1") == "DONE"


def test_前回失敗した社は後ろへ回す(harness):
    harness.items = [_aw_item("r1", "失敗社"), _aw_item("r2", "B社"), _aw_item("r3", "C社")]
    harness.accounts = {k: (["id"], "pw") for k in ("失敗社", "B社", "C社")}
    harness.ok = {"失敗社": False}
    _run(limit=1)
    assert harness.calls == ["失敗社"]               # 初回はシートの順
    harness.calls.clear()
    harness.ok = {}
    _run(limit=1)
    assert harness.calls == ["B社"]                  # 失敗社は後ろへ
    harness.calls.clear()
    _run(limit=3)
    assert harness.calls == ["C社", "失敗社"]       # B社はDONE。失敗社は最後だが外さない


def test_成功したら後ろへ回す印を消す():
    order = S.order_aw_groups([("AW::x", 1), ("AW::y", 2), ("AW::z", 3)],
                              lambda k: k == "AW::x")
    assert [k for k, _ in order] == ["AW::y", "AW::z", "AW::x"]


# ---- B. 会社名の書き方の違い ------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("サンプル物流 株式会社 新潟工場", "サンプル物流株式会社_新潟工場"),
    ("株式会社テスト産業埼玉工場", "株式会社テスト産業_埼玉工場"),
    ("見本製紙株式会社 東工場", "見本製紙株式会社＿東工場"),
    ("ロジテスト北海道", "ロジテスト北海道株式会社"),
    ("例示交通株式会社", "株式会社 例示交通"),
    ("例示鋼業株式会社御中", "例示鋼業株式会社"),
    ("㈱見本運輸", "見本運輸株式会社"),
])
def test_書き方の違いだけなら同じキー(a, b):
    assert _norm_company(a) == _norm_company(b)


@pytest.mark.parametrize("a,b", [
    ("サンプル運輸 甲営業所", "サンプル運輸 乙支店"),
    ("株式会社A_新潟工場", "株式会社A_埼玉工場"),
    ("見本産業株式会社 北支店", "見本産業株式会社 南支店"),
    ("株式会社A", "株式会社A 新潟工場"),            # 前方一致はしない
])
def test_事業所名が違えば一致しない(a, b):
    assert _norm_company(a) != _norm_company(b)


def test_御中は末尾だけ落とす():
    assert _norm_company("御中商事") == "御中商事".lower()


def _resolver(rows):
    COLS = {"closed": 0, "reclog": 1, "aid": 2, "bid": 3, "bpw": 4, "alias": 5, "comp": 6}
    r = AccountResolver()
    r.cols = dict(COLS)
    for row in rows:
        if row[6].strip():
            r.idx_comp[_norm(row[6])] = row
            k = _norm_company(row[6])
            if k:
                r.idx_comp_norm.setdefault(k, []).append(row)
    return r


def _srow(comp, bid="id1", pw="pw1"):
    return ["FALSE", "", "", bid, pw, "", comp]


def test_法人格の位置違いで1行に決まれば突合する():
    r = _resolver([_srow("株式会社例示運輸"), _srow("例示物流株式会社")])
    acc = r.resolve(_aw_item("t", "例示運輸株式会社"))
    assert acc is not None and acc.company == "株式会社例示運輸"
    assert acc.matched_by == "company_normalized"


def test_事業所違いの行には突合しない():
    r = _resolver([_srow("サンプル物流株式会社_新潟工場")])
    assert r.resolve(_aw_item("t", "サンプル物流株式会社 長岡工場")) is None


def test_法人格だけの名前は突合キーにしない():
    r = _resolver([_srow("株式会社")])
    assert r.resolve(_aw_item("t", "有限会社")) is None


# ---- C. 紐付け先の無い古い応募 ----------------------------------------------

def _rows(*jids):
    return [SimpleNamespace(media_job_id=j) for j in jids]


def _pr(status, jid):
    return ai.ProcessResult(status=status, applicant_key="k", media="AirWork",
                            media_job_id=jid)


@pytest.fixture
def aw_stub(monkeypatch):
    st = SimpleNamespace(ensure_calls=[], rows=[], results=[], hubspot_missing=[])

    async def fake_fetch(bid, pw, out_dir):
        return Path("x.csv")
    monkeypatch.setattr(S, "_fetch_aw_csv", fake_fetch)
    monkeypatch.setattr(ai, "load_applicants_csv", lambda p: list(st.rows))
    monkeypatch.setattr(ai, "append_apply_time_log", lambda p: 0)
    monkeypatch.setattr(ai, "RealHubSpotClient", lambda token: object())
    monkeypatch.setattr(ai, "run_import", lambda rows, cli, default_login_id="": list(st.results))
    monkeypatch.setattr(S, "_missing_aw_listings",
                        lambda ids, token: [j for j in ids if j in st.hubspot_missing])

    def fake_ensure(bid, pw, missing, out_dir, absent_out=None):
        st.ensure_calls.append(sorted(missing))
        if absent_out is not None:
            absent_out.extend(missing)          # 媒体の一覧にも無かった
        return 0
    monkeypatch.setattr(S, "_ensure_aw_jobs", fake_ensure)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    return st


def test_一覧に無い求人への応募は別の欄で数える(aw_stub):
    aw_stub.rows = _rows("OLD1", "OLD1", "NEW1", "LIVE")
    aw_stub.hubspot_missing = ["OLD1", "NEW1"]
    aw_stub.results = [_pr("unlinked", "OLD1"), _pr("unlinked", "OLD1"),
                       _pr("unlinked", "NEW1"), _pr("linked", "LIVE")]
    known = {"OLD1"}
    r = S._process_aw_account_raw("A社", ["id"], "pw", Path("."), dry_run=False,
                                  allow_acquire=True, known_absent=known)
    # OLD1 は取り直さない。NEW1 は新しいIDなので従来どおり取りに行く
    assert aw_stub.ensure_calls == [["NEW1"]]
    # NEW1 も一覧に無かったので、今回から古い応募として数える
    assert r["no_listing_old"] == 3 and r["unlinked"] == 0 and r["linked"] == 1
    assert known == {"OLD1", "NEW1"}
    assert r["ok"] is True


def test_同じ求人IDで自己修復を繰り返さない(aw_stub):
    aw_stub.rows = _rows("OLD1", "OLD2")
    aw_stub.hubspot_missing = ["OLD1", "OLD2"]
    aw_stub.results = [_pr("unlinked", "OLD1"), _pr("unlinked", "OLD2")]
    known: set = set()
    S._process_aw_account_raw("A社", ["id"], "pw", Path("."), dry_run=False,
                              allow_acquire=True, known_absent=known)
    S._process_aw_account_raw("A社", ["id"], "pw", Path("."), dry_run=False,
                              allow_acquire=True, known_absent=known)
    assert aw_stub.ensure_calls == [["OLD1", "OLD2"]]     # 2回目は取りに行かない


def test_一覧に無いと分かっていない未紐付けは従来どおり未紐付け(aw_stub):
    aw_stub.rows = _rows("X")
    aw_stub.results = [_pr("unlinked", "X")]
    r = S._process_aw_account_raw("A社", ["id"], "pw", Path("."), dry_run=False,
                                  allow_acquire=False, known_absent=set())
    assert r["unlinked"] == 1 and r["no_listing_old"] == 0


def test_一覧に無い求人IDを台帳に覚える(harness, monkeypatch):
    harness.items = [_aw_item("r1", "A社")]
    harness.accounts = {"A社": (["id"], "pw")}

    def proc(company, b_ids, b_pw, out_dir, dry_run=True, allow_acquire=False,
             known_absent=None):
        known_absent.add("J9")
        return {"ok": True, "linked": 0, "unlinked": 0, "no_listing_old": 1, "dup": 0,
                "jobs_fetched": 0, "error": "", "login_id": ""}
    monkeypatch.setattr(S, "process_aw_account", proc)
    s = _run()
    assert "J9" in harness.ledger.meta_names("aw_absent_job")
    assert s["no_listing_old"] == 1 and s["unlinked"] == 0


# ---- D. 台帳を混ぜる ---------------------------------------------------------

def test_回収runの台帳を新しい方で混ぜる(tmp_path):
    mine = tmp_path / "mine.json"
    other = tmp_path / "other.json"
    mine.write_text(json.dumps({
        "a": {"status": "FAILED", "attempts": 5, "updated": "2026-10-07T10:21:00"},
        "b": {"status": "DONE", "updated": "2026-10-07T10:20:00"},
        "c": {"status": "DONE", "updated": "2026-10-07T10:06:00"},
    }), encoding="utf-8")
    other.write_text(json.dumps({
        "a": {"status": "FAILED", "attempts": 3, "updated": "2026-10-07T10:03:00"},  # 古い
        "b": {"status": "FAILED", "attempts": 1, "updated": "2026-10-07T10:24:00"},  # 新しい
        "d": {"status": "DONE", "updated": "2026-10-07T10:10:00"},                  # 無い
    }), encoding="utf-8")
    L = S.Ledger(mine, merge_from=str(other))
    assert L.merged == 2
    assert L.data["a"]["attempts"] == 5          # 定期runの新しい記録を巻き戻さない
    assert L.data["b"]["status"] == "FAILED"
    assert L.data["c"]["status"] == "DONE" and L.data["d"]["status"] == "DONE"


def test_混ぜる台帳が無ければ何もしない(tmp_path):
    L = S.Ledger(tmp_path / "mine.json", merge_from=str(tmp_path / "none.json"))
    assert L.merged == 0 and L.data == {}


def test_会社ごとの記録は応募の件数に数えない(tmp_path):
    L = S.Ledger(tmp_path / "l.json")
    L.mark("r1", "DONE")
    L.set_meta("aw_fail", S.company_key("A社"), failed=True)
    assert L.n_records() == 1
    assert L.meta("aw_fail", S.company_key("A社"))["failed"] is True
    assert "A社" not in json.dumps(L.data, ensure_ascii=False)   # 会社名は台帳に書かない
