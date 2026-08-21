# -*- coding: utf-8 -*-
"""取引(Deal)の系列判定と「最新の取引」選択 (2026-08-06)。

なぜ必要か:
  取引は契約期間の単位で作られ、継続のたびに新しい取引が生まれる
  (新規 → サブスク継続① → ② → …)。求人は古い取引にぶら下がったままでも
  よいが、**システムが常に最古の取引を見ていた**ため、応募者に終了した契約の
  情報(契約プラン・一次対応要否)が転記され続けていた (2026-08-06 実測: 検証40件中40件)。

判定ルール (ユーザー確定 2026-08-06):
  1. 系列キー = (会社, 契約種別, 事業所名)
     - 契約種別 (サブスク継続 / AirWork広告運用 / 求人追加…) は**別系列**
       同じ事業所でも別サービスの並列契約なので混ぜない
     - 会社だけで束ねるのは不可: 同じ会社に別事業所の取引が並列に存在する
       (例: サカイ引越センター びわこ支社/滋賀支社/京都北支社 が同日契約)
  2. 系列内の順序は**枝番**: 新規(接頭辞なし)=0 < ①=1 < ②=2 < …
  3. 枝番が同じなら 契約開始日 → 作成日 で決める
  4. それでも決まらなければ**機械判断しない** (人の確認へ回す)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# 契約種別の接頭辞 (2025-10以降の実データ922件から抽出)
# ★長いものを先に並べる: 正規表現の交替は左から順に試すため、「マーケ」を
#   先に置くと「マーケ関連」が「マーケ」+「関連」に割れ、残りが事業所名へ
#   混入する (逆証明で検出: 「マーケ関連＿株式会社A」→事業所='関連株式会社A')
#   同じ理由で「紹介料」は「紹介」より前に置く。
KIND_PAT = (r"(サブスク継続|AirWork広告運用|エントリーフォーム|マーケ関連|"
            r"請負契約|求人追加|一次対応|再契約|紹介料|スポット|単発|"
            r"年契約|深耕|初回|紹介|マーケ|請負)")

# 「新規契約と同じ扱い」にする種別 (ユーザー確定 2026-08-06)。
# 再契約・深耕・紹介(料) は新規契約と同義で、継続すると通常どおり
# 「サブスク継続①②③…」に乗る。よって接頭辞は除去するが**系列は分けず**、
# 枝番0(=新規)として扱う。これを分けてしまうと
# 「再契約＿○○」と「サブスク継続①＿○○」が別系列になり継続が繋がらない。
NEW_CONTRACT_KINDS = ("再契約", "深耕", "紹介料", "紹介", "初回")
MARU = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def parse_deal_name(name: str) -> tuple:
    """取引名 → (契約種別, 枝番, 事業所キー)。

    例:
      「株式会社青葉運輸」                    → ("", 0, "株式会社青葉運輸")
      「サブスク継続②＿リンコー株式会社 敦賀工場」 → ("サブスク継続", 2, "リンコー株式会社敦賀工場")
      「AirWork広告運用②＿リンコー株式会社 敦賀工場」→ ("AirWork広告運用", 2, 同上)
        ↑ 事業所は同じでも契約種別が違うので別系列になる
    """
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    kind, seq = "", 0
    m = re.match(r"^" + KIND_PAT + r"([" + MARU + r"0-9]*)\s*[＿_\-–—]?\s*(.*)$", s)
    if m:
        kind = m.group(1)
        raw = m.group(2) or ""
        if raw:
            seq = (MARU.index(raw[0]) + 1 if raw[0] in MARU
                   else int(re.sub(r"\D", "", raw) or 0))
        else:
            seq = 1          # 枝番なしの「サブスク継続＿」は①相当
        s = m.group(3)
        if kind in NEW_CONTRACT_KINDS:
            # 新規契約と同じ扱い: 接頭辞は取り除くが系列は分けない。
            # 「再契約＿○○」→「サブスク継続①＿○○」が同一系列で繋がる。
            kind, seq = "", 0
    # 末尾の付記 (「- 新しい取引」「- 年契約①」等) を除去
    s = re.sub(r"\s*[-–—]\s*(新しい取引|年契約[" + MARU + r"0-9]*|"
               r"継続[" + MARU + r"0-9]*)\s*$", "", s)
    s = re.sub(r"[" + MARU + r"]", "", s)
    s = re.sub(r"[＿_\-–—\s　]+", "", s)
    return kind, seq, s


# メイン契約の系譜 (ユーザー確定 2026-08-06):
#   新規(接頭辞なし/再契約/深耕/紹介/初回) → サブスク継続① → ② → …
#   これらは**同じ契約が継続している**ので1つの系列として扱う。
#   一方 AirWork広告運用 / 求人追加 / 一次対応 等は別サービスの並列契約なので
#   別系列のまま (同じ事業所でも混ぜない)。
MAIN_SERIES = "__main__"
_MAIN_KINDS = ("", "サブスク継続")


def series_kind(kind: str) -> str:
    """契約種別 → 系列の識別子。メイン契約系譜は1つに束ねる。"""
    return MAIN_SERIES if kind in _MAIN_KINDS else kind


def series_key(company_id: str, deal_name: str) -> tuple:
    """系列キー (会社, 系列種別, 事業所名)。この単位で新旧を比較する。

    事業所名が空になる取引名 (例「サブスク継続②＿」) は、そのままだと
    別々の取引が同一キーに潰れて**別事業所を同じ系列と誤認する**恐れがある。
    その場合は取引名そのものをキーに使い、安全側 (=束ねない) に倒す。
    """
    kind, _seq, branch = parse_deal_name(deal_name)
    if not branch:
        branch = f"__raw__{str(deal_name or '').strip()}"
    return (str(company_id), series_kind(kind), branch)


def sort_key(deal_props: dict) -> tuple:
    """系列内で新しいほど大きくなるキー。枝番 → 契約開始日 → 作成日。"""
    _kind, seq, _b = parse_deal_name(deal_props.get("dealname"))
    return (seq,
            (deal_props.get("contract_start_date") or "")[:10],
            (deal_props.get("createdate") or ""))


def decision_key(deal_props: dict) -> tuple:
    """順序の**根拠**にしてよい情報だけのキー (枝番 + 契約開始日)。

    作成日(createdate)は根拠に含めない: 同名・同枝番・同契約開始日の取引は
    「どちらが後継か」を業務的に判断できないため、作成日の僅差で機械が
    勝手に決めてはいけない (例: 「飯盛運輸株式会社」が完全同名で2件)。
    こういうケースは人の確認へ回す。
    """
    _kind, seq, _b = parse_deal_name(deal_props.get("dealname"))
    return (seq, (deal_props.get("contract_start_date") or "")[:10])


def pick_latest(deal_ids: list, deals: dict) -> tuple:
    """系列内の最新取引を返す。

    Returns:
        (latest_deal_id, ambiguous)
        ambiguous=True のときは順序が決められない (枝番も日付も同着)。
        **その場合は機械で触らず人の確認に回すこと。**
    """
    if not deal_ids:
        return None, False
    if len(deal_ids) == 1:
        return deal_ids[0], False
    ranked = sorted(deal_ids, key=lambda d: sort_key(deals.get(d, {})))
    # 曖昧判定は decision_key (枝番+契約開始日) で行う。作成日の僅差で
    # 「決まった」ことにすると、同名・同条件の取引を機械が勝手に選んでしまう。
    top = decision_key(deals.get(ranked[-1], {}))
    tied = [d for d in deal_ids if decision_key(deals.get(d, {})) == top]
    return ranked[-1], len(tied) > 1


def latest_from_associations(assoc_ids: list, deals: dict) -> Optional[str]:
    """関連付け配列から最新の取引を選ぶ (最古を掴む既存挙動の置き換え用)。

    HubSpotの association 配列は作成順(=最古が先頭)で返るため、素朴に [0] を
    採ると常に最も古い契約を見てしまう。系列判定ができない場合でも、
    せめて契約開始日/作成日が新しいものを選ぶ。
    """
    if not assoc_ids:
        return None
    if len(assoc_ids) == 1:
        return assoc_ids[0]
    known = [d for d in assoc_ids if d in deals]
    if not known:
        return assoc_ids[0]
    return sorted(known, key=lambda d: sort_key(deals[d]))[-1]
