"""HR取込の Search 間隔・429再試行・控えの保存タイミング・本文/画像の埋め戻し (2026-10-09).

守る不変条件:
1. Search は 0.5 秒以上あけて呼ぶ. 429 は Retry-After (既定2秒, 上限30秒) を待って最大5回再試行. 5xx はバックオフ
2. 総時間が上限を超えたら明示エラー
3. 控え(スナップショット)は取込が成功した時 (または差分0件) だけ保存. 失敗時は前回の控えを残す
4. --copy-only は本文/画像の2プロパティだけ書き, 新規作成しない. HubSpotに無い行は飛ばす
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

import hrhacker_import as hi  # noqa: E402
import hr_watcher as hw  # noqa: E402


class Resp:
    def __init__(self, code=200, results=None, headers=None):
        self.status_code = code
        self._results = results or []
        self.headers = headers or {}

    def json(self):
        return {"results": self._results}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def hit(jid, hid, **props):
    return {"id": hid, "properties": {"id_hrhakkaa": jid, **props}}


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(hi.time, "sleep", lambda s: calls.append(s))
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    return calls


def test_search_spacing_and_chunking(monkeypatch, sleeps):
    posted = []
    monkeypatch.setattr(hi.requests, "post",
                        lambda url, **kw: posted.append(kw["json"]) or Resp())
    hi.find_hubspot_jobs([str(i) for i in range(250)])
    assert len(posted) == 3  # 100/100/50
    assert [len(b["filterGroups"][0]["filters"][0]["values"]) for b in posted] == [100, 100, 50]
    assert sleeps == [hi.SEARCH_INTERVAL_SEC] * 3
    assert hi.SEARCH_INTERVAL_SEC >= 0.5


def test_429_waits_retry_after_then_succeeds(monkeypatch, sleeps):
    seq = [Resp(429, headers={"Retry-After": "7"}), Resp(429, headers={"Retry-After": "999"}),
           Resp(429), Resp(200, [hit("1", "h1")])]
    monkeypatch.setattr(hi.requests, "post", lambda url, **kw: seq.pop(0))
    out = hi.find_hubspot_jobs(["1"])
    assert out["1"]["id"] == "h1"
    assert sleeps[:3] == [7.0, hi.RETRY_AFTER_CAP_SEC, hi.RETRY_AFTER_DEFAULT_SEC]


def test_429_gives_up_after_5_retries(monkeypatch, sleeps):
    n = []
    monkeypatch.setattr(hi.requests, "post", lambda url, **kw: n.append(1) or Resp(429))
    with pytest.raises(RuntimeError):
        hi.find_hubspot_jobs(["1"])
    assert len(n) == 6  # 初回 + 再試行5回


def test_5xx_backoff_and_4xx_raises_immediately(monkeypatch, sleeps):
    seq = [Resp(503), Resp(502), Resp(200)]
    monkeypatch.setattr(hi.requests, "post", lambda url, **kw: seq.pop(0))
    hi.find_hubspot_jobs(["1"])
    assert sleeps[:2] == [2.0, 4.0]
    n = []
    monkeypatch.setattr(hi.requests, "post", lambda url, **kw: n.append(1) or Resp(400))
    with pytest.raises(RuntimeError):
        hi.find_hubspot_jobs(["1"])
    assert len(n) == 1


def test_time_budget_raises(monkeypatch, sleeps):
    monkeypatch.setattr(hi, "SEARCH_TIME_BUDGET_SEC", -1)
    monkeypatch.setattr(hi.requests, "post", lambda url, **kw: Resp())
    with pytest.raises(RuntimeError, match="総時間"):
        hi.find_hubspot_jobs(["1"])


# ---------------------------------------------------------------- hr_watcher 控え
ROWS = [{"media_job_id": "1", "shop_id": "9", "job_name": "A", "original_status": "公開",
         "start_date": "", "end_date": "", "copy_body": "b", "copy_images": "i"}]


def run_watcher(tmp_path, run_fn, dry_run=False):
    return hw.run(csv_path="x.csv", dry_run=dry_run, skip_aw=True, snapshot_dir=tmp_path,
                  load_csv_fn=lambda p: ROWS, hrhacker_run_fn=run_fn)


def test_snapshot_not_saved_when_import_raises_then_retried(tmp_path):
    def boom(path, dry_run):
        raise RuntimeError("HTTP 429")
    res = run_watcher(tmp_path, boom)
    assert res["snapshot_kept_previous"] is True
    assert list(tmp_path.glob("*.json.gz")) == []
    # 次回: 控えが無いので同じ差分(new=1)がまた出る = 再試行される
    ok = run_watcher(tmp_path, lambda p, dry_run: {"updates_ng": 0, "creates_ng": 0})
    assert ok["diff"]["new"] == 1
    assert len(list(tmp_path.glob("*.json.gz"))) == 1
    # 成功後は差分0件
    again = run_watcher(tmp_path, lambda p, dry_run: pytest.fail("呼ばれないはず"))
    assert again["skip_reason"] == "no_diff"


def test_snapshot_not_saved_when_batch_has_failures(tmp_path):
    res = run_watcher(tmp_path, lambda p, dry_run: {"updates_ng": 2, "creates_ng": 0})
    assert res["snapshot_kept_previous"] is True
    assert list(tmp_path.glob("*.json.gz")) == []


def test_no_diff_still_saves_snapshot(tmp_path):
    run_watcher(tmp_path, lambda p, dry_run: {"updates_ng": 0})
    again = run_watcher(tmp_path, lambda p, dry_run: pytest.fail("x"))
    assert again["skip_reason"] == "no_diff" and "snapshot_path" in again


def test_first_run_mass_write_guard_keeps_behaviour(tmp_path):
    rows = [{**ROWS[0], "media_job_id": str(i)} for i in range(600)]
    res = hw.run(csv_path="x.csv", dry_run=False, skip_aw=True, snapshot_dir=tmp_path,
                 load_csv_fn=lambda p: rows,
                 hrhacker_run_fn=lambda p, dry_run: pytest.fail("中断されるはず"))
    assert res["aborted"] is True
    assert len(list(tmp_path.glob("*.json.gz"))) == 1  # 従来どおり保存


# ---------------------------------------------------------------- copy-only
def test_copy_only_updates_only_two_props_never_creates(monkeypatch, tmp_path):
    header = "求人id,店舗id,案件名,仕事内容,画像1,画像2,画像3,公開\n"
    lines = ["1,9,求人あ,内容あ,a.jpg,,,公開", "2,9,求人い,内容い,,,,公開",
             "3,9,求人う,内容う,,,,公開", "4,9,求人え,内容え,,,,公開"]
    p = tmp_path / "c.csv"
    p.write_text(header + "\n".join(lines) + "\n", encoding="shift_jis")
    rows = hi.load_hr_csv(p)
    same = {hi.PROP_COPY_BODY: rows[2]["copy_body"], hi.PROP_COPY_IMAGES: rows[2]["copy_images"]}
    existing = {"1": hit("1", "h1", hs_name="old", kyuujin_status="公開終了"),
                "2": hit("2", "h2", **{hi.PROP_COPY_BODY: "古い"}),
                "3": hit("3", "h3", **same)}  # 4 は HubSpot に無い
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    monkeypatch.setattr(hi, "find_hubspot_jobs", lambda ids: existing)
    captured = {}
    monkeypatch.setattr(hi, "batch_update", lambda u: captured.setdefault("u", u) and (len(u), 0, []))
    monkeypatch.setattr(hi, "batch_create", lambda c: pytest.fail("作成してはいけない"))
    res = hi.run_copy_only(str(p), dry_run=False)
    assert res["not_found"] == 1 and res["unchanged"] == 1 and res["updates_planned"] == 2
    assert res["updates_ok"] == 2
    for u in captured["u"]:
        assert set(u["properties"]) <= {hi.PROP_COPY_BODY, hi.PROP_COPY_IMAGES}
    assert {u["id"] for u in captured["u"]} == {"h1", "h2"}


def test_copy_only_dry_run_writes_nothing(monkeypatch, tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("求人id,店舗id,案件名,仕事内容,画像1,画像2,画像3,公開\n1,9,あ,い,,,,公開\n",
                 encoding="shift_jis")
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    monkeypatch.setattr(hi, "find_hubspot_jobs", lambda ids: {"1": hit("1", "h1")})
    monkeypatch.setattr(hi, "batch_update", lambda u: pytest.fail("dry-runで書いてはいけない"))
    res = hi.run_copy_only(str(p), dry_run=True)
    assert res["updates_planned"] == 1
