"""求人メモ→取引メモ集約を日次実行しても二重作成しないための回帰テスト。"""
from __future__ import annotations

from scripts.job_application_sync import rollup_memo_to_deal as rollup


def _row(deal_id: str, body: str) -> dict:
    return {
        "集約先種別": "DEAL",
        "集約先ID": deal_id,
        "元求人数": "1",
        "メモ本文": body,
    }


def test_同じ本文の既存集約メモは日次実行でも操作しない():
    """同一内容なら既存Noteをそのまま使い、二重作成も更新もしない。"""
    row = _row("D1", "📋 暗黙知メモ（取引単位・配下求人を網羅）\n本文A")
    existing = {
        "D1": {
            "note_id": "N1",
            "hash": rollup.rollup_body_hash(row["メモ本文"]),
        }
    }

    todo, summary = rollup.plan_rollup_writes([row], existing)

    assert todo == []
    assert summary == {"create": 0, "update": 0, "same": 1, "skip": 0}


def test_本文が変わった既存集約メモは同じNoteを更新対象にする():
    """日々の元メモ変更を取引の正本へ反映し、別Noteを追加しない。"""
    row = _row("D1", "📋 暗黙知メモ（取引単位・配下求人を網羅）\n本文B")
    existing = {"D1": {"note_id": "N1", "hash": "oldhash"}}

    todo, summary = rollup.plan_rollup_writes([row], existing)

    assert todo == [{**row, "operation": "update", "note_id": "N1"}]
    assert summary == {"create": 0, "update": 1, "same": 0, "skip": 0}


def test_既存集約メモが無い取引だけを新規作成対象にする():
    row = _row("D1", "📋 暗黙知メモ（取引単位・配下求人を網羅）\n本文A")

    todo, summary = rollup.plan_rollup_writes([row], {})

    assert todo == [{**row, "operation": "create"}]
    assert summary == {"create": 1, "update": 0, "same": 0, "skip": 0}


def test_既存集約メモは検索の全ページから取引ごとに取得する(monkeypatch):
    """★2026-08-31: after 方式 (10,000件で HTTP 400) をやめ、hs_object_id カーソルの
    search_all_by_id へ移した。ページング自体は test_hs_paging_by_id_20260831 が守る。
    ここでは「検索結果のうち集約署名を持つものだけを、取引IDごとに束ねる」ことを守る。
    ★検索を差し替えないと本物の API を叩く (旧版のこのテストがそうなっていた)."""
    calls = []
    searched = [
        {"id": "N1", "properties": {
            "hs_note_body": rollup.ROLLUP_SIGNATURE + "\n本文A"}},
        {"id": "N2", "properties": {
            "hs_note_body": rollup.ROLLUP_SIGNATURE + "\n本文B"}},
        # CONTAINS_TOKEN は語単位で当たるので偽陽性が混ざる。署名で落とす
        {"id": "N9", "properties": {
            "hs_note_body": "暗黙知メモの件で相談された、という人のメモ"}},
    ]

    def fake_search(obj_type, props, filters, **_kwargs):
        calls.append(("search", obj_type, filters))
        return list(searched)

    def fake_post(url, body, **_kwargs):
        calls.append(("post", url, body))
        assert [i["id"] for i in body["inputs"]] == ["N1", "N2"],             "署名の無い偽陽性は関連取得に回さない"
        return {"results": [
            {"from": {"id": "N1"}, "to": [{"toObjectId": "D1"}]},
            {"from": {"id": "N2"}, "to": [{"toObjectId": "D2"}]},
        ]}

    monkeypatch.setattr(rollup, "search_all_by_id", fake_search)
    monkeypatch.setattr(rollup, "_post", fake_post)
    monkeypatch.setattr(rollup.time, "sleep", lambda _seconds: None)

    state = rollup.existing_rollup_state()

    # ★body も持たせるようになった (2026-08-20)。マージ(積み上げ)に既存本文が
    #   要るため。厳密一致にしていると、必要な情報が増えるたびに落ちる。
    assert set(state) == {"D1", "D2"}
    for did, note_id, text in (("D1", "N1", "本文A"), ("D2", "N2", "本文B")):
        body_text = rollup.ROLLUP_SIGNATURE + "\n" + text
        assert state[did]["note_id"] == note_id
        assert state[did]["hash"] == rollup.rollup_body_hash(body_text)
        assert state[did]["body"] == body_text, "マージ用に本文を保持する"
    assert calls[0][:2] == ("search", "notes")
    assert calls[0][2][0]["operator"] == "CONTAINS_TOKEN"
    assert [c[0] for c in calls] == ["search", "post"], "関連取得は100件ごとに1回"


def test_差分は既存Noteを更新し新規対象だけを作成する(monkeypatch):
    """更新時に別Noteを作ると二重積みになるため、既存IDへPATCHする。"""
    todo = [
        {**_row("D1", "本文A"), "operation": "update", "note_id": "N1"},
        {**_row("D2", "本文B"), "operation": "create"},
    ]
    patched, created = [], []

    class Response:
        status_code = 200
        text = ""

    def fake_patch(url, **kwargs):
        patched.append((url, kwargs["json"]))
        return Response()

    def fake_post(url, body, **_kwargs):
        created.append((url, body))
        return {"id": "N2"}

    monkeypatch.setattr(rollup.requests, "patch", fake_patch)
    monkeypatch.setattr(rollup, "_post", fake_post)
    monkeypatch.setattr(rollup, "_h", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(rollup.time, "sleep", lambda _seconds: None)

    result = rollup.apply_rollup_writes(todo)

    assert result == {"created": 1, "updated": 1, "failed": 0,
                      "created_notes": [{"note_id": "N2", "deal_id": "D2"}]}
    assert patched == [(
        f"{rollup.BASE}/crm/v3/objects/notes/N1",
        {"properties": {"hs_note_body": "本文A", "hs_timestamp": patched[0][1]["properties"]["hs_timestamp"]}},
    )]
    assert created[0][0] == f"{rollup.BASE}/crm/v3/objects/notes"
    assert created[0][1]["associations"][0]["to"]["id"] == "D2"


def test_write_notesは既存との差分計画だけを反映する(monkeypatch, tmp_path):
    """CSV経由でも日次本線でも、同じ更新・冪等ロジックを必ず通す。"""
    row = _row("D1", "本文B")
    captured = []
    monkeypatch.setattr(rollup, "_REPO", tmp_path)
    monkeypatch.setattr(rollup, "existing_rollup_state", lambda: {
        "D1": {"note_id": "N1", "hash": "oldhash"}})
    monkeypatch.setattr(rollup, "apply_rollup_writes", lambda todo: (
        captured.extend(todo) or {"created": 0, "updated": 1, "failed": 0,
                                  "created_notes": []}))

    result = rollup.write_notes([row], tmp_path)

    assert captured == [{**row, "operation": "update", "note_id": "N1"}]
    assert result["created"] == 0
    assert result["updated"] == 1
    assert result["failed"] == 0
