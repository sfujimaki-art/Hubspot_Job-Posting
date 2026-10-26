"""架空の値だけでWindows拡張文字の読込と移行比較を検証する。"""
import csv

import pytest

from scripts.job_application_sync import backfill_deal_shop_id_via_email as backfill
from scripts.job_application_sync import hr_offers_csv as offers
from scripts.job_application_sync import hr_watcher as hw
from scripts.job_application_sync import hrhacker_import as hi
from scripts.job_application_sync.hr_csv_encoding_check import inspect_csv


def write_csv(tmp_path, text):
    path = tmp_path / "sample.csv"
    with path.open("w", encoding="cp932", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["求人id", "店舗id", "案件名", "仕事内容", "公開", "連絡先メールアドレス"])
        writer.writerow(["1", "2", "架空案件", text, "公開", "fictional@example.invalid"])
    return path


def snapshot(body, *, old=False, question_marks=False, **changes):
    row = {"media_job_id": "1", "original_status": "公開", "job_name": "架空案件",
           "copy_body": body, "copy_images": None, **changes}
    if old:
        row["copy_body"] = hw.legacy_text(body, question_marks)
    jobs = hw.csv_rows_to_snapshot([row], "sample.csv")["jobs"]
    if old:
        for key in ("csv_encoding", "legacy_job_names", "legacy_copy_hashes"):
            jobs["1"].pop(key)
    return jobs


def test_cp932_bytes_preserve_circled_numbers_rules_and_kanji(tmp_path):
    text = "①─通常の漢字"
    path = write_csv(tmp_path, text)
    assert b"\x87\x40" in path.read_bytes()
    [row] = hi.load_hr_csv(path)
    assert row["copy_body"] == "案件名：架空案件\n仕事内容：" + text
    assert offers.load_shop_to_mail(path) == {"2": ["fictional@example.invalid"]}
    assert backfill.load_hr_mail_to_shops(path) == {"fictional@example.invalid": {"2"}}


def test_invalid_bytes_do_not_abort_or_truncate(tmp_path):
    path = write_csv(tmp_path, "漢字")
    path.write_bytes(path.read_bytes().replace("漢字".encode("cp932"), b"\x81\x00"))
    [row] = hi.load_hr_csv(path)
    assert "\ufffd" in row["copy_body"]
    assert row["original_status"] == "公開"
    assert offers.load_shop_to_mail(path) == {"2": ["fictional@example.invalid"]}


@pytest.mark.parametrize("question_marks", [False, True])
def test_encoding_repair_only_is_not_a_change(question_marks):
    current = snapshot("架空①")
    previous = snapshot("架空①", old=True, question_marks=question_marks)
    assert hw.detect_diff(previous, current)["changed"] == []
    assert hw.detect_diff(current, current)["changed"] == []


@pytest.mark.parametrize("changes", [
    {"copy_body": "架空②追加"}, {"original_status": "非公開"},
    {"job_name": "別の架空案件"}, {"copy_images": "架空画像"},
])
def test_actual_changes_still_count(changes):
    previous = snapshot("架空①", old=True, question_marks=True)
    current = snapshot(**{"body": changes.get("copy_body", "架空①"), **{k: v for k, v in changes.items() if k != "copy_body"}})
    assert hw.detect_diff(previous, current)["changed"] == ["1"]


def test_questions_are_not_arbitrary_wildcards():
    assert hw.detect_diff(snapshot("架空?", old=True), snapshot("架空字"))["changed"] == ["1"]
    assert hw.detect_diff(snapshot("架空①"), snapshot("架空②"))["changed"] == ["1"]
    assert hw.detect_diff(snapshot("架空?"), snapshot("架空①"))["changed"] == ["1"]


def test_missing_hash_still_initializes_history():
    previous = snapshot("架空①", old=True)
    previous["1"].pop("copy_hash")
    assert hw.detect_diff(previous, snapshot("架空①"))["changed"] == ["1"]


def test_unrepresentable_characters_are_upstream_loss():
    for char in ("⼈", "⽀", "⽥", "═"):
        with pytest.raises(UnicodeEncodeError):
            char.encode("cp932")
        assert char.encode("cp932", errors="replace") == b"?"


def test_diagnostic_separates_source_questions_decoder_loss_and_mapping(tmp_path):
    result = inspect_csv(write_csv(tmp_path, "①─漢字?～－"))
    assert result == {
        "rows": 1, "body_question_rows": 1, "body_question_characters": 1,
        "body_decoder_replacement_rows": 1, "body_decoder_replacement_characters": 1,
        "body_mapping_difference_rows": 1, "cp932_replacement_body_rows": 0,
    }


def test_title_repair_is_ignored_only_for_legacy_snapshot():
    previous = snapshot("架空", job_name="架空?", old=True)
    current = snapshot("架空", job_name="架空①")
    assert hw.detect_diff(previous, current)["changed"] == []
    previous["1"]["csv_encoding"] = "cp932"
    assert hw.detect_diff(previous, current)["changed"] == ["1"]


def test_invalid_byte_replacement_is_not_an_encoding_repair():
    assert hw.detect_diff(snapshot("架空?", old=True), snapshot("架空\ufffd"))["changed"] == ["1"]


def test_duplicate_cp932_byte_sequences_use_original_bytes_for_legacy_hash(tmp_path):
    path = write_csv(tmp_path, "漢字")
    # 同じローマ数字に対応する別のcp932 bytes。再エンコードでは同じにならない。
    path.write_bytes(path.read_bytes().replace("漢字".encode("cp932"), b"\xfa\x40"))
    [current_row] = hi.load_hr_csv(path)
    [old_row] = hi.load_hr_csv(path, encoding="shift_jis")
    previous = hw.csv_rows_to_snapshot([old_row], "sample.csv")["jobs"]
    previous["1"].pop("csv_encoding")
    current = hw.csv_rows_to_snapshot([current_row], "sample.csv")["jobs"]
    assert hw.detect_diff(previous, current)["changed"] == []
