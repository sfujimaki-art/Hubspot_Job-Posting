"""取引先コード補完の回帰テスト (2026-08-18)。

## なぜ作ったか

求人メモの正を取引に置いたが、**取引も契約更新でIDが変わる**ため、そのままでは
更新のたびにメモが失われる。実測(2026-08-18)で既存キーはどれも足りなかった。

| 突合キー | 跡地1,449件から生きた取引を一意に特定 |
|---|---|
| 取引↔取引の直接関連 | 235件 (16%) |
| 管理用メール | 574件 (40%) |
| 会社レコード | 515件 (36%) |
| 媒体アカウント | 166件 (11%) |

**取引先コード(code_of_customer)だけが契約単位の安定キー。** 計上PLの取引が
100%保有している。これを納品管理へ補完すれば、同じコードを持つ取引群＝同一契約
として、新旧をまたいでメモを共有できる。

## このテストが守る性質

1. 計上が1つ→書く / 複数でコードが割れる→書かない
2. **既存値が計上と食い違えば計上で上書きする**(実測5件)。当初は人の入力を守る
   ため上書きしない設計にしたが、変更履歴で全件 AUTOMATION_PLATFORM の誤りと
   判明した。犯人は無効化済みの 1611321133「請求先&取引先コード転写 会社⇒取引」
   で、会社に紐づく取引"全部"へ同じコードを配るため事業所も契約も区別しない。
   前値は log に残し `--rollback` で戻せること
3. 計上がまだ無いものは「待ち」として翌晩に持ち越す。**エラーにしない**
   (作成当日に計上があるのは41%だけ。これは異常ではなく正常な時間差)
4. 生きている取引が30日超えても埋まらないときだけ人へ回す。
   跡地(解約済/継続済)は永久に埋まらないので通知しない
5. 跡地にも**書く**。新旧を繋ぐのが目的なので跡地にこそ必要
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.job_application_sync import deal_stages as DS
from scripts.job_application_sync import (
    backfill_deal_code_of_customer as BC)

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
LIVE = "52016155"       # 求人出稿完了 (書込可)
KEIZOKU = next(iter(DS.ENDED_ONLY))
KAIYAKU = next(iter(DS.KAIYAKU_STAGES))


def _deal(stage=LIVE, code="", name="取引A", created="2026-08-01T00:00:00Z"):
    return {"dealstage": stage, DS.PROP_CODE: code, "dealname": name,
            "createdate": created}


def _keijo(code):
    return {DS.PROP_CODE: code, "dealname": "計上"}


# --------------------------------------------------------------------------
# 基本: 計上から1つに決まれば書く
# --------------------------------------------------------------------------
def test_計上が1つなら書く():
    p = BC.plan_updates({"D1": _deal()}, {"D1": ["K1"]},
                        {"K1": _keijo("RL00001867")}, now=NOW)
    assert [w["code"] for w in p["write"]] == ["RL00001867"]
    assert p["waiting"] == 0


def test_計上が複数でもコードが同じなら書く():
    """ユーザー確認: 計上は複数ぶら下がるが取引先コードは統一されている."""
    p = BC.plan_updates({"D1": _deal()}, {"D1": ["K1", "K2", "K3"]},
                        {"K1": _keijo("RL1"), "K2": _keijo("RL1"),
                         "K3": _keijo("RL1")}, now=NOW)
    assert len(p["write"]) == 1 and not p["conflict"]


def test_コードが割れていたら書かない():
    """実測21件。機械では決められないので触らない (2026-08-18 ユーザー判断)."""
    p = BC.plan_updates({"D1": _deal()}, {"D1": ["K1", "K2"]},
                        {"K1": _keijo("RL1"), "K2": _keijo("RL2")}, now=NOW)
    assert not p["write"]
    assert p["conflict"][0]["候補"] == ["RL1", "RL2"]


def test_計上の空コードは候補にしない():
    p = BC.plan_updates({"D1": _deal()}, {"D1": ["K1", "K2"]},
                        {"K1": _keijo(""), "K2": _keijo("RL1")}, now=NOW)
    assert [w["code"] for w in p["write"]] == ["RL1"]


def test_計上以外の関連取引は無視する():
    """納品管理どうしの関連が張られていることがある。コード源にしない."""
    p = BC.plan_updates({"D1": _deal(), "D2": _deal(code="RL9")},
                        {"D1": ["D2"]}, {}, now=NOW)
    assert not p["write"] and p["waiting"] == 1


# --------------------------------------------------------------------------
# 既存値の保護
# --------------------------------------------------------------------------
def test_既存値が計上と食い違えば計上で上書きする():
    """★当初は「人の入力を守る」ため上書きしない設計だったが、変更履歴を見たら
    5件とも AUTOMATION_PLATFORM が入れた誤りだった(人の手は入っていない)。
    犯人は無効化済みの 1611321133「請求先&取引先コード転写 会社⇒取引」で、
    会社に紐づく取引"全部"へ同じコードを配るため事業所も契約も区別しない。
    計上は契約単位の発番なのでそちらが正しい (2026-08-18 承認)。"""
    p = BC.plan_updates({"D1": _deal(code="RL00001235")}, {"D1": ["K1"]},
                        {"K1": _keijo("RL00001236")}, now=NOW)
    assert [w["code"] for w in p["write"]] == ["RL00001236"]
    assert p["mismatch"][0]["現在値"] == "RL00001235"


def test_上書き時は前値を残す():
    """★ロールバックできなければ本番で走らせてはいけない."""
    p = BC.plan_updates({"D1": _deal(code="RL00000033")}, {"D1": ["K1"]},
                        {"K1": _keijo("RL00001133")}, now=NOW)
    assert p["write"][0]["before"] == "RL00000033"


def test_新規補完には前値が無い():
    """空欄を埋めた分は戻すとき空文字にする。誤って別の値を書かない."""
    p = BC.plan_updates({"D1": _deal()}, {"D1": ["K1"]},
                        {"K1": _keijo("RL1")}, now=NOW)
    assert p["write"][0].get("before", "") == ""


def test_コードが割れていれば既存値があっても触らない():
    """割れている21件は上書き対象にもしない (機械では正解が決まらない)."""
    p = BC.plan_updates({"D1": _deal(code="RL00001706")}, {"D1": ["K1", "K2"]},
                        {"K1": _keijo("RL00001713"), "K2": _keijo("RL00001714")},
                        now=NOW)
    assert not p["write"] and len(p["conflict"]) == 1


def test_既存値と計上が一致すれば食い違いに出さない():
    p = BC.plan_updates({"D1": _deal(code="RL1")}, {"D1": ["K1"]},
                        {"K1": _keijo("RL1")}, now=NOW)
    assert not p["write"] and not p["mismatch"]


def test_空白だけの既存値は未入力として扱う():
    p = BC.plan_updates({"D1": _deal(code="   ")}, {"D1": ["K1"]},
                        {"K1": _keijo("RL1")}, now=NOW)
    assert [w["code"] for w in p["write"]] == ["RL1"]


# --------------------------------------------------------------------------
# 計上待ち = 正常。エラーにしない
# --------------------------------------------------------------------------
def test_計上がまだ無いものは待ちに積む():
    """★作成当日に計上があるのは41%だけ。残りは異常ではなく時間差."""
    p = BC.plan_updates({"D1": _deal(created="2026-08-17T00:00:00Z")},
                        {}, {}, now=NOW)
    assert p["waiting"] == 1 and not p["write"] and not p["stale"]


def test_待ちは翌晩に再挑戦できる形で残る():
    """夜間の総ざらいで拾い直せること。write に入れず waiting に数える."""
    n = {"D1": _deal(), "D2": _deal(name="取引B")}
    p1 = BC.plan_updates(n, {}, {}, now=NOW)
    assert p1["waiting"] == 2
    p2 = BC.plan_updates(n, {"D1": ["K1"]}, {"K1": _keijo("RL1")}, now=NOW)
    assert len(p2["write"]) == 1 and p2["waiting"] == 1


# --------------------------------------------------------------------------
# 人へ回す条件
# --------------------------------------------------------------------------
def test_生きた取引が30日超で埋まらなければ人へ回す():
    old = (NOW - timedelta(days=40)).isoformat()
    p = BC.plan_updates({"D1": _deal(created=old)}, {}, {}, now=NOW)
    assert len(p["stale"]) == 1 and p["stale"][0]["経過日数"] == 40


def test_30日以内は人へ回さない():
    """実測で99%が30日以内に解決する。早すぎる通知は狼少年になる."""
    p = BC.plan_updates({"D1": _deal(created=(NOW - timedelta(days=20)).isoformat())},
                        {}, {}, now=NOW)
    assert not p["stale"] and p["waiting"] == 1


@pytest.mark.parametrize("stage", [KEIZOKU, KAIYAKU])
def test_跡地は埋まらなくても人へ回さない(stage):
    """解約済・継続済は永久に埋まらない。通知しても現場は何もできない."""
    old = (NOW - timedelta(days=400)).isoformat()
    p = BC.plan_updates({"D1": _deal(stage=stage, created=old)}, {}, {}, now=NOW)
    assert not p["stale"]


def test_作成日が読めなくても落ちない():
    p = BC.plan_updates({"D1": _deal(created="")}, {}, {}, now=NOW)
    assert p["waiting"] == 1 and not p["stale"]


# --------------------------------------------------------------------------
# 跡地にも書く (新旧を繋ぐのが目的)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("stage", [KEIZOKU, KAIYAKU])
def test_跡地にもコードを書く(stage):
    """★ここを生きている取引だけに絞ると、新旧を繋ぐという目的が達成できない。
    跡地にコードが入って初めて『同じ契約の前の取引』を引ける。"""
    p = BC.plan_updates({"D1": _deal(stage=stage)}, {"D1": ["K1"]},
                        {"K1": _keijo("RL1")}, now=NOW)
    assert [w["code"] for w in p["write"]] == ["RL1"]


def test_同じコードで新旧が繋がる():
    """補完後に『同一契約の取引群』が引けることを、結果から確かめる."""
    n = {"OLD": _deal(stage=KEIZOKU, name="サブスク継続①＿A社"),
         "NEW": _deal(stage=LIVE, name="サブスク継続②＿A社"),
         "OPT": _deal(stage=LIVE, name="求人追加＿A社")}
    links = {"OLD": ["K1"], "NEW": ["K2"], "OPT": ["K3"]}
    kj = {"K1": _keijo("RL00001469"), "K2": _keijo("RL00001469"),
          "K3": _keijo("RL00001469")}
    p = BC.plan_updates(n, links, kj, now=NOW)
    by_code = {}
    for w in p["write"]:
        by_code.setdefault(w["code"], []).append(w["deal_id"])
    assert sorted(by_code["RL00001469"]) == ["NEW", "OLD", "OPT"], \
        "本体・オプション・跡地が1つの契約として束ねられる"


# --------------------------------------------------------------------------
# 通知文
# --------------------------------------------------------------------------
def test_通知に対象と影響とやることが入る():
    old = (NOW - timedelta(days=45)).isoformat()
    p = BC.plan_updates({"D9": _deal(created=old, name="株式会社テスト")},
                        {}, {}, now=NOW)
    msg = BC.stale_message(p["stale"])
    assert "株式会社テスト" in msg and "D9" in msg
    assert "やること" in msg and "放置すると" in msg
    assert "計上" in msg, "どこを直せばよいかを名指しする"


def test_通知は件数が多くても打ち切って総数を出す():
    n = {f"D{i}": _deal(created=(NOW - timedelta(days=40)).isoformat())
         for i in range(30)}
    msg = BC.stale_message(BC.plan_updates(n, {}, {}, now=NOW)["stale"])
    assert "ほか 15件" in msg
    assert msg.count("・") <= 16


# --------------------------------------------------------------------------
# 取りこぼしの検知
# --------------------------------------------------------------------------
def test_取得漏れがあれば落とす(monkeypatch):
    """★search の paging は一時障害で途中終了しうる (2026-08-17 に実測で
    3,478件中600件しか取れなかった)。少ない件数で黙って走らせない。"""
    calls = {"n": 0}

    def fake(url, body, **kw):
        calls["n"] += 1
        if body.get("limit") == 1:
            return {"total": 500}
        return {"results": [{"id": "D1", "properties": {}}]}   # 1件しか返さない

    monkeypatch.setattr(BC, "post_retry", fake)
    with pytest.raises(RuntimeError, match="取得漏れ"):
        BC.search_pipeline(DS.PIPELINE_NOUHIN, ["dealstage"])


def test_件数が合えば通る(monkeypatch):
    def fake(url, body, **kw):
        if body.get("limit") == 1:
            return {"total": 2}
        return {"results": [{"id": "D1", "properties": {"dealstage": LIVE}},
                            {"id": "D2", "properties": {"dealstage": LIVE}}]}

    monkeypatch.setattr(BC, "post_retry", fake)
    assert len(BC.search_pipeline(DS.PIPELINE_NOUHIN, ["dealstage"])) == 2


# --------------------------------------------------------------------------
# 定数の正しさ (パイプラインIDは正本に1箇所だけ)
# --------------------------------------------------------------------------
def test_計上パイプラインが正本にある():
    assert DS.PIPELINES_KEIJO == frozenset(
        {"22417753", "741140824", "743705507"})
    assert DS.PROP_CODE == "code_of_customer"


def test_納品管理を計上と取り違えていない():
    assert DS.PIPELINE_NOUHIN not in DS.PIPELINES_KEIJO


# --------------------------------------------------------------------------
# 承認ゲート (2026-08-19 移植)
#
# 別実装 backfill_deal_code_apply.py が持っていた承認ゲートを、既存のこちらへ
# 移植した。同じ目的のスクリプトを2本持たないため。
#
# ★設計上いちばん大事なのは「日次の無人実行を止めないこと」。
#   このスクリプトは deal_hygiene (JST 01:40) で毎晩走る。常に承認を要求すると
#   毎晩 exit 2 で落ち、「補完が動いていない」状態が延々と続く。
#   守りたいのは一度きりの大量投入(実測1,615件)と既存値の上書きの2つだけ。
# --------------------------------------------------------------------------
def _plan(n_new=0, n_over=0):
    w = [{"deal_id": f"N{i}", "code": "RL1"} for i in range(n_new)]
    w += [{"deal_id": f"O{i}", "code": "RL2", "before": "RL9"}
          for i in range(n_over)]
    return {"write": w}


def test_日次の少量差分は承認なしで通る():
    """★これが落ちると毎晩の補完が止まる。実測の日次差分は2件."""
    assert BC.require_gate(_plan(n_new=2)) == ""


def test_書込ゼロでも通る():
    assert BC.require_gate(_plan()) == ""


def test_上限ちょうどまでは通る():
    assert BC.require_gate(_plan(n_new=BC.UNATTENDED_MAX)) == ""


def test_大量投入は承認が要る():
    """初回の一括投入(実測1,615件)を無人で撃たせない."""
    why = BC.require_gate(_plan(n_new=BC.UNATTENDED_MAX + 1))
    assert "承認が要る" in why and "書込" in why


def test_少量でも上書きを含めば承認が要る():
    """★空欄補完と違い、上書きは元の値が消える。件数に関係なく人を通す."""
    why = BC.require_gate(_plan(n_new=1, n_over=1))
    assert "上書き" in why


def test_承認があれば大量でも通る():
    future = (NOW + timedelta(hours=8)).isoformat()
    assert BC.require_gate(_plan(n_new=999), future, "fuji1", now=NOW) == ""


def test_承認者だけでは通らない():
    """時刻の宣言が無ければ「いつ承認したか」が残らない."""
    why = BC.require_gate(_plan(n_new=999), None, "fuji1", now=NOW)
    assert "承認が要る" in why


def test_時刻だけでは通らない():
    future = (NOW + timedelta(hours=8)).isoformat()
    assert "承認が要る" in BC.require_gate(_plan(n_new=999), future, "", now=NOW)


def test_直近の時刻は拒否する():
    """★過去や直近を許すとその場で値を入れて即実行でき、ゲートが名前だけになる."""
    soon = (NOW + timedelta(hours=1)).isoformat()
    why = BC.require_gate(_plan(n_new=999), soon, "fuji1", now=NOW)
    assert "タイムゲート未到達" in why


def test_過去の時刻は拒否する():
    past = (NOW - timedelta(days=1)).isoformat()
    assert "タイムゲート未到達" in BC.require_gate(
        _plan(n_new=999), past, "fuji1", now=NOW)


def test_タイムゾーンなしは拒否する():
    """+09:00 を省くと、どの時刻を宣言したのか一意に決まらない."""
    naive = "2026-08-30T02:00:00"
    why = BC.require_gate(_plan(n_new=999), naive, "fuji1", now=NOW)
    assert "タイムゾーン" in why


def test_解釈できない時刻は拒否する():
    why = BC.require_gate(_plan(n_new=999), "明日の夜", "fuji1", now=NOW)
    assert "解釈できない" in why


def test_不足時間を具体的に伝える():
    """あと何時間待てばよいかが分からないと、現場は闇雲に再実行する."""
    soon = (NOW + timedelta(hours=2)).isoformat()
    why = BC.require_gate(_plan(n_new=999), soon, "fuji1", now=NOW)
    assert "不足" in why and "h" in why
