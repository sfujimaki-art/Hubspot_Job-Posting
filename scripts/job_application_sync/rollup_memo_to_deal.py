# -*- coding: utf-8 -*-
"""求人メモ → 取引メモへの集約 (2026-08-06) — 既定はCSV出力のみ。

なぜこうするか (ユーザー確定 2026-08-06):
  求人は「新着」を取るためクローズ→出し直しで作り替えられるが、旧新を結ぶIDが
  無く、名前類似度でも66%しか対応付かない。そこで発想を逆転し、**メモの正を
  親の取引レコードに置く**。求人は取引からメモを転記されるだけになり、
  出し直しても取引は変わらないので「継承」という概念自体が不要になる。

  現場に書き直させるのは非現実的なため、**既存の記入済みメモ6,145件を
  取引へ集約**して初期値を作る。

集約の方針:
  - メモは人が読むもの。職種ごとに条件が違うのは矛盾ではなく**そう書くべき情報**
  - 集約先で値が1種類の項目 → 「顧客共通」へ
  - 値が複数ある項目 → 「求人別の条件」へ求人名付きで列挙
  - 出典(どの求人の何件から作ったか)を必ず残す

使い方:
  python scripts/job_application_sync/rollup_memo_to_deal.py            # CSV出力のみ
  python scripts/job_application_sync/rollup_memo_to_deal.py --actual   # 取引へNote作成
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
load_dotenv(_REPO / ".env")

# Windowsローカルの既定は cp932。ログ出力の1文字で処理全体が落ちるのは
# 本末転倒なので明示的に固定する (CIは PYTHONIOENCODING=utf-8 で問題ない)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


BASE = "https://api.hubapi.com"
# 集約メモの署名 (求人側テンプレと区別し、再実行時の冪等判定に使う)
try:  # script実行/パッケージ両対応の二重import
    from scripts.job_application_sync.rollup_merge import (  # noqa: E402
        merge_bodies)
    from scripts.job_application_sync.notes import (      # noqa: E402
        ROLLUP_SIGNATURE, TEMPLATE_SIGNATURE, TRANSFER_SIGNATURE, patch_note)
    from scripts.job_application_sync.hs_paging import search_all_by_id  # noqa: E402
except ImportError:  # pragma: no cover
    from rollup_merge import merge_bodies  # type: ignore
    from notes import (ROLLUP_SIGNATURE, TEMPLATE_SIGNATURE,  # type: ignore
                       TRANSFER_SIGNATURE, patch_note)
    from hs_paging import search_all_by_id  # type: ignore

# 抽出対象の項目。**現場の入力テンプレート実物に合わせてある**。
# 2026-08-06 の逆証明で、当初の11項目では記入率99%の「急ぎ度」「採用優先順位」が
# 丸ごと落ちていたことが判明した(6,145件のメモを全件パースして実測)。
# テンプレを推測で写さず、実データの出現数と記入率から決めること。
FIELD_GROUPS = [
    ("足切り基準", ["年齢", "性別", "経験年数", "必須資格", "学歴NG",
                    "前職業界NG", "前職企業NG", "理由", "その他", "自由記述"]),
    ("書類回収ルール", ["必要書類", "回収タイミング", "提出形式", "確認担当"]),
    ("優先順位", ["採用優先順位", "急ぎ度", "推薦時の注意点"]),
    ("一次対応で聞くこと", ["保有資格", "在職の有無", "転職理由・転職可能時期",
                            "連絡可能時間帯", "面接希望時期（曜日・時間帯）", "備考"]),
    ("二次面接で聞くこと", ["確認事項"]),
    ("顧客固有の事情", ["顧客が重視するポイント", "顧客固有の質問事項",
                        "想定NG・特殊事情", "コンサル所感", "選考フロー",
                        "応募後の対応"]),
]
FIELDS = [f for _g, fs in FIELD_GROUPS for f in fs]
_FIELD_OF_GROUP = {f: g for g, fs in FIELD_GROUPS for f in fs}
# 現場の書き癖による項目名のゆれ。正規の項目へ寄せる (実測で判明した分だけ)。
ALIASES = {"必要資格": "必須資格", "必要経験": "経験年数",
           "必要免許": "必須資格", "希望年齢": "年齢"}
# 長い項目名を先に置く (「保有資格」より先に「資格」等が来ると誤マッチする)
_ALT = "|".join(re.escape(f) for f in
                sorted(FIELDS + list(ALIASES), key=len, reverse=True))
FIELD_RE = re.compile(r"(" + _ALT + r")[ \t]*[:：][ \t]*([^\n]*)")
# 「■ 顧客が重視するポイント（書類選考の評価軸）」のように、コロンを使わず
# 見出しの直後に本文が来る形式。実測で記入332件(コンサル所感)。
# 本文は同じ行に続く場合と、次の行に来る場合の両方がある(HTMLの組み方次第)。
FREE_HEADS = ["顧客が重視するポイント", "顧客固有の質問事項",
              "想定NG・特殊事情", "コンサル所感"]
# テンプレの既定選択肢 (これは記入ではない)
DEFAULT_CHOICE = re.compile(r"^[^/]*/[^/]*/")
# 「記入なし」を意味する値。矢印付きの書き癖も実データにある。
EMPTY_VALUES = {"なし", "無し", "特になし", "ナシ", "無", "-", "—", "ー", "－",
                "→なし", "→特になし", "→無し", "不要", "特になし。"}


def _h() -> dict:
    tok = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not tok:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN が未設定です")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _post(url: str, body: dict, retries: int = 4):
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_h(), json=body, timeout=90)
        except requests.RequestException:
            if i < retries:
                time.sleep(2 ** i)
                continue
            raise
        if r.status_code in (200, 201, 207):
            return r.json() if r.content else {}
        if r.status_code in (429, 500, 502, 503, 504) and i < retries:
            time.sleep(2 ** i * 2)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    raise RuntimeError("retry exhausted")


# 本文末尾の出典行。日付を含むので**ハッシュからは外す**。
_SRC_LINE = re.compile(r"^（出典: 求人\d+件のメモを .+ に集約）$", re.M)


def rollup_body_hash(text: str) -> str:
    """集約本文の内容ハッシュ。日次実行で更新が必要か判定する。

    ★出典行(日付入り)を除いてハッシュする (2026-08-20 是正)。
      本文には「（出典: 求人13件のメモを 2026-08-20 に集約）」が入る。
      これを含めてハッシュすると**中身が同じでも日付が変わるだけで
      別物と判定**され、毎晩すべての集約Noteを書き換えることになる。
      実測(2026-08-20): 452件中 same=0 / update=451。つまり全件が
      毎晩無意味に書き換えられる状態だった。日次化の目的は「変わった
      ものだけ貼り直す」なので、日付は比較対象から外す。
    """
    core = _SRC_LINE.sub("", text).rstrip()
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:12]


def plan_rollup_writes(rows: list, existing: dict,
                       today: str = "") -> tuple[list, dict]:
    """集約案と既存Noteの状態から、作成・更新が必要な行だけを返す。

    existing は {deal_id: {"note_id", "hash", "body"}}。同じ本文は操作せず、
    変わった既存Noteだけを更新対象にするため、日次実行でも二重に積まない。

    today: 未更新マーカーに使う日付 (YYYY-MM-DD)。テストから固定できるよう
           引数にしている。省略時は実行日。
    """
    today = today or f"{datetime.now():%Y-%m-%d}"
    todo = []
    summary = {"create": 0, "update": 0, "same": 0, "skip": 0}
    for row in rows:
        if row.get("集約先種別") != "DEAL":
            summary["skip"] += 1
            continue
        current = existing.get(str(row["集約先ID"]))
        if not current:
            todo.append({**row, "operation": "create"})
            summary["create"] += 1
            continue
        # ★作り直さず積み上げる (2026-08-20)。
        #   求人はクローズ→出し直しでメモを失う。今回ぶんだけで置き換えると
        #   その喪失を取引側へ毎晩取り込む (実測 1,267字→200字)。既存の内容へ
        #   マージし、値が変われば新を採って旧は履歴へ、今回出てこない項目は
        #   残して未更新の印を付ける。
        merged = merge_bodies(current.get("body") or "", row["メモ本文"], today)
        row = {**row, "メモ本文": merged}
        if current.get("hash") == rollup_body_hash(merged):
            summary["same"] += 1
        else:
            todo.append({**row, "operation": "update",
                         "note_id": current["note_id"]})
            summary["update"] += 1
    return todo, summary


def strip_html(s: str) -> str:
    """HubSpotのリッチテキスト → 平文。**行の切れ目を必ず復元する**。

    以前は <br> と <p> しか改行にしておらず、実物の
    `</h3><ul><li><p>年齢</strong>:55歳まで` が
    `■ 足切り基準（顧客非開示）年齢:55歳まで` と1行に潰れていた。
    見出しと項目がくっつくと項目名の前方一致が壊れ、抽出漏れになる
    (2026-08-06 実測)。ブロック要素はすべて改行として扱う。
    """
    t = re.sub(r"<br\s*/?>", "\n", s or "")
    t = re.sub(r"</(p|li|h[1-6]|div|tr|ul|ol|table|blockquote)\s*>", "\n", t)
    t = re.sub(r"<(p|li|h[1-6]|div|tr)(\s[^>]*)?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    # HubSpotのリッチテキストは実体参照を残す。これを解かないと「&nbsp;対面」と
    # 「対面」が別値として扱われ、共通項目が「求人別の条件」へ落ちる
    # (実測: 451件中135件=30%で発生していた 2026-08-06)。
    # unescape で &nbsp; → U+00A0 になり、後段の NFKC が半角空白へ畳む。
    return html.unescape(t)


def existing_rollup_state() -> dict:
    """既存の集約Noteを全ページから取得し、取引IDごとの内容ハッシュを返す。

    Note検索は一度に100件しか返さない。ページングを省くと先頭以外の既存Noteを
    見落とし、日次実行で同じ取引へ集約Noteを重ねるため、終端まで必ず取得する。
    """
    # ★after 方式をやめ、hs_object_id カーソルで search を回す (2026-08-31 是正)。
    #   after 方式は 10,000件で HTTP 400 になる。「暗黙知メモ」のヒットは
    #   集約メモ(約650件)に転記メモ(約4,100件・増殖中)が混ざって 6,167件に達し、
    #   このままだと1週間ほどで上限に当たる見込みだった。
    #   list API (iter_all) は上限が無いが Note 30万件超で 35分かかり、
    #   毎晩走る inherit_rollup_by_contract には載せられない (一度載せて撤回)。
    notes = [note for note in search_all_by_id(
                 "notes", ["hs_note_body"],
                 [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
                   "value": "暗黙知メモ"}])
             if ROLLUP_SIGNATURE in (
                 (note.get("properties") or {}).get("hs_note_body") or "")]
    state = {}
    for i in range(0, len(notes), 100):
        chunk = notes[i:i + 100]
        linked = _post(f"{BASE}/crm/v4/associations/notes/0-3/batch/read",
                       {"inputs": [{"id": note["id"]} for note in chunk]})
        note_by_id = {str(note["id"]): note for note in chunk}
        for result in linked.get("results", []):
            note = note_by_id.get(str(result["from"]["id"]))
            targets = result.get("to") or []
            if not note or not targets:
                continue
            deal_id = str(targets[0]["toObjectId"])
            body = strip_html((note.get("properties") or {}).get(
                "hs_note_body") or "")
            state.setdefault(deal_id, {"note_id": str(note["id"]),
                                       "hash": rollup_body_hash(body),
                                       "body": body})
        time.sleep(0.1)
    return state


def _patch_note(note_id: str, body: str, retries: int = 4) -> None:
    """既存の集約Noteを更新する。関連付けを保つため作り直さない。

    ★実体は notes.patch_note (2026-08-31 に共通化)。転記側
      (sync_deal_memo_to_listing) が「作り直し+削除」で毎晩1,800件を増やして
      いたのは、ここと同じ更新の部品を使っていなかったため。1箇所に寄せる。
    """
    # トークンは rollup 側の _h() から渡す。notes 側で env を直接読ませると、
    # テストで _h を差し替えても env が要る (.env の無い環境で落ちた 2026-08-31)。
    tok = _h()["Authorization"].split(" ", 1)[1]
    patch_note(note_id, body, token=tok, retries=retries)


def apply_rollup_writes(todo: list) -> dict:
    """集約案を作成または既存Note更新として反映する。"""
    created = updated = failed = 0
    created_notes = []
    for row in todo:
        try:
            if row["operation"] == "update":
                _patch_note(row["note_id"], row["メモ本文"])
                updated += 1
            else:
                result = _post(f"{BASE}/crm/v3/objects/notes", {
                    "properties": {
                        "hs_note_body": row["メモ本文"].replace("\n", "<br>"),
                        "hs_timestamp": datetime.utcnow().strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                    },
                    "associations": [{"to": {"id": row["集約先ID"]}, "types": [{
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 214,
                    }]}],
                })
                created_notes.append({"note_id": result.get("id"),
                                      "deal_id": row["集約先ID"]})
                created += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ★失敗 deal={row['集約先ID']}: {type(exc).__name__}: "
                  f"{str(exc)[:90]}", flush=True)
        time.sleep(0.1)
    return {"created": created, "updated": updated, "failed": failed,
            "created_notes": created_notes}


def clean_value(v: str) -> str:
    """記入値のノイズを落とす。

    元データには手入力由来の汚れがある (実測):
      「:なし」  先頭にコロンが残る
      「なしなし」同じ語の重複
      「履歴書／職務経歴書」vs「履歴書 / 職務経歴書」区切りの揺れ
    これを落とさないと、同じ意味の値が別物として扱われ、本来は共通の項目が
    「求人別の条件」へ大量に振り分けられてメモが無駄に長くなる。
    """
    import unicodedata
    s = html.unescape(str(v or ""))              # 実体参照 (&nbsp; 等) を解く
    s = unicodedata.normalize("NFKC", s).strip()  # NBSP→半角空白もここで畳む
    s = re.sub(r"^[:：\s]+", "", s)              # 先頭のコロン
    s = re.sub(r"[／/]\s*", " / ", s)            # 区切りを統一
    s = re.sub(r"\s+", " ", s).strip()
    # 「対面 /」のように区切りだけ残った末尾を落とす (選択肢を消し残した跡)
    s = re.sub(r"[\s/／、,，・]+$", "", s).strip()
    # 「なしなし」のように同じ語が2回続く入力を1つに畳む。
    # ★汎用の (.{1,4}?)\1 は使わないこと。"55"→"5"、"2020"→"20" のように
    #   **年齢の足切り基準や年数を破壊する**(2026-08-06 実害: 年齢55が5になった)。
    #   畳んでよいのは意味が変わらない既知の語だけ。
    for _w in ("特になし", "なし", "無し", "ナシ"):
        if s == _w + _w:
            s = _w
            break
    return s


def norm_key(v: str) -> str:
    """比較用の正規化キー。表記ゆれを吸収して同値判定を効かせる。"""
    s = clean_value(v)
    s = re.sub(r"[\s　・、,，]", "", s)
    s = re.sub(r"[~〜～]", "-", s)
    return s.lower()


# 正規化で値が変わった全ケースの記録。
# なぜ要るか: この関数の「表記ゆれ吸収」が実際には値を壊していた
# (年齢55→5)。壊れていても集約結果を見ただけでは元の値が分からず気づけない。
# 変換前後を必ず残し、人が突き合わせられるようにする。
CHANGED: list = []


def extract_fields(text: str) -> dict:
    """メモ本文 → {項目: 値}。既定選択肢の羅列は記入とみなさない。

    2形式に対応する:
      「年齢:55」            … コロン区切り (FIELD_RE)
      「■ コンサル所感 本文」 … 見出し直後に本文が続く (FREE_RE)
    """
    out = {}
    for m in FIELD_RE.finditer(text):
        k, raw = ALIASES.get(m.group(1), m.group(1)), m.group(2)
        v = clean_value(raw)
        if not v or DEFAULT_CHOICE.match(v):
            continue
        if raw.strip() != v:
            CHANGED.append({"項目": k, "変換前": raw.strip(), "変換後": v})
        if k in out:
            # 同じ項目が1つのメモに2回書かれることがある(追記・別名の併存)。
            # 先勝ち/後勝ちのどちらでも片方が消えるので、違う値なら両方残す。
            # 実例: 保有資格に「普通自動車免許取得後3年以上」と
            #       「機械操作に弱くない人（goアプリ）」の2行があった。
            if norm_key(out[k]) != norm_key(v):
                out[k] = f"{out[k]} / {v}"
        else:
            out[k] = v
    out.update({k: v for k, v in _extract_free(text).items()
                if k not in out})
    return out


def _extract_free(text: str) -> dict:
    """見出し形式（コロンなし）の項目を拾う。

    本文は見出しと同じ行に続くことも、次の行に来ることもある。
    HTMLの組み方で変わるので両方見る。
    """
    out: dict = {}
    lines = [ln.rstrip() for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("■"):
            continue
        head = re.sub(r"^■[ \t　]*", "", s)
        name = next((h for h in FREE_HEADS if head.startswith(h)), None)
        if not name:
            continue
        rest = re.sub(r"^■[ \t　]*" + re.escape(name) +
                      r"(?:[（(][^）)]*[）)])?[ \t　]*[:：]?[ \t　]*", "", s)
        v = clean_value(rest)
        if not v:                       # 本文が次の行にある形
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    continue
                if nxt.startswith("■"):
                    break
                v = clean_value(nxt)
                break
        if v and not DEFAULT_CHOICE.match(v):
            out.setdefault(name, v)
    return out


def is_empty_value(v: str) -> bool:
    """「なし」系＝制限が無いことの表明。値としては持つが、並べると読めない。"""
    s = clean_value(v)
    return s in EMPTY_VALUES or s.lstrip("→ ").strip() in EMPTY_VALUES


def build_rollup_body(entries: list) -> str:
    """集約メモ本文を組み立てる。

    entries: [{"job": 求人名, "fields": {項目: 値}}]

    方針 (ユーザー確定 2026-08-06):
      「全部のメモをそのまま並べる」のではなく**共通部分をまとめる**。
      - 全求人で同じ値の項目 → 「全求人に共通」へ1行
      - 値が分かれる項目     → 条件が同じ求人をグループ化して列挙
        (同じ条件の求人を個別に並べるとメモが無駄に長くなる。実測では
         16求人が実質2パターンしかない例があった)
      比較は norm_key で正規化して行い、表記ゆれ(全角半角/区切り/~と〜)を吸収する。
    """
    if not entries:
        return ROLLUP_SIGNATURE
    byfield = defaultdict(list)     # 項目 → [(値, 求人名)]
    for e in entries:
        for k, v in e["fields"].items():
            byfield[k].append((v, e["job"]))
    n_jobs = len({e["job"] for e in entries})
    common, varying, none_keys = [], {}, []
    for k in FIELDS:
        vals = byfield.get(k) or []
        if not vals:
            continue
        groups = defaultdict(list)      # 正規化キー → [(表示値, 求人名)]
        for v, job in vals:
            groups[norm_key(v)].append((v, job))
        if len(groups) == 1:
            # 全求人で同じ値 → 共通へ (記入が一部求人のみでも、値が1種なら共通)
            if is_empty_value(vals[0][0]):
                # 「なし」は情報だが、26項目ぶん並べると本文が読めなくなる。
                # 末尾に1行でまとめ、「書いていない」との区別だけ残す。
                none_keys.append(k)
            else:
                common.append((_FIELD_OF_GROUP.get(k, ""), f"　{k}: {vals[0][0]}"))
        else:
            varying[k] = groups
    lines = [ROLLUP_SIGNATURE,
             "この取引に紐づく求人へ自動転記されます。", ""]
    if common:
        lines.append("■ 全求人に共通")
        last_g = None
        for g, text in common:
            if g != last_g:
                lines.append(f"〔{g}〕")
                last_g = g
            lines.append(text)
        lines.append("")
    if varying:
        lines.append("■ 求人によって異なる条件")
        for k, groups in varying.items():
            lines.append(f"　{k}:")
            # 求人数が多いグループを先に (代表的な条件が上に来る)
            for _nk, items in sorted(groups.items(),
                                     key=lambda x: -len(x[1])):
                val = items[0][0]
                jobs = sorted({j for _v, j in items})
                # 全求人の過半を占めるなら求人名を省いて簡潔に
                label = ("（上記以外）" if len(jobs) == n_jobs
                         else "、".join(j[:22] for j in jobs[:4])
                         + (f" ほか{len(jobs) - 4}件" if len(jobs) > 4 else ""))
                lines.append(f"　　{val}　← {label}")
        lines.append("")
    if none_keys:
        lines += ["■ 制限・指定なし（全求人共通）",
                  "　" + " / ".join(none_keys), ""]
    lines.append(f"（出典: 求人{n_jobs}件のメモを "
                 f"{datetime.now():%Y-%m-%d} に集約）")
    return "\n".join(lines)


def discover_filled_listings() -> list:
    """暗黙知テンプレNoteを持つ求人IDを HubSpot から直接見つける。

    ★静的スナップショットを読むのをやめた (2026-08-20 是正)。
      それまでは data/job_application_sync/filled_memo_listings.json
      (2026-08-06 時点・求人6,145件) を毎回読んでいた。このファイルを
      更新する仕組みはどこにも無く、**8/06以降に現場が書いたメモは
      永久に集約されない**状態だった。実測では 8/06 より後に作成・更新
      された元メモが 2,270件ある。日次化する以上、対象は毎晩数え直す。

      実測(2026-08-18): テンプレ署名を持つNote 8,265件 → 求人 5,134件。
    """
    # ★after 方式をやめ、hs_object_id カーソルで search を回す (2026-08-31 是正)。
    #   対象が 9,800件に達して after 方式の10,000件上限に当たり、
    #   2026-08-23 から4晩連続で HTTP 400 でクラッシュしていた (実ログで確認)。
    #
    #   しかもその 9,800件のうち 2,159件は偽陽性だった。HubSpotの
    #   CONTAINS_TOKEN は日本語を語単位で切るため、求人票本文の
    #   「テンプレートのある文書で…入力」のような無関係な文が
    #   「入力テンプレート」で引っかかる。**偽陽性が上限を押し上げていた**。
    #   本物は 7,641件 (2026-08-27 実測)。署名での確定は従来どおり行う。
    #
    #   list API (iter_all) も試したが Note 30万件超で 35分かかった。
    #   カーソル方式なら上限なしで 1〜2分。
    ids = [n["id"] for n in search_all_by_id(
               "notes", ["hs_note_body"],
               [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
                 "value": "暗黙知入力テンプレート"}])
           if TEMPLATE_SIGNATURE in ((n.get("properties") or {}).get(
               "hs_note_body") or "")]
    out = set()
    for i in range(0, len(ids), 100):   # ★100件ずつ (全件一度だと空が返る)
        r = _post(f"{BASE}/crm/v4/associations/notes/0-420/batch/read",
                  {"inputs": [{"id": x} for x in ids[i:i + 100]]})
        for res in r.get("results", []):
            for to in (res.get("to") or []):
                out.add(str(to["toObjectId"]))
        time.sleep(0.03)
    print(f"テンプレNote {len(ids):,}件 → 求人 {len(out):,}件 が対象",
          flush=True)
    return sorted(out)

def collect(filled_ids: list) -> dict:
    """求人ID群 → 集約先ごとの entries。"""
    props, l2d, l2n = {}, {}, defaultdict(list)
    for i in range(0, len(filled_ids), 100):
        ch = filled_ids[i:i + 100]
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
            print(f"  取得 {i:,}/{len(filled_ids):,}", flush=True)
        time.sleep(0.12)
    alln = sorted({n for v in l2n.values() for n in v})
    body = {}
    for i in range(0, len(alln), 100):
        r = _post(f"{BASE}/crm/v3/objects/notes/batch/read",
                  {"inputs": [{"id": x} for x in alln[i:i + 100]],
                   "properties": ["hs_note_body"]})
        for o in r.get("results", []):
            body[o["id"]] = strip_html((o.get("properties") or {}).get("hs_note_body"))
        time.sleep(0.1)
    groups = defaultdict(list)
    for lid in filled_ids:
        p = props.get(lid, {})
        did = l2d.get(lid)
        key = ("DEAL", did) if did else (
            "UNIT", p.get("id_shop_hrhakkaa") or p.get("airwork_account_login_id"))
        if not key[1]:
            continue
        fields = {}
        for n in l2n.get(lid, []):
            fields.update(extract_fields(body.get(n, "")))
        if fields:
            groups[key].append({"job": (p.get("hs_name") or "(名称なし)")[:40],
                                "listing": lid, "fields": fields})
    return groups


def write_notes(rows: list, out_dir: Path) -> dict:
    """集約案を取引の既存Noteへ差分反映する。

    rows: [{"集約先種別","集約先ID","元求人数","メモ本文"}]

    安全策:
      - 全ページの既存集約Noteを突合し、同一本文は何もしない
      - 本文が変わった取引は既存Noteを更新し、別Noteを積まない
      - 新規作成したNote IDだけ記録し、--rollback で削除できるようにする
    """
    todo, plan = plan_rollup_writes(rows, existing_rollup_state())
    print("\n=== 集約メモの内訳 ===", flush=True)
    print(f"  新規作成: {plan['create']:,}件 / 更新: {plan['update']:,}件 / "
          f"同一: {plan['same']:,}件 / 取引なし: {plan['skip']:,}件", flush=True)
    result = apply_rollup_writes(todo)
    bk = (_REPO / "data" / "job_application_sync" /
          f"rollup_notes_{datetime.now():%Y%m%dT%H%M%S}.json")
    bk.parent.mkdir(parents=True, exist_ok=True)
    bk.write_text(json.dumps(result["created_notes"], ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\n=== 結果 === 作成 {result['created']:,} / 更新 {result['updated']:,} / "
          f"同一 {plan['same']:,} / 失敗 {result['failed']:,}")
    print(f"作成したNoteの記録: {bk.resolve()}")
    print(f"取り消す場合: python {Path(__file__).name} --rollback {bk}")
    return {**result, **plan, "backup": str(bk)}


def rollback(path: str) -> int:
    """作成したNoteを削除して元に戻す。"""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"{len(items):,}件のNoteを削除します", flush=True)
    ok = fail = 0
    for n, it in enumerate(items, 1):
        nid = it.get("note_id")
        if not nid:
            continue
        r = requests.delete(f"{BASE}/crm/v3/objects/notes/{nid}",
                            headers=_h(), timeout=30)
        if r.status_code in (200, 204, 404):
            ok += 1
        else:
            fail += 1
            print(f"  ★削除失敗 note={nid}: HTTP {r.status_code}")
        if n % 50 == 0:
            print(f"  {n:,}/{len(items):,}", flush=True)
        time.sleep(0.08)
    print(f"=== 削除 {ok:,}件 / 失敗 {fail:,}件 ===")
    return 0 if not fail else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--actual", action="store_true", help="取引へNoteを作成")
    ap.add_argument("--from-csv", default="",
                    help="集約案CSVから直接書き込む (HubSpot再取得をしない)")
    ap.add_argument("--rollback", default="",
                    help="作成したNoteを削除する (rollup_notes_*.json を指定)")
    ap.add_argument("--out-dir", default="claudedocs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--from-snapshot", action="store_true",
                    help="対象求人を毎回数え直さず前回の一覧を使う"
                         "(再現用。日次実行では使わない)")
    a = ap.parse_args(argv)
    if a.rollback:
        return rollback(a.rollback)
    if a.from_csv:
        rows = list(csv.DictReader(
            Path(a.from_csv).open(encoding="utf-8-sig", newline="")))
        print(f"集約案: {a.from_csv} ({len(rows):,}件)", flush=True)
        if not a.actual:
            print("(--actual を付けると書き込みます)")
            return 0
        write_notes(rows, Path(a.out_dir))
        return 0
    scr = Path(os.environ.get("SCRATCH", "")) if os.environ.get("SCRATCH") else None
    src = (_REPO / "data" / "job_application_sync" / "filled_memo_listings.json")
    if a.from_snapshot:
        if not src.exists():
            raise SystemExit(f"スナップショットがありません: {src}")
        filled = json.loads(src.read_text(encoding="utf-8"))["filled"]
        print(f"(スナップショット {src.name} を使用)", flush=True)
    else:
        filled = discover_filled_listings()
        # 何を対象にしたかを残す。後から件数の変化を追えるようにする。
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps({"filled": filled}, ensure_ascii=False),
                       encoding="utf-8")
    if a.limit:
        filled = filled[:a.limit]
    print(f"記入済みメモを持つ求人 {len(filled):,}件 を集約します", flush=True)
    groups = collect(filled)
    print(f"\n集約先 = {len(groups):,}件", flush=True)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"取引メモ集約案_{datetime.now():%Y-%m-%d}.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["集約先種別", "集約先ID", "元求人数", "メモ本文"])
        for (kind, kid), entries in sorted(groups.items(),
                                           key=lambda x: -len(x[1])):
            w.writerow([kind, kid, len(entries), build_rollup_body(entries)])
    print(f"集約案(CSV): {p.resolve()}")
    # 正規化が値を書き換えた全ケースを出す。集約結果だけ見ても元の値は
    # 分からないので、突き合わせできる形で必ず残す。
    if CHANGED:
        uniq = {(c["項目"], c["変換前"], c["変換後"]): 0 for c in CHANGED}
        for c in CHANGED:
            uniq[(c["項目"], c["変換前"], c["変換後"])] += 1
        cp = out / f"メモ正規化の変換一覧_{datetime.now():%Y-%m-%d}.csv"
        with cp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["項目", "変換前", "変換後", "件数"])
            # ★ループ変数に a を使わない。argparse の名前空間(a)を潰し、
            #   後段の `a.actual` が AttributeError になる (2026-08-20 実行時に発覚)。
            #   CHANGED が空でないときだけ落ちるためテストでは出なかった。
            for (item, before, after), cnt in sorted(uniq.items(),
                                                     key=lambda x: -x[1]):
                w.writerow([item, before, after, cnt])
        print(f"正規化で値が変わったケース: {len(CHANGED):,}件 "
              f"({len(uniq):,}種) → {cp.resolve()}")
    big = sorted(groups.items(), key=lambda x: -len(x[1]))[:2]
    for (kind, kid), entries in big:
        print(f"\n===== 例: {kind}={kid} (元求人{len(entries)}件) =====")
        print(build_rollup_body(entries)[:900])
    if not a.actual:
        print("\n(既定はCSV出力のみ。--actual で取引へNote作成)")
        return
    rows = [
        {"集約先種別": kind, "集約先ID": kid, "元求人数": len(entries),
         "メモ本文": build_rollup_body(entries)}
        for (kind, kid), entries in groups.items()
    ]
    result = write_notes(rows, out)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    main()
