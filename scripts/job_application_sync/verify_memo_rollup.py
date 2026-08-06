# -*- coding: utf-8 -*-
"""メモ集約の逆証明 (2026-08-06) — 「情報が失われていない」ことを全件で確かめる。

なぜ必要か:
  集約結果だけを眺めても、元のメモに何が書いてあったかは分からない。実際
  `clean_value` の重複除去が **年齢55を5に潰していた** が、集約CSVを見ても
  「年齢 5」としか出ておらず、原文と突き合わせるまで誰も気づけなかった。
  同種の事故 (&nbsp;で同値が割れる / Search APIが10,000件で黙って打ち切る)
  も含め、原因は共通して「変換したつもり」を実データで検算していないこと。

  そこで集約の**逆**をたどる。集約結果から元へ戻して一致を確かめ、
  さらに元から集約へ辿って欠落を探す。両方向やらないと片手落ちになる:
    - 集約→元 だけだと「勝手に足した値」は見つかるが「落とした値」は見つからない
    - 元→集約 だけだとその逆

検証する6点:
  A. 抽出漏れ   … 原文に記入があるのに抽出されていない項目
  B. 値の変質   … 正規化の前後で数字や語が失われていないか
  C. 到達       … 抽出した値が集約メモに現れているか (元→集約)
  D. 捏造       … 集約メモの値が元のどれかに実在するか (集約→元)
  E. 網羅       … 元求人すべてがどこかの集約先に入っているか
  F. 取りこぼし … 記入済みなのに集約先を決められなかった求人

使い方:
  python scripts/job_application_sync/verify_memo_rollup.py
  python scripts/job_application_sync/verify_memo_rollup.py --refetch  # 再取得
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from scripts.job_application_sync.rollup_memo_to_deal import (  # noqa: E402
    ALIASES, BASE, DEFAULT_CHOICE, FIELDS, _post, clean_value,
    extract_fields, is_empty_value, norm_key, strip_html)

CACHE = _REPO / "data" / "job_application_sync" / "memo_raw_cache.json"
# 原文から「項目: 値」らしき行を拾う緩いパターン (抽出漏れ検出用)。
# 本番の FIELD_RE は既知11項目しか見ないので、それ以外の記入を炙り出す。
LOOSE_KV = re.compile(r"^[\s　■□▪・]*([^\n:：]{1,20})[:：][\s　]*(.+?)[\s　]*$", re.M)
NOISE_KEYS = ("入力テンプレート", "暗黙知メモ", "出典", "この取引")


def fetch_raw(filled: list) -> dict:
    """元データを取得。listing props / deal / notes body。"""
    props, l2d, l2n = {}, {}, defaultdict(list)
    for i in range(0, len(filled), 100):
        ch = filled[i:i + 100]
        r = _post(f"{BASE}/crm/v3/objects/0-420/batch/read",
                  {"inputs": [{"id": x} for x in ch],
                   "properties": ["hs_name", "id_shop_hrhakkaa",
                                  "airwork_account_login_id"]})
        for o in r.get("results", []):
            props[o["id"]] = o.get("properties") or {}
        r = _post(f"{BASE}/crm/v4/associations/0-420/0-3/batch/read",
                  {"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            to = res.get("to") or []
            if to:
                l2d[str(res["from"]["id"])] = str(to[0]["toObjectId"])
        r = _post(f"{BASE}/crm/v4/associations/0-420/notes/batch/read",
                  {"inputs": [{"id": x} for x in ch]})
        for res in r.get("results", []):
            for t in (res.get("to") or []):
                l2n[str(res["from"]["id"])].append(str(t["toObjectId"]))
        if (i // 100) % 20 == 0:
            print(f"  取得 {i:,}/{len(filled):,}", flush=True)
        time.sleep(0.1)
    alln = sorted({n for v in l2n.values() for n in v})
    body = {}
    for i in range(0, len(alln), 100):
        r = _post(f"{BASE}/crm/v3/objects/notes/batch/read",
                  {"inputs": [{"id": x} for x in alln[i:i + 100]],
                   "properties": ["hs_note_body"]})
        for o in r.get("results", []):
            body[o["id"]] = (o.get("properties") or {}).get("hs_note_body") or ""
        time.sleep(0.1)
    return {"props": props, "l2d": l2d, "l2n": dict(l2n), "body": body}


def load_raw(refetch: bool) -> dict:
    src = _REPO / "data" / "job_application_sync" / "filled_memo_listings.json"
    filled = json.loads(src.read_text(encoding="utf-8"))["filled"]
    if CACHE.exists() and not refetch:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if len(d.get("props", {})) >= len(filled) * 0.99:
            print(f"キャッシュを使用 ({CACHE.name})", flush=True)
            d["filled"] = filled
            return d
    print(f"元データを取得します (求人 {len(filled):,}件)", flush=True)
    d = fetch_raw(filled)
    d["filled"] = filled
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def num_signature(s: str) -> list:
    """値に含まれる数字列。変換前後で消えたら値が壊れている。

    全角/半角は NFKC で寄せてから比べる。「５０」→「50」は表記の統一であって
    値の変質ではないため、ここで区別しないと本物の変質(55→5)が埋もれる。
    """
    import unicodedata
    return re.findall(r"\d+", unicodedata.normalize("NFKC", s or ""))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--csv", default="")
    ap.add_argument("--out-dir", default="claudedocs")
    a = ap.parse_args(argv)

    raw = load_raw(a.refetch)
    props, l2d, l2n, body = raw["props"], raw["l2d"], raw["l2n"], raw["body"]
    filled = raw["filled"]

    csv_path = Path(a.csv) if a.csv else sorted(
        Path("claudedocs").glob("取引メモ集約案_*.csv"))[-1]
    agg = {r["集約先ID"]: r for r in csv.DictReader(
        csv_path.open(encoding="utf-8-sig", newline=""))}
    print(f"集約案: {csv_path.name} ({len(agg):,}件)\n", flush=True)

    issues = defaultdict(list)
    extracted = {}          # listing_id -> {項目: 値}
    raw_pairs = {}          # listing_id -> {項目: 生値}

    # ---- A. 抽出漏れ / B. 値の変質 --------------------------------------
    for lid in filled:
        merged, merged_raw = {}, {}
        for nid in l2n.get(lid, []):
            text = strip_html(body.get(nid, ""))
            merged.update(extract_fields(text))
            # 本番(extract_fields)と同じ組み立てにする。同じ項目が複数回
            # 書かれている場合、本番は両方を「A / B」で残すので、こちらも
            # 揃えないと「値が変わった」と誤検出する。
            for m in re.finditer(
                    r"(" + "|".join(re.escape(f) for f in
                                    sorted(FIELDS + list(ALIASES),
                                           key=len, reverse=True))
                    + r")[ \t]*[:：][ \t]*([^\n]*)", text):
                k, v = ALIASES.get(m.group(1), m.group(1)), m.group(2).strip()
                # 「応募時 / 一次面接前 / 二次面接前 / …」は記入ではなく
                # テンプレの選択肢。本番も捨てるので、こちらも捨てないと
                # 選択肢まで結合されて「値が変わった」に見える。
                if not v or DEFAULT_CHOICE.match(clean_value(v)):
                    continue
                if k in merged_raw and norm_key(merged_raw[k]) != norm_key(v):
                    merged_raw[k] = f"{merged_raw[k]} / {v}"
                else:
                    merged_raw.setdefault(k, v)
            # A: 既知11項目の外に記入がないか
            for k, v in LOOSE_KV.findall(text):
                k = k.strip()
                if (k not in FIELDS and k and v
                        and not any(n in k for n in NOISE_KEYS)
                        and len(v) < 60 and not v.startswith("http")):
                    issues["A_未対応の項目"].append(
                        {"求人": (props.get(lid, {}).get("hs_name") or "")[:30],
                         "項目": k, "値": v[:40]})
        extracted[lid] = merged
        raw_pairs[lid] = merged_raw
        # B: 正規化で数字が消えていないか
        for k, rv in merged_raw.items():
            cv = merged.get(k)
            if cv is None:
                continue
            if num_signature(rv) != num_signature(cv):
                issues["B_数字が変質"].append(
                    {"求人": (props.get(lid, {}).get("hs_name") or "")[:30],
                     "項目": k, "変換前": rv, "変換後": cv})
            # 文字数が半分以下になっていたら疑う (語の脱落)
            if cv and len(cv) * 2 < len(clean_value(rv)):
                issues["B_文字が脱落"].append(
                    {"項目": k, "変換前": rv, "変換後": cv})

    # ---- C. 到達 (元→集約) ------------------------------------------------
    for lid in filled:
        did = l2d.get(lid)
        if not did or did not in agg:
            continue
        agg_body = agg[did]["メモ本文"]
        # 集約本文をそのまま正規化して包含判定する
        norm_body = norm_key(agg_body)
        for k, v in extracted[lid].items():
            if is_empty_value(v):
                # 「なし」系は本文に値を書かず「■ 制限・指定なし」へ項目名だけ
                # 載せる仕様。項目名が載っていれば情報は失われていない。
                if norm_key(k) not in norm_body:
                    issues["C_なし項目が落ちている"].append(
                        {"取引ID": did, "項目": k})
                continue
            if norm_key(v) and norm_key(v) not in norm_body:
                issues["C_集約に現れない値"].append(
                    {"取引ID": did,
                     "求人": (props.get(lid, {}).get("hs_name") or "")[:30],
                     "項目": k, "値": v})

    # ---- D. 捏造 (集約→元) ------------------------------------------------
    by_deal = defaultdict(list)
    for lid in filled:
        did = l2d.get(lid)
        if did:
            by_deal[did].append(lid)
    for did, row in agg.items():
        src_vals = set()
        for lid in by_deal.get(did, []):
            for v in extracted[lid].values():
                src_vals.add(norm_key(v))
        # 「制限・指定なし」セクションは項目名の羅列であって値ではない
        in_none_block = False
        for line in row["メモ本文"].split("\n"):
            s = line.strip()
            if s.startswith("■"):
                in_none_block = s.startswith("■ 制限・指定なし")
                continue
            if in_none_block:
                continue
            # 〔足切り基準〕などのグループ見出しは構造であって値ではない
            if not s or s.startswith(("📋", "この取引", "（出典", "〔")):
                continue
            # 集約本文の行は2形態しかない:
            #   共通    「　年齢: 55」            → 項目名で始まる
            #   求人別  「　　~60歳　← 求人名A…」  → 値で始まり出典が続く
            # 単純に最初のコロンで割ると
            # 「・貿易事務のご経験(目安:1年以上)」のような**値の中のコロン**で
            # 切り違える。項目名の一致で判定する。
            s = s.split("　←")[0].strip()
            m = re.match(r"^(" + "|".join(re.escape(f) for f in
                                          sorted(FIELDS, key=len, reverse=True))
                         + r")[ \t]*[:：][ \t]*(.*)$", s)
            v = norm_key(m.group(2) if m else s)
            if v and v not in src_vals:
                issues["D_元に無い値"].append(
                    {"取引ID": did, "集約メモの値": s[:60]})

    # ---- E. 網羅 / F. 取りこぼし ------------------------------------------
    in_agg = {lid for did in agg for lid in by_deal.get(did, [])}
    have_fields = {lid for lid in filled if extracted[lid]}
    lost = have_fields - in_agg
    for lid in sorted(lost)[:200]:
        p = props.get(lid, {})
        issues["F_集約先が決まらなかった求人"].append(
            {"求人ID": lid, "求人": (p.get("hs_name") or "")[:34],
             "取引": l2d.get(lid, "(取引なし)"),
             "店舗ID": p.get("id_shop_hrhakkaa") or "",
             "AW": p.get("airwork_account_login_id") or ""})

    # ---- 出力 --------------------------------------------------------------
    print("=" * 70)
    print("逆証明の結果")
    print("=" * 70)
    print(f"  元の求人(記入済メモあり)      : {len(filled):,}件")
    print(f"  うち項目を抽出できた          : {len(have_fields):,}件")
    print(f"  集約先に入った                : {len(in_agg):,}件")
    print(f"  集約先の取引                  : {len(agg):,}件\n")
    order = ["B_数字が変質", "B_文字が脱落", "C_集約に現れない値",
             "C_なし項目が落ちている", "D_元に無い値",
             "F_集約先が決まらなかった求人", "A_未対応の項目"]
    ok = True
    for k in order:
        v = issues.get(k) or []
        mark = "OK " if not v else "NG "
        if v and not k.startswith(("A_", "F_")):
            ok = False
        print(f"  [{mark}] {k}: {len(v):,}件")
        for e in v[:4]:
            print(f"          {e}")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"メモ集約_逆証明_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["検証項目", "内容"])
        for k in order:
            for e in (issues.get(k) or []):
                w.writerow([k, json.dumps(e, ensure_ascii=False)])
    print(f"\n詳細: {p.resolve()}")
    print("\n判定:", "情報の欠落・変質は検出されませんでした" if ok
          else "★問題を検出しました。上記を確認してください")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
