# -*- coding: utf-8 -*-
"""集約メモを「作り直す」のではなく「積み上げる」ためのマージ層 (2026-08-20)。

## なぜ要るか

集約メモの正は取引に置く、と決めた。理由は**求人がクローズ→出し直しで作り
替えられ、旧新を結ぶIDが無い**ため、求人にメモを置くと出し直すたびに失われる
から。取引は作り替えられないので、そこに置けば失われない。

ところが日次化の実装は**毎晩すべての求人を読み直して集約を作り直す**もので、
避けたはずの喪失を毎晩取り込んでいた。

実測 (2026-08-20、サンプル60取引すべてで縮小):

    取引 56896732200: 1,267字 → 200字 (-1,067)
    取引 58134781822: 1,051字 → 156字 (-895)
    取引 15858362176: 1,046字 → 386字 (-660)

56896732200 の中身で言うと、消えるのは次のような内容:

    年齢上限:   ~55歳            ← トレーラー（完成車配送）ドライバー
    必須資格:   大型免許、けん引  ← 同上
    想定NG:     免許なし、経験なくて年齢高い

これは一次対応の担当者が電話中に見る足切り基準そのもの。消してはいけない。

## 方針 (ユーザー確定 2026-08-20)

**判断1: 同じ項目で値が違うとき → C案**
    新しい値を採用し、古い値は本文末尾の履歴へ残す。
    「同じ条件とは限らない。だから残す。最終的には人間が編集して判断する」

**判断2: 今回のメモに出てこなくなった項目 → F案**
    消さずに残し、「いつ以降 未更新か」を添える。
    古い情報が黙って居座ると誤った足切りをするので、古さを必ず見えるようにする。

## 毎晩書き換わらないようにする工夫

「最終確認日」を**今回も確認できた項目には付けない**。今あるものは現役なので
日付は要らない。付けるのは**今回見つからなかった項目だけ**で、その日付は
一度決めたら動かさない。こうしないと毎晩すべての行に今日の日付が入り、
中身が同じでもハッシュが変わって全件書き換えになる (同じ理由で出典行も
ハッシュから除外している)。
"""
from __future__ import annotations

import re
from collections import OrderedDict

# 未更新マーカー。値の後ろに付ける。
STALE_FMT = "（{date} 以降 未更新）"
STALE_RE = re.compile(r"（(\d{4}-\d{2}-\d{2}) 以降 未更新）\s*$")

HISTORY_HEAD = "■ 過去の値（人が確認して整理してください）"
HISTORY_LINE = "　{field}: {value}　← {label}（{date} まで）"
HISTORY_RE = re.compile(
    r"^　(?P<field>[^:]+): (?P<value>.*?)　← (?P<label>.*?)（(?P<date>\d{4}-\d{2}-\d{2}) まで）$")

# 1項目あたりに残す履歴の上限。増え続けると本文が読めなくなる。
HISTORY_MAX = 3

SRC_RE = re.compile(r"^（出典: 求人\d+件のメモを (\d{4}-\d{2}-\d{2}) に集約）", re.M)


def parse_body(body: str) -> dict:
    """集約メモ本文 → 構造化データ。

    戻り: {
      "common":  OrderedDict[(group, field)] = (値, stale_date or None),
      "varying": OrderedDict[field] = [ (値, ラベル, stale_date or None), ... ],
      "none":    [項目, ...],
      "history": [ {field, value, label, date}, ... ],
      "asof":    "YYYY-MM-DD" or "",
    }

    ★build_rollup_body が出した形をそのまま読み戻す。書式を変えたら
      ここも直すこと。読めなかった行は捨てずに varying へ落とす。
    """
    body = (body or "").replace("<br>", "\n").replace("\r\n", "\n")
    body = re.sub(r"<[^>]+>", "", body)
    out = {"common": OrderedDict(), "varying": OrderedDict(),
           "none": [], "history": [], "asof": ""}
    m = SRC_RE.search(body)
    if m:
        out["asof"] = m.group(1)
    section = None
    group = ""
    field = ""
    for raw in body.split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("■ 全求人に共通"):
            section, group = "common", ""
            continue
        if line.startswith("■ 求人によって異なる条件"):
            section, field = "varying", ""
            continue
        if line.startswith("■ 制限・指定なし"):
            section = "none"
            continue
        if line.startswith(HISTORY_HEAD):
            section = "history"
            continue
        if line.startswith("■"):
            section = None
            continue
        if section == "common":
            if line.startswith("〔") and line.endswith("〕"):
                group = line[1:-1]
                continue
            if line.startswith("　") and ": " in line:
                k, v = line[1:].split(": ", 1)
                v, stale = _split_stale(v)
                out["common"][(group, k.strip())] = (v.strip(), stale)
            continue
        if section == "varying":
            if line.startswith("　　") and "　← " in line:
                v, label = line[2:].split("　← ", 1)
                label, stale = _split_stale(label)
                out["varying"].setdefault(field, []).append(
                    (v.strip(), label.strip(), stale))
            elif line.startswith("　") and line.rstrip().endswith(":"):
                field = line.strip().rstrip(":")
                out["varying"].setdefault(field, [])
            continue
        if section == "none":
            out["none"] += [x.strip() for x in line.strip().split(" / ")
                            if x.strip()]
            continue
        if section == "history":
            hm = HISTORY_RE.match(line)
            if hm:
                out["history"].append(hm.groupdict())
    return out


def _split_stale(text: str):
    """末尾の未更新マーカーを剥がして (本体, 日付 or None) を返す."""
    m = STALE_RE.search(text)
    if not m:
        return text.rstrip(), None
    return text[:m.start()].rstrip(), m.group(1)


def merge_bodies(old_body: str, new_body: str, today: str) -> str:
    """既存の集約メモへ、今回の集約結果を積み上げる。

    C案: 同じ項目・同じラベルで値が違えば**新しい値を採用**し、
         古い値は履歴へ移す。
    F案: 今回出てこなかった項目・値は**残し**、未更新マーカーを付ける。

    today: "YYYY-MM-DD"。**今回見つからなかったものに初めて印を付けるとき
           だけ**使う。既に印がある行の日付は動かさない (毎晩の書き換え防止)。
    """
    if not (old_body or "").strip():
        return new_body
    old = parse_body(old_body)
    new = parse_body(new_body)
    # 既存に印が無い項目が今回消えていたら、「最後に確認できた日」= 既存の集約日
    lost_date = old.get("asof") or today
    hist = list(old["history"])

    # --- 全求人に共通 -----------------------------------------------------
    common = OrderedDict()
    for key, (val, stale) in new["common"].items():
        common[key] = (val, None)                 # 今回確認できた = 現役
        ov = old["common"].get(key)
        if ov and ov[0] != val:
            hist.append({"field": key[1], "value": ov[0],
                         "label": "全求人に共通", "date": lost_date})
    for key, (val, stale) in old["common"].items():
        if key in common:
            continue
        common[key] = (val, stale or lost_date)   # 今回見つからない = 印を付ける

    # --- 求人によって異なる条件 -------------------------------------------
    varying = OrderedDict()
    for field, items in new["varying"].items():
        varying[field] = [(v, lab, None) for v, lab, _s in items]
    for field, items in old["varying"].items():
        cur = varying.setdefault(field, [])
        seen_labels = {lab for _v, lab, _s in cur}
        for v, lab, s in items:
            if lab in seen_labels:
                # 同じラベルで値が違う → 新を採り、古いのは履歴へ (C案)
                newv = next(nv for nv, nl, _ in cur if nl == lab)
                if newv != v:
                    hist.append({"field": field, "value": v,
                                 "label": lab, "date": lost_date})
                continue
            cur.append((v, lab, s or lost_date))  # 今回無い → 残して印 (F案)

    none_keys = list(dict.fromkeys(old["none"] + new["none"]))
    # 現役として復活した項目は「制限なし」から外す
    live = {k for _g, k in common} | set(varying)
    none_keys = [k for k in none_keys if k not in live]

    hist = _dedup_history(hist)
    return _render(common, varying, none_keys, hist, new_body)


def _dedup_history(hist: list) -> list:
    """同じ (項目, 値, ラベル) は1件に畳み、項目ごとに上限を掛ける."""
    seen = {}
    for h in hist:
        k = (h["field"], h["value"], h["label"])
        if k not in seen or h["date"] > seen[k]["date"]:
            seen[k] = h
    per = {}
    out = []
    for h in sorted(seen.values(), key=lambda x: (x["field"], x["date"]),
                    reverse=True):
        n = per.get(h["field"], 0)
        if n >= HISTORY_MAX:
            continue
        per[h["field"]] = n + 1
        out.append(h)
    return sorted(out, key=lambda x: (x["field"], x["date"]))


def _render(common, varying, none_keys, hist, new_body: str) -> str:
    """マージ結果を本文へ戻す。見出しと出典行は new_body のものを流用."""
    head = []
    for line in new_body.replace("<br>", "\n").split("\n"):
        if line.startswith("■") or line.startswith("（出典:"):
            break
        head.append(line)
    lines = [x for x in head if x.strip()] + [""]
    if common:
        lines.append("■ 全求人に共通")
        last = None
        for (g, k), (v, stale) in common.items():
            if g != last:
                if g:
                    lines.append(f"〔{g}〕")
                last = g
            mark = STALE_FMT.format(date=stale) if stale else ""
            lines.append(f"　{k}: {v}{mark}")
        lines.append("")
    if varying:
        lines.append("■ 求人によって異なる条件")
        for field, items in varying.items():
            if not items:
                continue
            lines.append(f"　{field}:")
            for v, lab, stale in items:
                mark = STALE_FMT.format(date=stale) if stale else ""
                lines.append(f"　　{v}　← {lab}{mark}")
        lines.append("")
    if none_keys:
        lines += ["■ 制限・指定なし（全求人共通）",
                  "　" + " / ".join(none_keys), ""]
    if hist:
        lines.append(HISTORY_HEAD)
        for h in hist:
            lines.append(HISTORY_LINE.format(**h))
        lines.append("")
    src = SRC_RE.search(new_body.replace("<br>", "\n"))
    lines.append(src.group(0) if src else "")
    return "\n".join(x for x in lines if x is not None).rstrip() + "\n"
