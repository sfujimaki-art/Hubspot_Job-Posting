"""deal_master: 同じ取引先コードの取引群から「どれを見るか」の規則 (2026-10-02)。

## 守る不変条件

1. 今の契約の属性 (担当者・要否・取引名) は**生きている取引**の最新から取る。
   終わった取引しか無いときだけ、終わった取引の最新に落ちる。
2. 新しさは取引名の枝番ではなく**契約開始日 → 作成日**。
   (枝番で決めて、解約済の「サブスク継続①」が「再契約」より新しいと判定された
    事故が実在する。2026-10-01 実測: MSK)
3. 暗黙知のマスター = 中身が入っている取引のうち契約期間が最も新しいもの。
   生きているかは問わないが、**解約済は使わない**。
4. テンプレートのまま (足切り条件・応募報告先の既定文言) は「中身なし」。
   見出しに値を書き足した行は「中身あり」。
5. 応募へ写すのは中身のある項目だけ。テンプレートは写さない。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import deal_master as M  # noqa: E402

LIVE = "52016156"      # 定期1
ENDED = "66848546"     # 継続済
KAIYAKU = "90598807"   # 解約済（充足）
TPL_ASHI = M._TEMPLATES["ashigirijouken"]
TPL_HOUKOKU = M._TEMPLATES["oubohoukokusaki"]


def deal(stage, start="", created="", **kw):
    p = {"dealstage": stage, "contract_start_date": start, "createdate": created,
         "ashigirijouken": TPL_ASHI, "oubohoukokusaki": TPL_HOUKOKU}
    p.update(kw)
    return p


# ---- 1. 今の契約は生きている取引から ----------------------------------------

def test_生きている取引を終わった新しい取引より優先する():
    deals = {"old_live": deal(LIVE, "2026-04-01"),
             "new_ended": deal(ENDED, "2026-07-01")}
    assert M.latest_live(["old_live", "new_ended"], deals) == "old_live"


def test_生きている取引が複数なら契約開始日が新しい方():
    deals = {"a": deal(LIVE, "2026-01-01"), "b": deal(LIVE, "2026-04-01")}
    assert M.latest_live(["a", "b"], deals) == "b"


def test_契約開始日が同じなら作成日で決める():
    deals = {"a": deal(LIVE, "2026-04-01", "2026-03-01T00:00:00Z"),
             "b": deal(LIVE, "2026-04-01", "2026-03-05T00:00:00Z")}
    assert M.latest_live(["a", "b"], deals) == "b"


def test_生きている取引が無ければ終わった取引の最新():
    deals = {"a": deal(ENDED, "2025-01-01"), "b": deal(KAIYAKU, "2025-06-01")}
    assert M.latest_live(["a", "b"], deals) == "b"


def test_取引名の枝番では決めない():
    # 解約済の「サブスク継続①」より、生きている「再契約」が最新
    deals = {"s1": deal(KAIYAKU, "2025-10-01", dealname="サブスク継続①＿A社 西日本支社"),
             "re": deal(LIVE, "2026-06-01", dealname="再契約＿A社 西日本支社")}
    assert M.latest_live(["s1", "re"], deals) == "re"


def test_取得できなかった取引IDは無視する():
    assert M.latest_live(["x", "y"], {}) is None
    assert M.latest_live(["x", "a"], {"a": deal(LIVE)}) == "a"


# ---- 4. テンプレートは中身なし ----------------------------------------------

def test_テンプレートのままは中身なし():
    assert not M.value_has_content("ashigirijouken", TPL_ASHI)
    assert not M.value_has_content("oubohoukokusaki", TPL_HOUKOKU)
    assert not M.value_has_content("keikenumukakunin", "")
    assert not M.value_has_content("keikenumukakunin", None)


def test_見出しに値を書き足せば中身あり():
    v = TPL_ASHI.replace("年齢:", "年齢: 60歳まで")
    assert M.value_has_content("ashigirijouken", v)
    assert M.value_has_content("oubohoukokusaki", "担当者名: 山田\n電話番号:\nアドレス:")


def test_なしと書いたものは意図した入力として中身あり():
    assert M.value_has_content("mensetsujijisanbutsu", "なし")


def test_プルダウンは値があれば中身あり():
    assert M.has_anmokuchi({"shodoutaiou": "書類回収",
                            "ashigirijouken": TPL_ASHI, "oubohoukokusaki": TPL_HOUKOKU})
    assert not M.has_anmokuchi({"ashigirijouken": TPL_ASHI, "oubohoukokusaki": TPL_HOUKOKU})


# ---- 3. 暗黙知のマスター ------------------------------------------------------

def test_最新が空なら中身のある1つ前の契約をマスターにする():
    deals = {"prev": deal(ENDED, "2026-01-01", keikenumukakunin="フォーク経験1年"),
             "new": deal(LIVE, "2026-07-01")}
    assert M.anmokuchi_master(["prev", "new"], deals) == "prev"


def test_中身のある取引が複数なら契約期間が新しい方():
    deals = {"a": deal(ENDED, "2026-01-01", keikenumukakunin="旧条件"),
             "b": deal(LIVE, "2026-07-01", keikenumukakunin="新条件")}
    assert M.anmokuchi_master(["a", "b"], deals) == "b"


def test_解約済の取引はマスターにしない():
    deals = {"k": deal(KAIYAKU, "2026-07-01", keikenumukakunin="解約前の条件")}
    assert M.anmokuchi_master(["k"], deals) is None


def test_どれも空ならマスター無し():
    deals = {"a": deal(LIVE, "2026-01-01"), "b": deal(LIVE, "2026-07-01")}
    assert M.anmokuchi_master(["a", "b"], deals) is None


# ---- 5. 写す値 ----------------------------------------------------------------

def test_応募へは中身のある項目だけを応募の内部名で写す():
    p = deal(LIVE, shodoutaiou="書類回収", keikenumukakunin="",
             mensetsujijisanbutsu="履歴書")
    vals = M.anmokuchi_values(p)
    assert vals == {"anmokuchi_shodoutaiou": "書類回収",
                    "anmokuchi_mensetsujijisanbutsu": "履歴書"}
    # テンプレートのままの足切り条件・応募報告先は写さない
    assert "anmokuchi_ashigirijouken" not in vals
    assert "anmokuchi_oubohoukokusaki" not in vals


def test_8項目すべてに応募側の受け皿がある():
    assert len(M.ANMOKUCHI_PROPS) == 8
    assert all(v.startswith("anmokuchi_") for v in M.ANMOKUCHI_PROPS.values())


def test_取引先コードで束ねる_コード無しは含めない():
    deals = {"a": {"code_of_customer": "RL1"}, "b": {"code_of_customer": "RL1"},
             "c": {"code_of_customer": ""}, "d": {}}
    assert M.group_by_code(deals) == {"RL1": ["a", "b"]}
