# -*- coding: utf-8 -*-
"""納品管理PL(21596025)のステージ分類の**正本** (2026-08-17)。

## なぜ独立させたか

同じ「この取引は生きているか」の判定が2実装あり、結果が食い違っていた。

    backfill_deal_rpo_mail._closed_stages … ラベル部分一致 `"解約" in label`
    relink_to_latest_deal.KAIYAKU_STAGES  … ステージID直書き4件

ラベル部分一致は「Tヨミ：10％未満（継続に対して否定的意見、解約意向がある提案
（10％未満））」= **継続確度の予測ステージ**まで解約済として掴む。
実測(2026-08-17): 該当22件。うち管理用メールが空の1件
(取引22995522675『富士オイルサービス株式会社』)が母数から永久に落ちていた。
さらにラベル文字列に依存すると、HubSpot側でステージ名を1文字編集した日に
母数が黙って動く(実測で1,410件が動く位置にあった)。

HubSpot の `metadata.isClosed` も判定には使えない (2026-08-17 API実測):

    isClosed=true なのに契約継続中 … 「オプション（求人追加・一次対応）」
                                     「基本料プラン」「継続済」
    isClosed=false なのに終了     … 「解約済（充足）」
                                     「満了済オプション（一次対応・追加）」

よって **ステージIDのホワイトリスト**を正本に置く。ラベルは検証にだけ使う
(`check_labels()`)。未知のステージIDは「不明」= 書き込み対象外に倒す
(新ステージが実は終了ステージだった場合に黙って書き込むより安全)。

## 3分類

    解約済 (KAIYAKU_STAGES)
        契約が終わっている。値を入れても使われない。
        **索引の供給源からも外す**: 解約顧客のアドレスを生きた取引へ
        伝播させないため。

    契約終了 (ENDED_ONLY = 継続済 / 満了済オプション)
        「継続済」は契約が**新しい取引へ移った**跡地。「満了済オプション」も
        同じくオプション契約の跡地。どちらも書き込み対象にしてはいけない。
        実測(2026-08-17): 納品管理PL 3,476件のうち継続済1,390件。ここへ
        kanri_mail_address を書くと、sync_deal_association.build_mail_to_deal
        が `m.setdefault(km, d["id"])` の**先勝ち**(検索既定順=createdate昇順、
        実測インバージョン 1/1,718)なので、古い跡地の取引が勝者になり、
        新規求人が死んだ取引へ紐付く。
        ただし**値の供給源としては有用**(前契約は同じ顧客のもの)なので、
        索引には残す。書く先にしないだけ。

    書き込み可 (WRITABLE_STAGES)
        上記以外。ヨミ(T/D/C/B)は継続提案中=生きている契約なので含む。
"""
from __future__ import annotations

PIPELINE_NOUHIN = "21596025"          # リクロジ_納品管理

# 計上PL。取引先コード(code_of_customer)の発行元 (2026-08-18 API実測)。
#   商談管理_計上   3,630件 / コード保有 3,617 (100%)
#   請負契約_計上     109件 / コード保有   109 (100%)
#   リクロジバイト＿計上  0件 (レコード無し。将来のために残す)
# ★コードは「契約」単位の附番。法人が同じでも契約が違えば別コードになる。
#   納品管理の取引は契約更新のたびに作り替えられIDが変わるが、コードは
#   変わらないので、これが新旧をまたぐ唯一の安定キーになる。
PIPELINES_KEIJO = frozenset({"22417753", "741140824", "743705507"})
PROP_CODE = "code_of_customer"        # 取引先コード。string/書込可(実測)

# GET /crm/v3/pipelines/0-3/21596025 の実測 (2026-08-17)。
# ラベルは検証専用。判定はIDで行う。
STAGE_LABELS: dict = {
    "52016153": "運用MTG実施前",
    "52016154": "運用MTG実施済",
    "52016155": "求人出稿完了",
    "1281184160": "継続_定期実施前（早巻き）",
    "1049738304": "オプション（求人追加・一次対応）",
    "121846962": "基本料プラン",
    "52016156": "定期1",
    "121846961": "定期2",
    "164378752": "定期3",
    "164378755": "定期4",
    "164378758": "定期5",
    "1177549843": "定期6",
    "1177549844": "定期7",
    "1177549845": "定期8",
    "1177549846": "定期9",
    "1177549847": "定期10",
    "1177549848": "定期11",
    "164378760": "継続MTG前_壁打ち済",
    "52016157": "継続MTG",
    "1216616080": "Tヨミ：10％未満（継続に対して否定的意見、解約意向がある提案（10％未満））",
    "1216616081": "Dヨミ：20％（継続意思不明だが提案中）",
    "1216616082": "Cヨミ：50％（担当者の継続意思あり）",
    "1216616083": "Bヨミ：80％（決裁者の継続意思あり）",
    "1281526627": "満了済オプション（一次対応・追加）",
    "1016664339": "解約済架電禁止先",
    "90598807": "解約済（充足）",
    "52016159": "解約済(成果不足)",
    "52016158": "解約済(会社方針・その他)",
    "66848546": "継続済",
    "1278456227": "マーケ関連",
}

# 解約済。relink_to_latest_deal.KAIYAKU_STAGES と同一集合(2026-08-17 突合済)。
KAIYAKU_STAGES = frozenset({
    "1016664339",   # 解約済架電禁止先
    "90598807",     # 解約済（充足）
    "52016159",     # 解約済(成果不足)
    "52016158",     # 解約済(会社方針・その他)
})

# 契約が別の取引へ移った / オプションが満了した跡地。値の供給源にはするが、
# 書き込み先にはしない。
ENDED_ONLY = frozenset({
    "66848546",     # 継続済
    "1281526627",   # 満了済オプション（一次対応・追加）
})

DEAD_STAGES = frozenset(KAIYAKU_STAGES | ENDED_ONLY)
WRITABLE_STAGES = frozenset(set(STAGE_LABELS) - DEAD_STAGES)

KIND_KAIYAKU = "解約済"
KIND_ENDED = "契約終了"
KIND_WRITABLE = "書込可"
KIND_UNKNOWN = "不明"


def classify(stage_id) -> str:
    """ステージID → 4分類。未知IDは「不明」(=書き込み対象外)。"""
    s = str(stage_id or "")
    if s in KAIYAKU_STAGES:
        return KIND_KAIYAKU
    if s in ENDED_ONLY:
        return KIND_ENDED
    if s in WRITABLE_STAGES:
        return KIND_WRITABLE
    return KIND_UNKNOWN


def is_writable(stage_id) -> bool:
    return classify(stage_id) == KIND_WRITABLE


def is_kaiyaku(stage_id) -> bool:
    return classify(stage_id) == KIND_KAIYAKU


def label(stage_id) -> str:
    return STAGE_LABELS.get(str(stage_id or ""), "")


def check_labels(api_labels: dict) -> list:
    """APIから取った {ステージID: ラベル} とこのモジュールの表を突き合わせる。

    IDで判定しているのでラベル変更では壊れないが、**ステージの増減**は
    分類の見落としになる。増えたステージは「不明」= 書き込み対象外に倒れる
    ので誤書き込みにはならないが、黙って母数が縮むのは避けたいので警告を返す。

    Returns:
        警告文のリスト (空なら一致)。
    """
    warns = []
    api = {str(k): (v or "") for k, v in (api_labels or {}).items()}
    for sid in sorted(set(api) - set(STAGE_LABELS)):
        warns.append(f"未知のステージが増えています: {sid} 『{api[sid]}』 "
                     f"→ 分類「不明」= 書き込み対象外として扱います。"
                     f"deal_stages.py に分類を追加してください")
    for sid in sorted(set(STAGE_LABELS) - set(api)):
        warns.append(f"ステージが消えています: {sid} 『{STAGE_LABELS[sid]}』")
    for sid in sorted(set(api) & set(STAGE_LABELS)):
        if api[sid] != STAGE_LABELS[sid]:
            warns.append(f"ステージ名が変わりました: {sid} "
                         f"『{STAGE_LABELS[sid]}』→『{api[sid]}』 "
                         f"(判定はIDなので影響なし。表を更新してください)")
    return warns
