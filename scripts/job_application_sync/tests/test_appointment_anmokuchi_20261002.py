"""応募カードへの一次対応8項目(暗黙知)の転記 (2026-10-02)。

## 守る不変条件 (2026-10-01 定例MTG決定)

1. 作成時: 取引群のマスター (契約期間が最も新しく中身のある取引) から
   8項目と「転記済みの印」を書く。マスターが無ければ何も書かない (印も)。
2. 今の契約の属性 (取引名・要否) は生きている取引の最新から取る
   (枝番では選ばない。終わった取引の要否を写さない)。
3. 夜間補完: 印のある応募は**絶対に触らない** (親が後で変わっても上書きしない)。
4. 要否は空のときだけ補完し、古い応募には付けない (作成時と同じガード)。
5. テンプレートのままの値は写さない。
6. 窓は FLOOR と「直近30日」の新しい方。
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import applicant_import as AI  # noqa: E402
import backfill_appointment_anmokuchi as B  # noqa: E402
import deal_master as DM  # noqa: E402

LIVE = "52016156"
ENDED = "66848546"
TPL_ASHI = DM._TEMPLATES["ashigirijouken"]
TPL_HOUKOKU = DM._TEMPLATES["oubohoukokusaki"]
NOW = 1759300000000


def deal(stage, start, **kw):
    p = {"dealstage": stage, "contract_start_date": start, "createdate": start,
         "ashigirijouken": TPL_ASHI, "oubohoukokusaki": TPL_HOUKOKU,
         "dealname": "", "itijitaiou": ""}
    p.update(kw)
    return p


# ---- 1. 作成時の転記 -----------------------------------------------------------

def test_作成時_マスターの8項目と印を書く():
    deals = {"old": deal(ENDED, "2026-01-01", keikenumukakunin="フォーク1年"),
             "new": deal(LIVE, "2026-07-01")}
    p = AI.anmokuchi_transfer_props(["old", "new"], deals, now_ms=NOW)
    assert p["anmokuchi_keikenumukakunin"] == "フォーク1年"
    assert p[DM.APPT_TRANSFERRED_AT] == str(NOW)
    assert p[DM.APPT_TRANSFERRED_FROM] == "old"


def test_作成時_マスターが無ければ印も書かない():
    deals = {"a": deal(LIVE, "2026-07-01")}
    assert AI.anmokuchi_transfer_props(["a"], deals, now_ms=NOW) == {}


def test_テンプレートは写さない():
    deals = {"a": deal(LIVE, "2026-07-01", shodoutaiou="書類回収")}
    p = AI.anmokuchi_transfer_props(["a"], deals, now_ms=NOW)
    assert "anmokuchi_ashigirijouken" not in p
    assert "anmokuchi_oubohoukokusaki" not in p
    assert p["anmokuchi_shodoutaiou"] == "書類回収"


# ---- 2. 今の契約の属性 ---------------------------------------------------------

def test_取引名と要否は生きている取引から取る():
    deals = {"s1": deal(ENDED, "2026-08-01", dealname="サブスク継続②＿A社", itijitaiou="true"),
             "re": deal(LIVE, "2026-06-01", dealname="再契約＿A社", itijitaiou="false")}
    p = AI.deal_current_props(["s1", "re"], deals)
    assert p["oubosaki_torihiki_name"] == "再契約＿A社"
    assert p["ichijitaiounoumu"] == "不要"


def test_要否が未設定の取引からは要否を写さない():
    deals = {"a": deal(LIVE, "2026-06-01", dealname="A社")}
    assert "ichijitaiounoumu" not in AI.deal_current_props(["a"], deals)


# ---- 3/4. 夜間補完 -------------------------------------------------------------

TODAY = datetime.now().strftime("%Y-%m-%d")


def test_夜間_印のある応募は親が変わっても触らない():
    deals = {"a": deal(LIVE, "2026-07-01", keikenumukakunin="変更後の条件", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "1759000000000", "ichijitaiounoumu": "必要",
            "yingmuri": TODAY}
    assert B.plan_for_appt(appt, ["a"], deals, NOW) == {}


def test_夜間_印が空でマスターがあれば転記する():
    deals = {"a": deal(LIVE, "2026-07-01", keikenumukakunin="条件", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "", "ichijitaiounoumu": "必要", "yingmuri": TODAY}
    p = B.plan_for_appt(appt, ["a"], deals, NOW)
    assert p["anmokuchi_keikenumukakunin"] == "条件"
    assert DM.APPT_TRANSFERRED_AT in p
    assert "ichijitaiounoumu" not in p          # 入っている要否は上書きしない


def test_夜間_マスターが無ければ何も書かない_翌晩また見る():
    deals = {"a": deal(LIVE, "2026-07-01", itijitaiou="true")}
    appt = {"ichijitaiounoumu": "必要", "yingmuri": TODAY}
    assert B.plan_for_appt(appt, ["a"], deals, NOW) == {}


def test_夜間_要否は空のときだけ補完する():
    deals = {"a": deal(LIVE, "2026-07-01", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "1", "ichijitaiounoumu": "", "yingmuri": TODAY}
    assert B.plan_for_appt(appt, ["a"], deals, NOW, today=TODAY) == {"ichijitaiounoumu": "必要"}


def test_夜間_古い応募には要否を付けない():
    deals = {"a": deal(LIVE, "2026-07-01", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "1", "ichijitaiounoumu": "", "yingmuri": "2026-01-01"}
    assert B.plan_for_appt(appt, ["a"], deals, NOW, today="2026-10-02") == {}


def test_夜間_取引が無い応募は何も書かない():
    appt = {"ichijitaiounoumu": "", "yingmuri": TODAY}
    assert B.plan_for_appt(appt, [], {}, NOW) == {}


# ---- 6. 窓 ---------------------------------------------------------------------

def test_窓_FLOOR直後はFLOORから():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert B.window_start(now) == B.FLOOR


def test_窓_FLOORから30日を過ぎたら直近30日():
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert B.window_start(now) == "2026-11-01T00:00:00Z"


# ---- 作成時の経路 (RealHubSpotClient) をモックで通す ---------------------------

class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        return None


class _FakeRequests:
    """listing→deal 関連・deal batch/read・コード検索だけを返す偽物。"""

    def __init__(self, assoc, deals, shop=""):
        self.assoc, self.deals, self.shop = assoc, deals, shop

    def get(self, url, **kw):
        if "/associations/0-3" in url:
            return _Resp({"results": [{"toObjectId": d} for d in self.assoc]})
        return _Resp({"properties": {"id_shop_hrhakkaa": self.shop}})

    def post(self, url, json=None, **kw):
        if url.endswith("/0-3/batch/read"):
            ids = [i["id"] for i in json["inputs"]]
            return _Resp({"results": [{"id": i, "properties": self.deals[i]}
                                      for i in ids if i in self.deals]})
        if url.endswith("/0-3/search"):
            f0 = json["filterGroups"][0]["filters"][0]
            if f0["propertyName"] == "hrhacker_shop_ids":
                return _Resp({"results": [
                    {"id": i, "properties": p} for i, p in self.deals.items()
                    if f0["value"] in str(p.get("hrhacker_shop_ids") or "").split(";")]})
            code = f0["value"]
            return _Resp({"results": [{"id": i, "properties": p} for i, p in self.deals.items()
                                      if p.get("code_of_customer") == code]})
        return _Resp({})


def test_作成時経路_終わった取引にしか付いていない求人でも後継の生きた取引から取る():
    deals = {"old": deal(ENDED, "2025-04-01", dealname="旧", itijitaiou="true",
                         code_of_customer="RLX", keikenumukakunin="旧条件"),
             "new": deal(LIVE, "2026-04-01", dealname="新", itijitaiou="false",
                         code_of_customer="RLX")}
    cli = AI.RealHubSpotClient.__new__(AI.RealHubSpotClient)
    cli.BASE = "https://api.hubapi.com"
    cli.headers = {}
    cli._requests = _FakeRequests(["old"], deals)
    p = cli.get_oubosaki_props("L1", "HRハッカー", "", "")
    assert p["oubosaki_torihiki_name"] == "新"
    assert p["ichijitaiounoumu"] == "不要"
    # 新しい取引は空なので、中身のある旧取引がマスター
    assert p["anmokuchi_keikenumukakunin"] == "旧条件"
    assert p[DM.APPT_TRANSFERRED_FROM] == "old"


# ---- 2026-10-02 逆証明の是正 ------------------------------------------------

def test_夜間_要否が未設定unsetなら空とみなして補完する():
    deals = {"a": deal(LIVE, "2026-07-01", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "1", "ichijitaiounoumu": "unset", "yingmuri": TODAY}
    assert B.plan_for_appt(appt, ["a"], deals, NOW, today=TODAY) == {"ichijitaiounoumu": "必要"}


def test_夜間_応募日が空の応募には要否を入れない():
    # 古い応募か判断できない。BPOのキューに過去分を流さない
    deals = {"a": deal(LIVE, "2026-07-01", itijitaiou="true")}
    appt = {DM.APPT_TRANSFERRED_AT: "1", "ichijitaiounoumu": "", "yingmuri": ""}
    assert B.plan_for_appt(appt, ["a"], deals, NOW, today=TODAY) == {}


def test_夜間_今の契約はオプションより主契約():
    deals = {"m": deal(LIVE, "2026-04-01", itijitaiou="false", dealname="サブスク継続②＿A社"),
             "o": deal("1049738304", "2026-08-01", itijitaiou="true", dealname="求人追加＿A社")}
    appt = {DM.APPT_TRANSFERRED_AT: "1", "ichijitaiounoumu": "", "yingmuri": TODAY}
    assert B.plan_for_appt(appt, ["m", "o"], deals, NOW, today=TODAY) == {"ichijitaiounoumu": "不要"}


def test_作成時経路_店舗IDを別会社と共有する求人には取引由来の値を入れない():
    deals = {"a": deal(LIVE, "2026-04-01", dealname="A社", itijitaiou="true",
                       code_of_customer="RL1", hrhacker_shop_ids="S9",
                       keikenumukakunin="A社の条件"),
             "b": deal(LIVE, "2026-04-01", dealname="B社", itijitaiou="false",
                       code_of_customer="RL2", hrhacker_shop_ids="S9")}
    cli = AI.RealHubSpotClient.__new__(AI.RealHubSpotClient)
    cli.BASE = "https://api.hubapi.com"
    cli.headers = {}
    cli._requests = _FakeRequests(["a"], deals, shop="S9")
    p = cli.get_oubosaki_props("L1", "HRハッカー", "", "")
    for k in ("oubosaki_torihiki_name", "ichijitaiounoumu",
              "anmokuchi_keikenumukakunin", DM.APPT_TRANSFERRED_AT):
        assert k not in p


def test_作成時経路_店舗IDが自社の取引だけなら通常どおり入れる():
    deals = {"a": deal(LIVE, "2026-04-01", dealname="A社", itijitaiou="true",
                       code_of_customer="RL1", hrhacker_shop_ids="S1",
                       keikenumukakunin="A社の条件")}
    cli = AI.RealHubSpotClient.__new__(AI.RealHubSpotClient)
    cli.BASE = "https://api.hubapi.com"
    cli.headers = {}
    cli._requests = _FakeRequests(["a"], deals, shop="S1")
    p = cli.get_oubosaki_props("L1", "HRハッカー", "", "")
    assert p["oubosaki_torihiki_name"] == "A社" and p["anmokuchi_keikenumukakunin"] == "A社の条件"


def test_作成時経路_別会社の取引に同時に紐付いた求人には取引由来の値を入れない():
    deals = {"a": deal(LIVE, "2026-04-01", dealname="A社", itijitaiou="true",
                       code_of_customer="RL1", keikenumukakunin="A社の条件"),
             "b": deal(LIVE, "2026-05-01", dealname="B社", itijitaiou="false",
                       code_of_customer="RL2")}
    cli = AI.RealHubSpotClient.__new__(AI.RealHubSpotClient)
    cli.BASE = "https://api.hubapi.com"
    cli.headers = {}
    cli._requests = _FakeRequests(["a", "b"], deals)
    p = cli.get_oubosaki_props("L1", "HRハッカー", "", "")
    for k in ("oubosaki_torihiki_name", "ichijitaiounoumu", "anmokuchi_keikenumukakunin"):
        assert k not in p


def test_作成時経路_持ち主コードがあれば店舗IDの共有でもその会社の取引群から取る():
    deals = {"x": deal(LIVE, "2026-04-01", dealname="X社", itijitaiou="true",
                       code_of_customer="RLX", hrhacker_shop_ids="S9", keikenumukakunin="X社の条件"),
             "y": deal(LIVE, "2026-05-01", dealname="Y社", itijitaiou="false",
                       code_of_customer="RLY", hrhacker_shop_ids="S9")}

    class _Owned(_FakeRequests):
        def get(self, url, **kw):
            if "/associations/0-3" in url:
                return _Resp({"results": [{"toObjectId": d} for d in self.assoc]})
            return _Resp({"properties": {"id_shop_hrhakkaa": "S9", DM.LISTING_OWNER: "RLX"}})

    cli = AI.RealHubSpotClient.__new__(AI.RealHubSpotClient)
    cli.BASE = "https://api.hubapi.com"
    cli.headers = {}
    cli._requests = _Owned(["y"], deals)          # 今はY社の取引に付いている
    p = cli.get_oubosaki_props("L1", "HRハッカー", "", "")
    assert p["oubosaki_torihiki_name"] == "X社" and p["ichijitaiounoumu"] == "必要"
    assert p["anmokuchi_keikenumukakunin"] == "X社の条件"
