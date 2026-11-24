"""AirWork 求人の全列の変更検知 (2026-11-24)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aw_changes as C  # noqa: E402
import airwork_import as awi  # noqa: E402

HDR = ["求人番号(job_offer_id)", "仕事内容(description)", "問い合わせ先担当者名(contact_name)",
       "メモ", "", "仕事内容(description)"]


def _row(raw, hdr=HDR):
    cols = C.column_keys(hdr)
    return {"media_job_id": "1", "original_status": "02",
            "aw_colhash": C.digest(cols, raw), "aw_col_labels": C.labels(cols)}


def test_column_keys():
    cols = C.column_keys(HDR)
    assert cols[0] == ("job_offer_id", "求人番号")
    assert cols[3] == ("メモ", "メモ")
    assert cols[4] is None
    assert cols[5] == ("description#2", "仕事内容")


def test_digest_has_no_values():
    s = C.digest(C.column_keys(HDR), ["1", "秘密の仕事", "担当太郎", "m", "x", "y"])
    assert "秘密" not in s and "担当" not in s
    assert set(json.loads(s)) == {"job_offer_id", "description", "contact_name", "メモ", "description#2"}


def test_crlf_and_spaces_are_same():
    cols = C.column_keys(HDR)
    assert C.digest(cols, ["1", "a\r\nb ", None]) == C.digest(cols, ["1", "a\nb", ""])


def test_first_time_writes_hash_only():
    r = _row(["1", "a", "b", "", "", ""])
    p = C.change_props(r, {}, "2026-11-24T12:00:00Z")
    assert p == {C.PROP_COL_HASH: r["aw_colhash"]}


def test_same_writes_nothing():
    r = _row(["1", "a", "b", "", "", ""])
    assert C.change_props(r, {C.PROP_COL_HASH: r["aw_colhash"]}, "x") == {}


def test_changed_columns_named():
    old = _row(["1", "a", "b", "", "", ""])
    new = _row(["1", "a2", "b2", "", "", ""])
    p = C.change_props(new, {C.PROP_COL_HASH: old["aw_colhash"]}, "2026-11-24T12:00:00Z")
    assert p[C.PROP_COL_HASH] == new["aw_colhash"]
    assert p[C.PROP_CHANGED_AT] == "2026-11-24T12:00:00Z"
    assert p[C.PROP_CHANGED_COLS] == "仕事内容、問い合わせ先担当者名"
    assert "a2" not in json.dumps(p, ensure_ascii=False)


def test_removed_column_is_not_change_added_empty_is_not_change():
    old = _row(["1", "a", "b"], HDR[:3])
    new = _row(["1", "a", "", ""], [HDR[0], HDR[1], "新しい列(new_col)", HDR[3]])
    p = C.change_props(new, {C.PROP_COL_HASH: old["aw_colhash"]}, "t")
    assert C.PROP_CHANGED_AT not in p and p[C.PROP_COL_HASH] == new["aw_colhash"]
    new2 = _row(["1", "a", "値あり", ""], [HDR[0], HDR[1], "新しい列(new_col)", HDR[3]])
    p = C.change_props(new2, {C.PROP_COL_HASH: old["aw_colhash"]}, "t")
    assert p[C.PROP_CHANGED_COLS] == "新しい列"


def test_broken_previous_is_baseline():
    r = _row(["1", "a", "b", "", "", ""])
    assert C.change_props(r, {C.PROP_COL_HASH: "{壊れ"}, "t") == {C.PROP_COL_HASH: r["aw_colhash"]}


def test_update_and_create_props():
    old = _row(["1", "a", "b", "", "", ""])
    new = _row(["1", "a", "c", "", "", ""])
    p = awi.build_update_props(new, {C.PROP_COL_HASH: old["aw_colhash"]}, "2026-11-24",
                               "2026-11-24T12:00:00Z", "f.xlsx")
    assert p[C.PROP_CHANGED_COLS] == "問い合わせ先担当者名"
    p = awi.build_update_props(new, {C.PROP_COL_HASH: new["aw_colhash"]}, "2026-11-24",
                               "2026-11-24T12:00:00Z", "f.xlsx")
    assert not set(C.PROPS) & set(p)
    p = awi.build_create_props({**new, "job_name": "x"}, "2026-11-24", "2026-11-24T12:00:00Z",
                               "f.xlsx", "login")
    assert p[C.PROP_COL_HASH] == new["aw_colhash"] and C.PROP_CHANGED_AT not in p
