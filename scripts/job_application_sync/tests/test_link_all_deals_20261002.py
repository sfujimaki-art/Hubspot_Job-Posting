"""求人を同じ取引先コードの「生きている取引すべて」へ紐付ける (2026-10-02)。

## なぜ直したか

2026-10-01 実測: 直近14日の応募の43%が終わった取引にしか届いていなかった。
原因は2段:
  1. 新しい求人が作られた時点で終わった取引に付く。店舗ID・管理メールの索引を
     setdefault の先勝ちで作っており、検索の既定順 (作成日昇順) で古い取引が勝つ。
  2. 付け替え処理が「最新の取引に求人が1件でもあれば何もしない」ので直らない。

2026-10-01 定例MTGの方針: 関連する取引すべてに紐付ける。

## 守る不変条件

sync_deal_association (まだどこにも付いていない求人)
  1. 索引は「キー → 取引のリスト」。古い取引だけを返さない
  2. 候補の取引先コードが1つ → そのコードの生きている取引すべてに付ける
     (候補に入っていない、店舗IDを持たない新しい取引も含む)
  3. 生きている取引が無ければ1件だけ (候補とコードの取引のうち最新)
  4. 候補が別々の取引先コードにまたがる → 付けずに人の確認へ
  5. 既に何かに付いている求人には触らない (追加紐付けは relink の役割)
  6. 求人の担当者は、紐付く取引群の最新の生きている取引の担当者に毎晩そろえる。
     取引の担当者が空なら消さない
  7. dry-run では書き込みAPIを1回も呼ばない

relink_to_latest_deal (既に付いている求人の追加紐付け)
  8. コード内のどれかの取引に付いている求人を、生きている取引すべてへ足す
  9. 終わった取引へは足さない / 既にある紐付けは重ねない / 外さない
 10. コード無し・コード割れの取引は人の確認へ。割れた取引は系列に入れない
 11. 別々のコードの取引に付いている求人は広げず人の確認へ
 12. 「最新に求人が1件でもあれば何もしない」判定は無い
"""
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

from scripts.job_application_sync import sync_deal_association as SDA  # noqa: E402
from scripts.job_application_sync import relink_to_latest_deal as RL  # noqa: E402
from scripts.job_application_sync import deal_master as DM  # noqa: E402

LIVE = "52016156"      # 定期1
LIVE2 = "1049738304"   # オプション（求人追加・一次対応）
ENDED = "66848546"     # 継続済
KAIYAKU = "90598807"   # 解約済（充足）


def d(stage, code="", start="", created="", shops="", mails="", owner=""):
    return {"dealstage": stage, "code_of_customer": code,
            "contract_start_date": start, "createdate": created,
            "hrhacker_shop_ids": shops, "kanri_mail_address": mails,
            "hubspot_owner_id": owner}


# ============ sync_deal_association ==========================================

def test_索引は同じ店舗IDを持つ取引を全部返す():
    deals = {"100": d(ENDED, shops="S1"), "200": d(LIVE, shops="S1;S2"),
             "300": d(LIVE, mails="A@x.jp, b@x.jp")}
    shop, mail = SDA.build_indexes(deals)
    assert shop["S1"] == ["100", "200"]
    assert shop["S2"] == ["200"]
    assert mail["a@x.jp"] == ["300"] and mail["b@x.jp"] == ["300"]


def test_候補のコードが1つなら生きている取引すべてへ_店舗ID無しの新取引も含む():
    deals = {"old": d(ENDED, "RL1", "2025-01-01", shops="S1"),
             "new1": d(LIVE, "RL1", "2026-04-01"),          # 店舗IDを引き継いでいない
             "opt": d(LIVE2, "RL1", "2026-05-01"),
             "other": d(LIVE, "RL9")}
    by_code = DM.group_by_code(deals)
    targets, how = SDA.resolve_targets(["old"], deals, by_code)
    assert targets == ["new1", "opt"] and how == SDA.ST_CODE_LIVE


def test_生きている取引が無ければ最新1件だけ():
    deals = {"a": d(ENDED, "RL1", "2025-01-01"), "b": d(KAIYAKU, "RL1", "2025-06-01")}
    targets, how = SDA.resolve_targets(["a"], deals, DM.group_by_code(deals))
    assert targets == ["b"] and how == SDA.ST_CODE_NOLIVE


def test_候補が別の取引先コードにまたがるなら付けない():
    deals = {"a": d(LIVE, "RL1"), "b": d(LIVE, "RL2")}
    targets, how = SDA.resolve_targets(["a", "b"], deals, DM.group_by_code(deals))
    assert targets == [] and how == SDA.ST_MULTI


def test_コード無しの候補とコードありの候補はコードありの系列で決める():
    deals = {"nocode": d(ENDED, ""), "c": d(ENDED, "RL1"), "live": d(LIVE, "RL1")}
    targets, how = SDA.resolve_targets(["nocode", "c"], deals, DM.group_by_code(deals))
    assert targets == ["live"] and how == SDA.ST_CODE_LIVE


def test_コード無しの候補だけなら最新の生きている取引1件():
    deals = {"a": d(LIVE, "", "2025-01-01"), "b": d(LIVE, "", "2026-01-01"),
             "c": d(ENDED, "", "2026-06-01")}
    targets, how = SDA.resolve_targets(["a", "b", "c"], deals, DM.group_by_code(deals))
    assert targets == ["b"] and how == SDA.ST_NOCODE


def _listing(lid, shop="", login="", owner=""):
    return {"id": lid, "properties": {"id_shop_hrhakkaa": shop,
                                      "airwork_account_login_id": login,
                                      "hubspot_owner_id": owner}}


def test_新規紐付けの計画_既存はスキップ_HRとAWと別会社と未解決():
    deals = {"o": d(ENDED, "RL1", shops="S1"), "n": d(LIVE, "RL1"),
             "m": d(LIVE, "RL2", mails="k@x.jp"),
             "x": d(LIVE, "RL3", shops="S9"), "y": d(LIVE, "RL4", shops="S9")}
    by_code = DM.group_by_code(deals)
    shop, mail = SDA.build_indexes(deals)
    listings = [_listing("L0", shop="S1"), _listing("L1", shop="S1"),
                _listing("L2", login="aw1"), _listing("L3", shop="S9"),
                _listing("L4", shop="ZZ")]
    pairs, stat, review = SDA.plan_new_links(
        listings, {"L0": ["o"]}, shop, mail, {"aw1": "k@x.jp"}, deals, by_code)
    assert ("L1", "n", "hr") in pairs
    assert ("L2", "m", "aw") in pairs
    assert not any(p[0] in ("L0", "L3", "L4") for p in pairs)
    assert stat["already_linked"] == 1 and stat["unresolved"] == 1
    assert [r["求人ID"] for r in review] == ["L3"]
    # 公開リポジトリの成果物に載るので取引名(=顧客名)は出さない
    assert set(review[0]) == {"求人ID", "手がかり", "候補の取引ID", "候補の取引先コード"}


def test_担当者は最新の生きている取引の担当者で毎晩上書き():
    deals = {"old": d(ENDED, "RL1", "2025-01-01", owner="U_old"),
             "new": d(LIVE, "RL1", "2026-04-01", owner="U_new"),
             "blank": d(LIVE, "RL2", owner="")}
    by_code = DM.group_by_code(deals)
    listings = [_listing("L1", owner="U_old"), _listing("L2", owner="U_new"),
                _listing("L3", owner="U_keep")]
    to_set = SDA.plan_owner(listings, {"L1": ["old"], "L2": ["new"], "L3": ["blank"]},
                            deals, by_code, {"U_old", "U_new", "U_keep"})
    # 求人は古い取引にしか付いていなくても、同じコードの新しい取引の担当者になる
    assert to_set == [("L1", "U_new")]


def test_担当者は生きている取引が無ければ書き換えない():
    deals = {"e1": d(ENDED, "RL1", "2025-01-01", owner="U_a"),
             "e2": d(KAIYAKU, "RL1", "2025-06-01", owner="U_b")}
    to_set = SDA.plan_owner([_listing("L1", owner="U_x")], {"L1": ["e1"]},
                            deals, DM.group_by_code(deals), {"U_a", "U_b", "U_x"})
    assert to_set == []


def test_担当者は無効化ユーザーへは書き換えない():
    deals = {"n": d(LIVE, "RL1", "2026-04-01", owner="U_gone")}
    to_set = SDA.plan_owner([_listing("L1", owner="U_ok"), _listing("L2", owner="")],
                            {"L1": ["n"], "L2": ["n"]}, deals, DM.group_by_code(deals),
                            {"U_ok"})
    # 有効→無効の悪化も、空→無効も起こさない
    assert to_set == []


def test_一括紐付けは失敗した求人を数え成功扱いにしない():
    calls = []

    def fake_post(url, body):
        calls.append((url, body))
        return {"errors": [{"context": {"fromObjectId": ["L2"]}}], "numErrors": 1}

    with mock.patch.object(SDA, "_post_retry", side_effect=fake_post), \
         mock.patch.object(SDA.time, "sleep"):
        ok, fail = SDA.associate_batch([("L1", "D1"), ("L2", "D2")])
    assert ok == {("L1", "D1")} and fail == 1
    assert calls[0][0].endswith("/crm/v4/associations/0-420/0-3/batch/associate/default")


def test_一括紐付けの失敗内訳が読めなければ成功扱いにしない():
    with mock.patch.object(SDA, "_post_retry",
                           return_value={"errors": [{"message": "x"}], "numErrors": 2}), \
         mock.patch.object(SDA.time, "sleep"):
        ok, fail = SDA.associate_batch([("L1", "D1"), ("L2", "D2")])
    assert ok == set() and fail == 2


def test_一括紐付けは100件ずつ送る():
    sizes = []
    with mock.patch.object(SDA, "_post_retry",
                           side_effect=lambda u, b: sizes.append(len(b["inputs"])) or {}), \
         mock.patch.object(SDA.time, "sleep"):
        ok, fail = SDA.associate_batch([(f"L{i}", "D") for i in range(250)])
    assert sizes == [100, 100, 50] and len(ok) == 250 and fail == 0


def test_dry_runでは書き込みAPIを呼ばない(tmp_path):
    deals = {"o": d(ENDED, "RL1", shops="S1", owner="U1"),
             "n": d(LIVE, "RL1", owner="U2")}
    listings = [_listing("L1", shop="S1"), _listing("L2", shop="S1", owner="U1")]
    with mock.patch.object(SDA, "list_all", return_value=listings), \
         mock.patch.object(SDA, "_existing_deal_assoc", return_value={"L2": ["o"]}), \
         mock.patch.object(SDA, "load_deals", return_value=deals), \
         mock.patch.object(SDA, "build_login_to_mail", return_value={}), \
         mock.patch.object(SDA, "load_active_owners", return_value={"U1", "U2"}), \
         mock.patch.object(SDA, "_post_retry") as post, \
         mock.patch.object(SDA, "associate_batch") as assoc:
        res = SDA.run(dry_run=True, out_dir=tmp_path)
    post.assert_not_called()
    assoc.assert_not_called()
    assert res["associations"] == 1 and res["owner_target"] == 2


# ============ relink_to_latest_deal ==========================================

def test_relink_古い取引の求人を生きている取引すべてへ足す():
    deals = {"old": d(ENDED, "RL1"), "new": d(LIVE, "RL1"), "opt": d(LIVE2, "RL1")}
    d2l = {"old": ["L1", "L2"], "new": ["L2"]}
    pairs, manual, stat = RL.plan_all_live(deals, d2l, {}, {})
    assert sorted(pairs) == [("L1", "new"), ("L1", "opt"), ("L2", "opt")]
    assert manual == []


def test_relink_最新に求人が1件でもあっても足す():
    deals = {"old": d(ENDED, "RL1"), "new": d(LIVE, "RL1")}
    pairs, _m, _s = RL.plan_all_live(deals, {"old": ["L1"], "new": ["L9"]}, {}, {})
    assert pairs == [("L1", "new")]


def test_relink_終わった取引へは足さない_生きた取引が無いコードは触らない():
    deals = {"a": d(ENDED, "RL1"), "b": d(KAIYAKU, "RL1"),
             "c": d(ENDED, "RL2"), "e": d(LIVE, "RL2")}
    pairs, _m, stat = RL.plan_all_live(deals, {"a": ["L1"], "e": ["L5"]}, {}, {})
    assert pairs == []
    assert stat["生きている取引が無いコード(対象外)"] == 1


def test_relink_コード無しとコード割れは人の確認へ_割れた取引は系列に入れない():
    deals = {"nc": d(LIVE, ""), "sp": d(ENDED, "RL1"), "live": d(LIVE, "RL1")}
    d2d = {"sp": ["k1", "k2"]}
    keijo = {"k1": {"code_of_customer": "RL1"}, "k2": {"code_of_customer": "RL7"}}
    pairs, manual, _s = RL.plan_all_live(deals, {"sp": ["L1"], "nc": ["L2"]}, d2d, keijo)
    assert pairs == []        # 割れた取引の求人は RL1 へ運ばない
    kinds = sorted(r["区分"] for r in manual)
    assert kinds == sorted([RL.NG_NOCODE, RL.NG_SPLIT])


def test_relink_別々のコードに付いている求人は広げず人の確認へ():
    deals = {"a": d(ENDED, "RL1"), "a2": d(LIVE, "RL1"),
             "b": d(ENDED, "RL2"), "b2": d(LIVE, "RL2")}
    d2l = {"a": ["LX", "L1"], "b": ["LX"]}
    pairs, manual, stat = RL.plan_all_live(deals, d2l, {}, {})
    assert pairs == [("L1", "a2")]
    assert any(r["区分"] == RL.NG_CROSS for r in manual)
    assert stat["★別々の取引先コードに紐付いている求人(人の確認)"] == 1


def test_relink_dry_runでは書き込まない(tmp_path):
    deals = {"old": d(ENDED, "RL1"), "new": d(LIVE, "RL1")}
    with mock.patch.object(RL, "collect", return_value={
            "deals": deals, "d2l": {"old": ["L1"]}, "d2d": {}, "keijo": {}}), \
         mock.patch.object(RL, "apply") as ap:
        rc = RL.main(["--report-dir", str(tmp_path)])
    ap.assert_not_called()
    assert rc == 0
