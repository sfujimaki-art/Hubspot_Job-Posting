"""求人票の本文・画像を HubSpot の求人に残す (2026-10-08)。

求人文面管理 (HR_HR /app/job-copy) が、HubSpot のプロパティ履歴を版の履歴として使う。

## 守る不変条件

1. 本文は決まった列順の「列名：値」。空の列は出さない。制作メモ・電話番号・メールは入れない
2. 画像は画像1〜3 を並び順のまま。空の枠は「なし」と書く (画像なし→あり を変化として残す)
3. 今の HubSpot の値と同じなら書かない (履歴を同じ値で埋めない)
4. CSV に列が無いときは書かない (列の無い CSV で本文を空にしない)。長すぎる値は切らずに書かない
5. 本文・画像だけが変わった日も取込を動かす。前回の控えに指紋が無いときは 1 度だけ変化扱い
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

import hrhacker_import as hi  # noqa: E402
import hr_watcher as hw  # noqa: E402

HEADER = ["求人id", "店舗id", "職種id", "案件名", "画像1", "画像2", "画像3", "仕事内容", "キャッチコピー",
          "給与形態", "基本給与 最小", "基本給与 最大", "制作メモ", "問い合わせ電話番号", "連絡先メールアドレス",
          "公開開始日時", "公開終了日時", "公開"]


def write_csv(tmp_path, rows, header=HEADER):
    path = tmp_path / "hr.csv"
    with open(path, "w", encoding="shift_jis", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return path


def row(**kw):
    base = {"求人id": "101", "店舗id": "9", "案件名": "配送ドライバー", "画像1": "https://img/a.jpg",
            "仕事内容": "地域の店舗へ配送します。\r\n1日8件程度。", "キャッチコピー": "いつもの道で",
            "給与形態": "月給", "基本給与 最小": "250000", "基本給与 最大": "280000",
            "制作メモ": "社内メモ", "問い合わせ電話番号": "0120-000-000", "連絡先メールアドレス": "a@example.com",
            "公開": "公開"}
    base.update(kw)
    return base


def test_本文は列名つきの決まった順で_社内メモと連絡先は入れない(tmp_path):
    [r] = hi.load_hr_csv(write_csv(tmp_path, [row()]))
    assert r["copy_body"] == (
        "案件名：配送ドライバー\n"
        "仕事内容：\n地域の店舗へ配送します。\n1日8件程度。\n"
        "キャッチコピー：いつもの道で\n"
        "給与形態：月給\n基本給与 最小：250000\n基本給与 最大：280000")
    for secret in ("社内メモ", "0120-000-000", "a@example.com"):
        assert secret not in r["copy_body"]


def test_画像は並び順のまま_空の枠はなし(tmp_path):
    [r] = hi.load_hr_csv(write_csv(tmp_path, [row(画像2="https://img/b.jpg")]))
    assert r["copy_images"] == "画像1：https://img/a.jpg\n画像2：https://img/b.jpg\n画像3：なし"
    [none] = hi.load_hr_csv(write_csv(tmp_path, [row(画像1="")]))
    assert none["copy_images"] == "画像1：なし\n画像2：なし\n画像3：なし"


def test_列の無いCSVでは本文も画像も書かない(tmp_path):
    header = ["求人id", "店舗id", "案件名", "公開開始日時", "公開終了日時", "公開"]
    [r] = hi.load_hr_csv(write_csv(tmp_path, [row()], header))
    # 案件名だけはあるので本文は案件名のみ。画像列は無いので None
    assert r["copy_body"] == "案件名：配送ドライバー"
    assert r["copy_images"] is None
    assert hi.PROP_COPY_IMAGES not in hi.copy_props(r, {})


def test_今の値と同じなら書かず_違えば書く(tmp_path):
    [r] = hi.load_hr_csv(write_csv(tmp_path, [row()]))
    same = {hi.PROP_COPY_BODY: r["copy_body"], hi.PROP_COPY_IMAGES: r["copy_images"]}
    assert hi.copy_props(r, same) == {}
    props = hi.build_update_props(r, {**same, hi.PROP_COPY_BODY: "古い本文"}, "2026-10-08", "2026-10-08T00:00:00Z", "hr.csv")
    assert props[hi.PROP_COPY_BODY] == r["copy_body"]
    assert hi.PROP_COPY_IMAGES not in props
    created = hi.build_create_props(r, "2026-10-08", "2026-10-08T00:00:00Z", "hr.csv")
    assert created[hi.PROP_COPY_BODY] == r["copy_body"] and created[hi.PROP_COPY_IMAGES] == r["copy_images"]


def test_長すぎる本文は切らずに書かない():
    r = {"copy_body": "あ" * (hi.COPY_MAX_CHARS + 1), "copy_images": "画像1：なし\n画像2：なし\n画像3：なし"}
    assert hi.copy_props(r, {}) == {hi.PROP_COPY_IMAGES: r["copy_images"]}


def test_本文や画像だけが変わった日も差分になる(tmp_path):
    rows = hi.load_hr_csv(write_csv(tmp_path, [row()]))
    prev = hw.csv_rows_to_snapshot(rows, "a.csv")["jobs"]
    changed_rows = hi.load_hr_csv(write_csv(tmp_path, [row(画像2="https://img/b.jpg")]))
    curr = hw.csv_rows_to_snapshot(changed_rows, "b.csv")["jobs"]
    assert hw.detect_diff(prev, curr)["changed"] == ["101"]
    assert hw.detect_diff(prev, prev)["changed"] == []
    # 導入直後: 前回の控えに指紋が無い → 1 度だけ変化として最初の版を書く
    old = {"101": {k: v for k, v in prev["101"].items() if k != "copy_hash"}}
    assert hw.detect_diff(old, prev)["changed"] == ["101"]
