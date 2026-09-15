# -*- coding: utf-8 -*-
"""HR求人CSV(hr_offers_all_*.csv)の選択と読み取り (2026-08-17)。

## なぜ独立したモジュールにしたか

同じCSVを backfill_deal_rpo_mail と health_check が**別の読み方**で読んでいた。

  backfill  : bytes を読んで shift_jis + errors="replace" (必ず読み切る)
  health    : open(..., encoding="cp932") で errors 指定なし

後者はデコード不能バイトに当たると読み取り途中で UnicodeDecodeError が飛び、
`except` が握って **そこまでの部分索引のまま静かに返る**。同じ店舗IDを引いても
2つのスクリプトで答えが食い違う。読み方は1か所に集約する。

## なぜ「ファイル名の辞書順で最後」を信用しないか

実測 2026-08-17 (scratchpad/csv_fetched/hr の実ファイル):

    hr_offers_all_20260726_060056.csv   91.7 MB
    hr_offers_all_20260727_091754.csv    5.5 MB   ← 辞書順ではこちらが「最新」

翌日のファイルが前日の 1/17 のサイズしかない。取得が途中で切れた部分ファイルが
「最新」として無条件に採用され、会社名の逆引きと店舗ID索引が黙って劣化していた
(実測: 店舗ID 3,078種 → 850種)。エラーも警告も出ない。

そこで **サイズが直近世代の中央値の半分を切るファイルは採用しない**。
サイズを使うのは、判定のためだけに 91MB を読むのが本末転倒だから
(行数との相関は十分で、9割欠けたファイルは必ずサイズにも出る)。
採用したファイル名・サイズ・日付・今日との差日数は必ず呼び出し側へ返す。
黙って劣化させないことが目的なので、退避したときは理由を warnings に残す。
"""
from __future__ import annotations

import csv
import io
import re
import statistics
from datetime import date, datetime
from pathlib import Path

GLOB = "hr_offers_all_*.csv"
#: 直近世代のサイズ中央値に対して、これを下回るファイルは「取得途中」とみなす
MIN_SIZE_RATIO = 0.5
#: 中央値を取るときに見る世代数 (古すぎる世代を混ぜても意味が無い)
MEDIAN_WINDOW = 10
#: これより古いCSVしか無ければ警告する (会社名の逆引きが実態から離れる)
STALE_DAYS = 7

_DATE_RE = re.compile(r"hr_offers_all_(\d{8})_")


def _file_date(p: Path) -> date | None:
    m = _DATE_RE.search(p.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def pick(dirpath: Path, today: date | None = None) -> dict:
    """採用するCSVを1本決める。

    戻り値:
      {"path": Path|None, "warnings": [str], "skipped": [(name, size, why)],
       "size": int, "date": date|None, "age_days": int|None, "candidates": int}

    ★見つからないことは異常ではない (CIの deal_hygiene ジョブは求人取込と別
      ランナーなので、このディレクトリは存在しない)。呼び出し側が
      「HRの会社名は全部空になる」と読者へ説明できるよう、理由を返す。
    """
    today = today or date.today()
    out: dict = {"path": None, "warnings": [], "skipped": [], "size": 0,
                 "date": None, "age_days": None, "candidates": 0}
    if not dirpath.is_dir():
        out["warnings"].append(f"HR求人CSVのディレクトリが無い: {dirpath}")
        return out
    cands = sorted(dirpath.glob(GLOB))
    out["candidates"] = len(cands)
    if not cands:
        out["warnings"].append(f"HR求人CSVが1本も無い: {dirpath}/{GLOB}")
        return out

    sizes = [p.stat().st_size for p in cands[-MEDIAN_WINDOW:]]
    med = statistics.median(sizes) if sizes else 0
    floor = med * MIN_SIZE_RATIO
    for p in reversed(cands):
        sz = p.stat().st_size
        if med and sz < floor:
            out["skipped"].append((p.name, sz, f"直近{len(sizes)}世代の中央値"
                                               f"{int(med):,}Bの{MIN_SIZE_RATIO:.0%}未満"))
            continue
        out["path"], out["size"] = p, sz
        out["date"] = _file_date(p)
        break
    if out["path"] is None:
        # 全部が「小さすぎる」= 中央値そのものが壊れている。最新を使うが必ず言う。
        p = cands[-1]
        out["path"], out["size"] = p, p.stat().st_size
        out["date"] = _file_date(p)
        out["warnings"].append("全世代がサイズ基準を下回りました。最新をそのまま"
                               "使いますが、HR取得側が壊れている可能性があります")
    for name, sz, why in out["skipped"]:
        out["warnings"].append(f"取得途中と判断してスキップ: {name} "
                               f"({sz:,}B) — {why}")
    if out["date"]:
        out["age_days"] = (today - out["date"]).days
        if out["age_days"] >= STALE_DAYS:
            out["warnings"].append(
                f"採用したHR求人CSVが {out['age_days']}日前 ({out['date']}) です。"
                "新しい店舗は逆引きできません")
    return out


def load_shop_to_mail(path: Path) -> dict:
    """店舗id → [連絡先メールアドレス(小文字)] を返す。

    ★encoding は shift_jis + errors="replace" に固定する。監視や補完が
      1バイトのデコード失敗で部分索引になるのは、静かに間違えるという意味で
      落ちるより悪い。列はヘッダ名で引く (位置ハードコード禁止)。
    """
    raw = Path(path).read_bytes()
    txt = raw.decode("shift_jis", errors="replace")
    rd = csv.reader(io.StringIO(txt))
    try:
        hdr = next(rd)
    except StopIteration:
        return {}
    try:
        si, mi = hdr.index("店舗id"), hdr.index("連絡先メールアドレス")
    except ValueError:
        return {}
    out: dict = {}
    for row in rd:
        if len(row) <= max(si, mi):
            continue
        sid, mail = row[si].strip(), row[mi].strip().lower()
        if not sid or not mail:
            continue
        lst = out.setdefault(sid, [])
        if mail not in lst:
            lst.append(mail)
    return out
