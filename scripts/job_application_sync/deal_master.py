# -*- coding: utf-8 -*-
"""同じ取引先コードの取引群から「どの取引を見るか」を決める正本 (2026-10-02)。

## なぜ1か所にまとめるか

求人1件に紐付く取引は、契約更新のたびに増える。これまで各処理が
「紐付く取引のどれを見るか」をそれぞれ独自に決めていた:

    sync_deal_association … 店舗ID/管理メール索引を setdefault の先勝ち (=最古)
    sync_ichijitaiou      … どれか1件が「必要」なら必要 (true優先)
    applicant_import      … 取引名の枝番が大きいもの (生きているかは見ない)
    relink_to_latest_deal … 最新に求人が1件でもあれば何もしない

その結果、新しい求人が終わった取引に付き、応募に古い契約の情報が入った
(2026-10-01 実測: 直近14日の応募の43%が終わった取引にしか届かない)。

2026-10-01 の定例MTGで方針が決まった:

  - 求人は**関連する取引すべて**に紐付ける (取捨選択はしない)
  - 暗黙知のマスターは「契約期間が最も新しく、かつ中身が入っている取引」
  - 応募に一度連携した暗黙知は、親の値が後で変わっても上書きしない

ここでは「どの取引を見るか」の規則だけを持ち、HubSpotは一切叩かない。

## 2つの「最新」を区別する

  latest_live(…)    担当者・要否・取引名など**今の契約の属性**を見るとき。
                    生きている取引 (deal_stages.is_writable) を優先する。
                    終わった取引の担当者や要否を拾わないため。
  anmokuchi_master(…) 一次対応の8項目 (暗黙知) を見るとき。
                    中身が入っている取引のうち、契約期間が最も新しいもの。
                    生きているかは問わない: 継続直後で新しい取引が空でも、
                    1つ前の契約に書いた条件はまだ有効なことが多い
                    (2026-10-02 実測: 生きた系列557のうち12系列がこの形)。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

try:
    from . import deal_stages as DS
except ImportError:  # pragma: no cover — CIはスクリプト直実行
    import deal_stages as DS  # type: ignore

PROP_CODE = "code_of_customer"

# 取引の「一次対応」グループのうち、応募カードへ転記する8項目。
# キー = 取引の内部名 / 値 = 応募の内部名 (2026-10-02 作成)。
# 指定テンプレート・履歴書格納ドライブ・備考は対象外 (ユーザー指定)。
ANMOKUCHI_PROPS = {
    "shodoutaiou": "anmokuchi_shodoutaiou",                          # 初動対応
    "taiouhouhou": "anmokuchi_taiouhouhou",                          # 対応方法
    "taiouhouhousonotashousai": "anmokuchi_taiouhouhousonotashousai",  # 対応方法（その他詳細）
    "oubohoukokusaki": "anmokuchi_oubohoukokusaki",                  # 誰に行うか（応募報告先）
    "mensetsujijisanbutsu": "anmokuchi_mensetsujijisanbutsu",        # 面接時持参物
    "shikakuhoyuukakunin": "anmokuchi_shikakuhoyuukakunin",          # 資格保有確認
    "keikenumukakunin": "anmokuchi_keikenumukakunin",                # 経験有無確認
    "ashigirijouken": "anmokuchi_ashigirijouken",                    # 足切り条件
}
# 転記済みの印。入っていれば二度と転記しない (上書きしない方針の実装)。
APPT_TRANSFERRED_AT = "anmokuchi_tenki_nichiji"
APPT_TRANSFERRED_FROM = "anmokuchi_tenki_moto_deal_id"

# 取引作成時にHubSpotが入れる既定値 (テンプレート)。これだけでは「中身なし」。
# 2026-10-02 実測: 足切り条件 3,678件 / 応募報告先 3,696件 がこの文言のまま。
# ★文言を行単位で消してから残りを見る。現場が見出しの一部を書き換えても、
#   値を書き足した行は残るので「中身あり」と判定できる。
_TEMPLATES = {
    "ashigirijouken": (
        "足切り条件を記載\n年齢:\n\n経験年数:\n\n必須資格:\n\n学歴NG:\n\n"
        "前職業界NG:\n\n前職企業NG:\n\nその他条件:\n\n"
        "※年齢を記載する場合は「〇歳以下」や「〇歳～〇歳まで」など、"
        "範囲が明確にわかるように記載"),
    "oubohoukokusaki": "担当者名:\n電話番号:\nアドレス:",
}
_TEMPLATE_LINES = {p: [ln.strip() for ln in t.splitlines() if ln.strip()]
                   for p, t in _TEMPLATES.items()}


def is_live(props: dict) -> bool:
    return DS.is_writable((props or {}).get("dealstage"))


def _period_key(props: dict) -> tuple:
    """契約期間の新しさ。契約開始日 → 作成日 の順で比べる。

    取引名の枝番は使わない: 人が手で付ける自由入力で、接頭辞の二重付け
    (「サブスク継続⑤＿再契約＿…」) や「再契約」を新規扱いにする規則のせいで
    解約済の①が最新と判定される事故があった (2026-10-01 実測: MSK)。
    取引先コードで束ねた時点で同じ契約の系列であることは確定しているので、
    日付で並べてよい。
    """
    p = props or {}
    return ((p.get("contract_start_date") or "")[:10], p.get("createdate") or "")


# オプション契約。主契約と並行する別サービスで、担当者・要否が主契約と違う。
# 2026-10-02 逆証明: 最新の生きた取引がオプションになるコードが46あり、
# うち11で要否、11で担当者が主契約と食い違っていた。主契約を優先する。
OPTION_STAGES = frozenset({"1049738304"})   # オプション（求人追加・一次対応）
_OPTION_NAME = re.compile(r"^\s*(求人追加|AirWork広告運用|一次対応|エントリーフォーム)")


def is_option(props: dict) -> bool:
    import unicodedata
    p = props or {}
    name = unicodedata.normalize("NFKC", str(p.get("dealname") or ""))
    return str(p.get("dealstage") or "") in OPTION_STAGES or bool(_OPTION_NAME.match(name))


def link_targets(deal_ids: Iterable[str], deals: dict) -> list:
    """求人を付ける取引: 生きている主契約すべて。主契約が無ければ生きているオプション。

    オプション取引には、そのオプションで出した求人が既に付いている。主契約の
    求人までオプションへ足すと、オプションが「今の契約」に見えてしまう。
    """
    ids = [d for d in dict.fromkeys(deal_ids) if d in deals and is_live(deals[d])]
    main = [d for d in ids if not is_option(deals[d])]
    return sorted(main or ids)


def latest_live(deal_ids: Iterable[str], deals: dict) -> Optional[str]:
    """今の契約の取引。優先順: 生きている主契約 → 生きているオプション → 全体。
    その中で契約開始日 → 作成日 が最新のもの。

    担当者・一次対応の要否・応募先取引名など「今の契約」の属性を取る用。
    """
    ids = [d for d in dict.fromkeys(deal_ids) if d in deals]
    if not ids:
        return None
    live = [d for d in ids if is_live(deals[d])]
    main = [d for d in live if not is_option(deals[d])]
    pool = main or live or ids
    return max(pool, key=lambda d: _period_key(deals[d]))


def value_has_content(prop: str, value) -> bool:
    """1項目に、テンプレート以外の中身があるか。"""
    v = str(value or "")
    for ln in _TEMPLATE_LINES.get(prop, ()):
        v = v.replace(ln, "")
    return bool(re.sub(r"[\s:：]", "", v))


def has_anmokuchi(props: dict) -> bool:
    """8項目のどれか1つにでも中身があるか。"""
    p = props or {}
    return any(value_has_content(k, p.get(k)) for k in ANMOKUCHI_PROPS)


def anmokuchi_master(deal_ids: Iterable[str], deals: dict) -> Optional[str]:
    """中身が入っている取引のうち、契約期間が最も新しいもの。無ければ None。

    解約済は除く: 契約を終えた顧客の条件を新しい応募に流さない。
    """
    ids = [d for d in dict.fromkeys(deal_ids) if d in deals
           and not DS.is_kaiyaku(deals[d].get("dealstage"))
           and has_anmokuchi(deals[d])]
    if not ids:
        return None
    return max(ids, key=lambda d: _period_key(deals[d]))


def anmokuchi_values(props: dict) -> dict:
    """マスター取引の8項目 → 応募へ書く {応募の内部名: 値}。

    中身の無い項目 (空・テンプレートのまま) は書かない。テンプレートを
    応募に写すと、BPOが「条件あり」と誤読する。
    """
    p = props or {}
    return {appt: p.get(deal) for deal, appt in ANMOKUCHI_PROPS.items()
            if value_has_content(deal, p.get(deal))}


def shop_code_index(deals: dict) -> dict:
    """{HRハッカー店舗ID: {取引先コード}}。"""
    out: dict = {}
    for p in deals.values():
        code = str((p or {}).get(PROP_CODE) or "").strip()
        if not code:
            continue
        for sid in str((p or {}).get("hrhacker_shop_ids") or "").replace(",", ";").split(";"):
            if sid.strip():
                out.setdefault(sid.strip(), set()).add(code)
    return out


def is_shared_shop(shop_id, index: dict) -> bool:
    """その店舗IDを、別々の取引先コードの取引が持っているか (=会社が決まらない)。

    2026-10-02 逆証明: 店舗IDを別会社と共有している (実測94件)。共有店舗の求人は
    たまたま先に付いた会社の取引にぶら下がっており、そのまま値を写すと
    別会社の担当者・要否・一次対応の条件が入る。触らずに人の確認へ回す。
    """
    return len(index.get(str(shop_id or "").strip(), ())) > 1


def group_by_code(deals: dict) -> dict:
    """{取引先コード: [取引ID]}。コードの無い取引は含めない。"""
    out: dict = {}
    for did, p in deals.items():
        c = str((p or {}).get(PROP_CODE) or "").strip()
        if c:
            out.setdefault(c, []).append(did)
    return out
