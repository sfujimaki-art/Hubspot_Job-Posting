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


# ---- 求人の持ち主 (2026-10-05 ユーザー決定・案A) --------------------------------
# HRハッカーでは1つの店舗に複数の会社の求人が入る。さらに
# backfill_deal_shop_id_via_email が通知先メールの一致した会社の取引へ店舗IDを
# 足していくため、同じ店舗IDを複数の会社の取引が持つ (実測95店舗)。
# → 店舗IDと通知先メールは独立した証拠ではない。**通知先メールを主、店舗IDを補助**に
#   して持ち主の取引先コードを決め、求人のシステム専用項目へ書く (現場の入力は無し)。
LISTING_OWNER = "hr_owner_code"
LISTING_OWNER_BASIS = "hr_owner_basis"
BASIS_MAIL = "通知先メールで1社"
BASIS_MAIL_SHOP = "通知先メールが複数社→店舗IDで1社"
BASIS_SHOP = "通知先メールが取引に無い→店舗IDで1社"
BASIS_NG_SITES = "判定不可: 通知先メールも店舗IDも複数社(同じ会社の別拠点など)"
BASIS_NG_SHOP = "判定不可: 通知先メールが取引に無く店舗IDも複数社"
BASIS_NG_NONE = "判定不可: 手がかり無し"
BASIS_MAIL_LIVE = "通知先メールが複数社→生きた契約は1社"
BASIS_NG_DEAD = "判定不可: 持ち主の候補に生きた契約が無い"
# 別名つきアドレス (local+alias@domain) の「元」になっている別名なしのアドレスは、
# 全社共通の受け口でどの会社の求人かを示さないので判定に使わない
# (2026-10-05 逆証明: 元アドレスが1社の取引の管理用メールに入っており、元アドレスを
#  通知先にした求人がすべてその会社に寄る形だった)。
# ★実在のアドレスは公開リポジトリに書かない。元アドレスはデータから見つける。


def _base_of(mail: str) -> str:
    local, _, domain = mail.partition("@")
    return f"{local.split('+')[0]}@{domain}" if "+" in local else ""


def owner_indexes(deals: dict) -> tuple:
    """({店舗ID: {コード}}, {管理用メール(小文字): {コード}})。コード無しの取引は使わない。"""
    shop, mail = {}, {}
    for p in deals.values():
        code = str((p or {}).get(PROP_CODE) or "").strip()
        if not code:
            continue
        for s in str((p or {}).get("hrhacker_shop_ids") or "").replace(",", ";").split(";"):
            if s.strip():
                shop.setdefault(s.strip(), set()).add(code)
        for m in re.split(r"[;,\s]+", str((p or {}).get("kanri_mail_address") or "").lower()):
            if "@" in m:
                mail.setdefault(m.strip(), set()).add(code)
    bases = {_base_of(m) for m in mail} - {""}
    for b in bases:
        mail.pop(b, None)                 # 共通の元アドレスは手がかりにしない
    return shop, mail


def live_codes(deals: dict) -> set:
    """生きている取引を1件以上持つ取引先コード。"""
    return {str(p.get(PROP_CODE) or "").strip() for p in deals.values()
            if is_live(p) and str(p.get(PROP_CODE) or "").strip()}


def resolve_owner(shop_id, notify_mails, shop_index: dict, mail_index: dict,
                  live: set = None) -> tuple:
    """求人1件の持ち主 (取引先コード or None, 根拠)。

    M = 通知先メールを管理用メールに持つ取引のコード / S = 店舗IDを持つ取引のコード。
    live を渡すと、**生きている取引のあるコードだけ**を持ち主にする
    (2026-10-05 逆証明: 終わった契約のコードを持ち主にすると、要否や応募の取引名を
     終わった取引から取ってしまう。実測86件)。

    1. M が1つ → 持ち主
    2. M が複数 → 生きているコードが1つならそれ / 店舗IDとの共通部分が1つならそれ
    3. M が空 → S が1つなら持ち主
    4. 候補が終わった契約だけ → 店舗IDの生きたコードが1つならそれ。無ければ決めない
    5. それ以外は決めない (別会社の値を入れるより空で人に回す)
    """
    S = set(shop_index.get(str(shop_id or "").strip(), ()))
    M = set()
    for m in notify_mails or ():
        M |= mail_index.get(str(m).strip().lower(), set())
    narrowed = False
    if live is not None:
        Ml, Sl = M & live, S & live
        narrowed = len(M) > 1 and len(Ml) == 1
        if M and not Ml:                       # 通知先は終わった契約だけ
            if len(Sl) == 1:
                return next(iter(Sl)), BASIS_SHOP
            return None, BASIS_NG_DEAD
        M = Ml if M else M
        if not M and S and not Sl:
            return None, BASIS_NG_DEAD
        S = Sl if S else S
    if len(M) == 1:
        return next(iter(M)), (BASIS_MAIL_LIVE if narrowed else BASIS_MAIL)
    if len(M) > 1:
        both = M & S
        if len(both) == 1:
            return next(iter(both)), BASIS_MAIL_SHOP
        return None, BASIS_NG_SITES
    if len(S) == 1:
        return next(iter(S)), BASIS_SHOP
    if len(S) > 1:
        return None, BASIS_NG_SHOP
    return None, BASIS_NG_NONE


def owner_group(listing_props: dict, by_code: dict) -> Optional[list]:
    """求人に持ち主コードが書かれていれば、そのコードの取引群。無ければ None。"""
    c = str((listing_props or {}).get(LISTING_OWNER) or "").strip()
    return list(by_code.get(c, [])) if c else None


def spans_codes(deal_ids: Iterable[str], deals: dict) -> bool:
    """取引群が別々の取引先コードにまたがるか (=どの会社の契約か決まらない)。

    2026-10-02 逆証明(第2回): 求人が別会社の取引に同時に紐付いている (実測122件)、
    または管理用メールを別会社の取引が共有している (実測35件) と、両社の取引から
    「今の契約」が選ばれ、別会社の担当者・要否・一次対応の条件が入りうる。
    """
    codes = {str((deals.get(d) or {}).get(PROP_CODE) or "").strip()
             for d in deal_ids} - {""}
    return len(codes) > 1


def group_by_code(deals: dict) -> dict:
    """{取引先コード: [取引ID]}。コードの無い取引は含めない。"""
    out: dict = {}
    for did, p in deals.items():
        c = str((p or {}).get(PROP_CODE) or "").strip()
        if c:
            out.setdefault(c, []).append(did)
    return out
