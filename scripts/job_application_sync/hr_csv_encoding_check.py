"""ローカルHR求人CSVの文字コード診断。本文・識別子・パスは出力しない。"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path

from scripts.job_application_sync.hr_offers_csv import HR_CSV_ENCODING
from scripts.job_application_sync.hrhacker_import import compose_copy_body


def inspect_csv(path: Path) -> dict[str, int]:
    """? は欠落の候補件数。元の文字と通常の疑問符はCSVだけでは区別できない。"""
    raw = path.read_bytes()
    current = csv.DictReader(io.StringIO(raw.decode(HR_CSV_ENCODING, errors="replace")))
    previous = csv.DictReader(io.StringIO(raw.decode("shift_jis", errors="replace")))
    counts = {
        "rows": 0, "body_question_rows": 0, "body_question_characters": 0,
        "body_decoder_replacement_rows": 0, "body_decoder_replacement_characters": 0,
        "body_mapping_difference_rows": 0, "cp932_replacement_body_rows": 0,
    }
    for row, old in zip(current, previous):
        body = compose_copy_body(row, list(row)) or ""
        old_body = compose_copy_body(old, list(old)) or ""
        # 同じ元bytesを各デコーダで読む。cp932で読めて旧デコーダで読めない
        # 文字と、両方で読めるがUnicode対応が違う文字を分ける。
        replacements = 0
        mapping_difference = False
        for char in body:
            try:
                encoded = char.encode(HR_CSV_ENCODING)
            except UnicodeEncodeError:
                continue
            legacy = encoded.decode("shift_jis", errors="replace")
            if "\ufffd" in legacy:
                replacements += 1
            elif legacy != char:
                mapping_difference = True
        counts["rows"] += 1
        counts["body_question_rows"] += "?" in body
        counts["body_question_characters"] += body.count("?")
        counts["body_decoder_replacement_rows"] += replacements > 0
        counts["body_decoder_replacement_characters"] += replacements
        counts["body_mapping_difference_rows"] += mapping_difference and body != old_body
        counts["cp932_replacement_body_rows"] += "\ufffd" in body
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_csv(args.csv), ensure_ascii=False))


if __name__ == "__main__":
    main()
