"""AirWork 求人の全列の変更検知 (2026-11-24).

XLSX の全列 (2026-11 時点で 271 列) を列ごとに指紋 (SHA-1 の先頭 10 桁) にし、
HubSpot の求人に保存する。次の取得で前回の指紋と比べ、変わった列があれば
「内容が変わった日時」と「変わった列の名前」を同じ 1 回の更新で書く。

- 保存するのは指紋と列名だけ。値 (求人の中身・問い合わせ先・会社情報) は保存しない。
- 指紋が前回と同じなら何も書かない (変更履歴を増やさない)。
- 初回 (前回の指紋が無い) は指紋だけを書き、変更とは扱わない。
- XLSX から列が無くなっただけでは変更と扱わない。新しく増えた列は、
  値が空でなければ変更と扱う (前回は空だったとみなす)。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

PROP_COL_HASH = "aw_retsu_hash"
PROP_CHANGED_AT = "aw_naiyou_henkou_nichiji"
PROP_CHANGED_COLS = "aw_henkou_retsu"
PROPS = [PROP_COL_HASH, PROP_CHANGED_AT, PROP_CHANGED_COLS]

_KEY_RE = re.compile(r"[\(（]([A-Za-z0-9_]+)[\)）]\s*$")


def _norm(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def _h(v) -> str:
    return hashlib.sha1(_norm(v).encode("utf-8")).hexdigest()[:10]


EMPTY = _h("")


def column_keys(headers: list[str]) -> list[Optional[tuple[str, str]]]:
    """ヘッダの各列の (比べる鍵, 日本語の列名). 位置は headers と同じ.

    鍵は括弧内の英名。英名が無い列はヘッダそのもの、空のヘッダは None。
    同じ鍵が 2 回以上出たら #2, #3 を付けて区別する。
    """
    out: list[Optional[tuple[str, str]]] = []
    seen: dict[str, int] = {}
    for h in headers:
        h = h or ""
        m = _KEY_RE.search(h)
        key = m.group(1) if m else h.strip()
        label = h[:m.start()].strip() if m else h.strip()
        if not key:
            out.append(None)
            continue
        n = seen.get(key, 0) + 1
        seen[key] = n
        if n > 1:
            key = f"{key}#{n}"
        out.append((key, label or key))
    return out


def labels(cols: list[Optional[tuple[str, str]]]) -> dict[str, str]:
    return {c[0]: c[1] for c in cols if c}


def digest(cols: list[Optional[tuple[str, str]]], raw) -> str:
    """1 行の全列の指紋を JSON 文字列で返す (鍵の順は XLSX の列の順)."""
    d = {c[0]: _h(raw[i] if i < len(raw) else None)
         for i, c in enumerate(cols) if c}
    return json.dumps(d, ensure_ascii=False,
                      separators=(",", ":"))


def change_props(row: dict, existing_props: Optional[dict],
                 now_iso: str) -> dict:
    """更新に足すプロパティ (変わっていなければ空)."""
    cur_s = row.get("aw_colhash") or ""
    if not cur_s:
        return {}
    prev_s = (existing_props or {}).get(PROP_COL_HASH) or ""
    if prev_s == cur_s:
        return {}
    p = {PROP_COL_HASH: cur_s}
    try:
        prev = json.loads(prev_s) if prev_s else None
    except ValueError:
        prev = None
    if not isinstance(prev, dict):
        return p  # 初回 (または読めない): 指紋だけ入れる
    cur = json.loads(cur_s)
    changed = [k for k in cur if prev.get(k, EMPTY) != cur[k]]
    if changed:
        labels = row.get("aw_col_labels") or {}
        p[PROP_CHANGED_AT] = now_iso
        p[PROP_CHANGED_COLS] = "、".join(labels.get(k, k) for k in changed)
    return p
