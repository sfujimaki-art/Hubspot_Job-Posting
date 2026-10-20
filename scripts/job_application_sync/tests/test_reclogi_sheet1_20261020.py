"""シート1 (現場の作業シート) のリクロジ行に 顧客=応募者ページURL / 担当CS=コンサル担当の姓 を書く (2026-10-20)。

行は messageId 列で特定し、書く直前に読み直して同じIDか確かめる。
顧客は空か宛先 (support@reclogi.jp) のときだけ、担当CS は空のときだけ書く。
値・氏名はすべてダミー (公開リポジトリ)。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync import reclogi_applicants as rl
from scripts.job_application_sync.tests.test_reclogi_applicants_20261020 import (
    FakeClient, FakeWs, _row, _values)

S1_HEADER = ["件名", "媒体", "顧客", "日付", "会社名", "マーカー", "担当CS", "応募企業名",
             "messageId（自動転記用・編集しないでください）"]
URL = "https://app.hubspot.com/contacts/23708633/record/0-421/{}"


def _s1(rows: list) -> FakeWs:
    """rows: [(messageId, 顧客, 担当CS)]"""
    vals = [list(S1_HEADER)]
    for mid, cust, cs in rows:
        vals.append(["応募がありました", "x", cust, "2026/10/20", "", "", cs, "", mid])
    return FakeWs(vals, title="シート1")


def _item(mid="msg-0001", appt="9001", listing="L-1", received=""):
    return {"key": f"RL-{mid}", "mid": mid, "appt": appt, "listing": listing,
            "received": received}


class S1Client(FakeClient):
    """取引のコンサル担当と owner の姓を返す。owner の呼び出し回数を数える。"""

    def __init__(self, consultants=None, names=None, appt_listing=None, **kw):
        super().__init__(**kw)
        self.consultants = consultants or {}
        self.names = names or {}
        self.appt_listing = appt_listing or {}
        self.owner_calls: list = []

    def get_listing_consultant(self, listing_id, media="", login_id=""):
        return self.consultants.get(listing_id)

    def get_owner_last_name(self, owner_id):
        self.owner_calls.append(owner_id)
        return self.names.get(owner_id)

    def get_appointment_listing(self, appointment_id):
        return self.appt_listing.get(appointment_id)


def _cli():
    return S1Client(consultants={"L-1": "O-1"}, names={"O-1": "見本"})


# ---------------------------------------------------------------- 列と上書きの条件
def test_列はヘッダ名で引く_並びが変わっても同じ():
    c = rl.s1_columns(S1_HEADER)
    assert (c["id"], c["customer"], c["cs"]) == (8, 2, 6)
    c2 = rl.s1_columns(list(reversed(S1_HEADER)) + ["追加列"])
    assert (c2["id"], c2["customer"], c2["cs"]) == (0, 6, 2)
    assert rl.s1_columns(["件名", "messageId"])["cs"] is None


@pytest.mark.parametrize("cur, ok", [
    ("", True), ("  ", True), ("support@reclogi.jp", True), ("SUPPORT@reclogi.jp ", True),
    ("リクロジ <support@reclogi.jp>", True),
    ("https://app.hubspot.com/contacts/23708633/record/0-421/1", False),   # 書いた後
    ("someone@example.com", False),                                        # 現場が書き換えた
    ("support@reclogi.jp, someone@example.com", False),
    ("担当に確認中", False)])
def test_顧客は空か宛先のままのときだけ書く(cur, ok):
    assert rl.customer_writable(cur) is ok


@pytest.mark.parametrize("cur, ok", [("", True), (" ", True), ("見本", False)])
def test_担当CSは空のときだけ書く(cur, ok):
    assert rl.cs_writable(cur) is ok


# ---------------------------------------------------------------- 書き込み
def test_顧客にURL_担当CSに姓を書いて台帳に済を残す(tmp_path):
    ws1 = _s1([("msg-other", "a@example.com", "x"), ("msg-0001", "support@reclogi.jp", "")])
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item()], _cli(), ledger)
    assert ws1.cell(3, "顧客") == URL.format("9001")
    assert ws1.cell(3, "担当CS") == "見本"
    assert ws1.cell(2, "顧客") == "a@example.com"          # 別の行は触らない
    assert out["s1_rows"] == 1 and out["s1_customer"] == 1 and out["s1_cs"] == 1
    assert ledger.data["RL-msg-0001"]["s1"] == "done"


def test_現場が書き換えた値は潰さない(tmp_path):
    ws1 = _s1([("msg-0001", "someone@example.com", "見本二")])
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item()], _cli(), ledger)
    assert ws1.writes == []
    assert ws1.cell(2, "顧客") == "someone@example.com" and ws1.cell(2, "担当CS") == "見本二"
    assert out["s1_rows"] == 1 and ledger.data["RL-msg-0001"]["s1"] == "done"


def test_姓が取れなければ担当CSは書かない():
    ws1 = _s1([("msg-0001", "", "")])
    rl.sync_sheet1(ws1, [_item(listing="")], S1Client())   # 求人に紐付かない応募
    assert ws1.cell(2, "顧客") == URL.format("9001") and ws1.cell(2, "担当CS") == ""


def test_求人が分からなければ応募者の紐付けから引く():
    ws1 = _s1([("msg-0001", "", "")])
    cli = S1Client(consultants={"L-9": "O-1"}, names={"O-1": "見本"},
                   appt_listing={"9001": "L-9"})
    rl.sync_sheet1(ws1, [_item(listing="")], cli)
    assert ws1.cell(2, "担当CS") == "見本"


def test_担当CSの列が無ければ顧客だけ書いて警告する(capsys):
    hdr = [h for h in S1_HEADER if h != "担当CS"]
    ws1 = FakeWs([hdr, ["応募がありました", "x", "", "2026/10/20", "", "", "", "msg-0001"]],
                 title="シート1")
    rl.sync_sheet1(ws1, [_item()], _cli())
    assert ws1.cell(2, "顧客") == URL.format("9001")
    assert "担当CS" in capsys.readouterr().out


def test_messageId列が無ければ何も書かない():
    ws1 = FakeWs([["件名", "顧客", "担当CS"], ["x", "", ""]], title="シート1")
    out = rl.sync_sheet1(ws1, [_item()], _cli())
    assert ws1.writes == [] and out["s1_waiting"] == 1


def test_読んでから行が入れ替わったら書かない(tmp_path):
    ws1 = _s1([("msg-0001", "", "")])
    orig = ws1.batch_get

    def moved(ranges):                    # 行番号を引いた後で、その行が別の応募に替わった
        ws1.values[1][8] = "msg-9999"
        return orig(ranges)
    ws1.batch_get = moved
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item()], _cli(), ledger)
    assert ws1.writes == [] and out["s1_waiting"] == 1
    assert "RL-msg-0001" not in ledger.data           # 次の回に引き直す


# ---------------------------------------------------------------- 行が無い・再試行・諦め
def test_行が無いときは書かずに次の回へ_期限を過ぎたら諦める(tmp_path):
    ws1 = _s1([("msg-other", "", "")])
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item(received="2026-10-18")], _cli(), ledger,
                         today="2026-10-20")
    assert ws1.writes == [] and out["s1_waiting"] == 1
    assert "RL-msg-0001" not in ledger.data
    out = rl.sync_sheet1(ws1, [_item(received="2026-10-16")], _cli(), ledger,
                         today="2026-10-20")
    assert out["s1_gave_up"] == 1 and ledger.data["RL-msg-0001"]["s1"] == "gave_up"


def test_対象は応募者IDがありシート1が未済の行(tmp_path):
    ledger = S.Ledger(tmp_path / "l.json")
    ledger.data["RL-m3"] = {"status": "DONE", "s1": "done"}
    ledger.data["RL-m4"] = {"status": "DONE", "s1": "gave_up"}
    ledger.data["RL-m5"] = {"status": "DONE", "sheet_status": "受入済",
                            "appointment_id": "905", "listing_id": "L-5"}
    _, recs = rl.records_from_values(_values([
        _row(messageId="m1", HubSpot登録="受入済", HubSpot応募者ID="901"),
        _row(messageId="m2", HubSpot登録="求人未特定", HubSpot応募者ID="902"),
        _row(messageId="m3", HubSpot登録="受入済", HubSpot応募者ID="903"),
        _row(messageId="m4", HubSpot登録="受入済", HubSpot応募者ID="904"),
        _row(messageId="m5"),                       # 書き戻し前 (台帳にだけ結果がある)
        _row(messageId="m6", HubSpot登録="エラー: x"),
        _row(messageId="m7", HubSpot登録="対象外"),
        _row(messageId="m8"),                       # この回に作った
    ]))
    items = rl.s1_items(recs, ledger, {"RL-m8": ("908", "L-8")})
    got = {it["mid"]: (it["appt"], it["listing"], it["received"]) for it in items}
    assert got == {"m1": ("901", "", "2026-10-14"), "m2": ("902", "", "2026-10-14"),
                   "m5": ("905", "L-5", "2026-10-14"), "m8": ("908", "L-8", "")}


# ---------------------------------------------------------------- run の中で
def test_応募者を作った回にシート1も書く(tmp_path):
    ws = FakeWs(_values([_row()]))
    ws1 = _s1([("msg-0001", "support@reclogi.jp", "")])
    cli = S1Client(consultants={"L-1": "O-1"}, names={"O-1": "見本"},
                   listings_reclogi={"10756": "L-1"})
    ledger = S.Ledger(tmp_path / "l.json")
    s = rl.run(dry_run=False, ledger=ledger, ws=ws, ws1=ws1, client=cli)
    appt = cli.created_appts[0]["id"]
    assert ws1.cell(2, "顧客") == URL.format(appt) and ws1.cell(2, "担当CS") == "見本"
    assert s["s1_rows"] == 1
    e = ledger.data["RL-msg-0001"]
    assert e["s1"] == "done" and e["listing_id"] == "L-1" and e["appointment_id"] == appt


def test_シート1の行が後から来たら次の回に書く(tmp_path):
    ws = FakeWs(_values([_row()]))
    ws1 = _s1([])                                         # GAS の転記がまだ
    cli = S1Client(consultants={"L-1": "O-1"}, names={"O-1": "見本"},
                   listings_reclogi={"10756": "L-1"})
    ledger = S.Ledger(tmp_path / "l.json")
    s = rl.run(dry_run=False, ledger=ledger, ws=ws, ws1=ws1, client=cli)
    assert s["s1_waiting"] == 1 and "s1" not in ledger.data["RL-msg-0001"]
    ws1.values.append(["応募がありました", "x", "support@reclogi.jp", "", "", "", "", "",
                       "msg-0001"])
    s2 = rl.run(dry_run=False, ledger=ledger, ws=ws, ws1=ws1, client=cli)
    assert s2["targets"] == 0 and len(cli.created_appts) == 1   # 応募者は作り直さない
    assert s2["s1_rows"] == 1 and ws1.cell(2, "担当CS") == "見本"
    assert ws1.cell(2, "顧客") == URL.format(cli.created_appts[0]["id"])


def test_dry_runではシート1に書かない(tmp_path):
    ws = FakeWs(_values([_row(HubSpot登録="受入済", HubSpot応募者ID="901")]))
    ws1 = _s1([("msg-0001", "", "")])
    s = rl.run(dry_run=True, ledger=S.Ledger(tmp_path / "l.json"), ws=ws, ws1=ws1,
               client=_cli())
    assert ws1.writes == [] and ws.writes == []
    assert s["targets"] == 0


def test_シート1が落ちても取り込みと書き戻しは済む(tmp_path):
    class Broken(FakeWs):
        def row_values(self, n):
            raise RuntimeError("503 (test)")
    ws = FakeWs(_values([_row()]))
    cli = S1Client(listings_reclogi={"10756": "L-1"})
    ledger = S.Ledger(tmp_path / "l.json")
    s = rl.run(dry_run=False, ledger=ledger, ws=ws, ws1=Broken([S1_HEADER]), client=cli)
    assert s["linked"] == 1 and ws.cell(2, "HubSpot登録") == "受入済"
    assert "s1" not in ledger.data["RL-msg-0001"]     # 次の回に再試行


# ---------------------------------------------------------------- HubSpot 側 (コンサル担当と姓)
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def test_複数取引ならlatest_liveの取引のコンサル担当を選ぶ(monkeypatch):
    from scripts.job_application_sync import deal_stages as DS
    cli = ai.RealHubSpotClient("dummy")
    live = sorted(DS.WRITABLE_STAGES)[0]
    deals = {   # 生きている取引 (開始が古い) と、解約済の取引 (開始が新しい)
        "D1": {"dealname": "取引A", "dealstage": live, "contract_start_date": "2025-01-01",
               "consultant": "O-LIVE"},
        "D2": {"dealname": "取引B", "dealstage": "52016159", "contract_start_date": "2026-04-01",
               "consultant": "O-ENDED"},
    }
    monkeypatch.setattr(cli._requests, "get",
                        lambda url, **k: _Resp(200, {"properties": {}}))
    monkeypatch.setattr(cli, "_shop_is_shared", lambda shop: False)
    monkeypatch.setattr(cli, "_resolve_deal_group", lambda l, m, lo: (list(deals), deals))
    assert ai.DM.latest_live(list(deals), deals) == "D1"
    assert cli.get_listing_consultant("L-1", "リクロジメディア") == "O-LIVE"
    # 応募へ写す取引名も同じ取引のもの
    assert ai.deal_current_props(list(deals), deals)["oubosaki_torihiki_name"] == "取引A"


def test_コンサル担当が決まらなければNone(monkeypatch):
    cli = ai.RealHubSpotClient("dummy")
    monkeypatch.setattr(cli._requests, "get",
                        lambda url, **k: _Resp(200, {"properties": {"id_shop_hrhakkaa": "1"}}))
    monkeypatch.setattr(cli, "_shop_is_shared", lambda shop: True)   # 別会社と共有
    assert cli.get_listing_consultant("L-1") is None


def test_ownerの姓を取り出し_同じownerは1回だけ引く(monkeypatch):
    cli = ai.RealHubSpotClient("dummy")
    calls: list = []

    def get(url, **k):
        calls.append(url)
        if "/owners/O-1" in url:
            return _Resp(200, {"id": "O-1", "firstName": "太郎", "lastName": "見本"})
        if "/owners/O-2" in url and "archived=false" in url:
            return _Resp(404, {})
        if "/owners/O-2" in url:                     # 無効化されたユーザー
            return _Resp(200, {"id": "O-2", "firstName": "花子", "lastName": "例示"})
        return _Resp(404, {})
    monkeypatch.setattr(cli._requests, "get", get)
    assert cli.get_owner_last_name("O-1") == "見本"
    assert cli.get_owner_last_name("O-1") == "見本"
    assert sum("/owners/O-1" in u for u in calls) == 1
    assert cli.get_owner_last_name("O-2") == "例示"
    assert cli.get_owner_last_name("O-3") is None
    assert cli.get_owner_last_name("") is None
    assert all("dummy" not in u for u in calls)       # トークンを URL に載せない


# ---------------------------------------------------------------- messageId 列を中身で引く
def _s1_no_header(rows: list, mid_col: int = 26, extra_hex_col: int = None) -> FakeWs:
    """見出しが messageId でない (AA1 にデータの値が入った) シート1。rows: [(mid, 顧客, 担当CS)]"""
    hdr = S1_HEADER[:8] + [""] * (mid_col - 8) + ["1a149561b4366559"]
    vals = [hdr]
    for mid, cust, cs in rows:
        r = ["応募がありました", "x", cust, "2026/10/20", "", "", cs, ""] + [""] * (mid_col - 8)
        r.append(mid)
        if extra_hex_col is not None:
            while len(r) <= extra_hex_col:
                r.append("")
            r[extra_hex_col] = "0123456789abcdef"
        vals.append(r)
    return FakeWs(vals, title="シート1")


def _hex(i: int) -> str:
    return f"{i:016x}"


def test_見出しが無ければ値の過半が16桁16進の列をmessageIdとみなす(tmp_path):
    rows = [(_hex(i), "a@example.com", "x") for i in range(1, 6)]
    rows += [("", "b@example.com", "x"), (_hex(99), "support@reclogi.jp", "")]
    ws1 = _s1_no_header(rows)
    assert rl.resolve_s1_columns(ws1)["id"] == 26             # AA
    ledger = S.Ledger(tmp_path / "l.json")
    rl.sync_sheet1(ws1, [_item(mid=_hex(99))], _cli(), ledger)
    assert ws1.cell(8, "顧客") == URL.format("9001") and ws1.cell(8, "担当CS") == "見本"
    assert ws1.cell(1, "顧客") == "顧客"                       # 1行目 (見出し) は触らない


def test_中身の候補が2列以上なら書かずに警告する(capsys):
    rows = [(_hex(i), "", "") for i in range(1, 6)]
    ws1 = _s1_no_header(rows, extra_hex_col=20)   # U列にも16進
    assert rl.resolve_s1_columns(ws1)["id"] is None
    out = rl.sync_sheet1(ws1, [_item(mid=_hex(1))], _cli())
    assert ws1.writes == [] and out["s1_waiting"] == 1
    assert "候補が複数" in capsys.readouterr().out


def test_過半に満たない列や値が少なすぎる列は候補にしない():
    block = [["x", "0123456789abcdef", _hex(1)],
             ["x", "メモ", _hex(2)],
             ["x", "メモ", _hex(3)],
             ["x", "", "zz"]]
    assert rl.msgid_column_by_content(block) == (2, [2])
    assert rl.msgid_column_by_content(block[:2]) == (None, [])   # 値が2つしかない


def test_見出しがあれば中身は読まない():
    ws1 = _s1([("msg-0001", "", "")])
    ws1.get = lambda rng: pytest.fail("中身は読まない")
    assert rl.resolve_s1_columns(ws1)["id"] == 8


# ---------------------------------------------------------------- 姓が取れない行は担当CSだけ再試行
def test_姓が取れなければ顧客だけ書いてpartial_次の回に担当CSを書く(tmp_path):
    ws1 = _s1([("msg-0001", "support@reclogi.jp", "")])
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item()], S1Client(), ledger)      # 取引がまだ無い
    assert ws1.cell(2, "顧客") == URL.format("9001") and ws1.cell(2, "担当CS") == ""
    assert out["s1_partial"] == 1 and ledger.data["RL-msg-0001"]["s1"] == "partial"
    # 次の回: 対象に残り、顧客は書き直さず担当CSだけ書く
    _, recs = rl.records_from_values(_values([_row(HubSpot登録="受入済", HubSpot応募者ID="9001")]))
    items = rl.s1_items(recs, ledger, {})
    assert [it["mid"] for it in items] == ["msg-0001"]
    n_writes = len(ws1.writes)
    cli = S1Client(consultants={"L-1": "O-1"}, names={"O-1": "見本"},
                   appt_listing={"9001": "L-1"})          # 夜のうちに取引が付いた
    out = rl.sync_sheet1(ws1, items, cli, ledger, today="2026-10-15")
    assert ws1.cell(2, "担当CS") == "見本" and out["s1_customer"] == 0
    assert ledger.data["RL-msg-0001"]["s1"] == "done"
    assert [d["range"].split("!")[-1] for d in ws1.writes[n_writes]] == ["G2"]


def test_姓が取れないまま期限を過ぎたら諦める(tmp_path):
    ws1 = _s1([("msg-0001", "", "")])
    ledger = S.Ledger(tmp_path / "l.json")
    out = rl.sync_sheet1(ws1, [_item(received="2026-10-10")], S1Client(), ledger,
                         today="2026-10-20")
    assert out["s1_gave_up"] == 1 and ledger.data["RL-msg-0001"]["s1"] == "gave_up"
    assert ws1.cell(2, "顧客") == URL.format("9001")       # 顧客は書ける分を書く


def test_partialの間に現場が担当CSを書いたら済にする(tmp_path):
    ws1 = _s1([("msg-0001", URL.format("9001"), "見本二")])
    ledger = S.Ledger(tmp_path / "l.json")
    ledger.data["RL-msg-0001"] = {"status": "DONE", "s1": "partial"}
    rl.sync_sheet1(ws1, [_item()], S1Client(), ledger)
    assert ws1.writes == [] and ledger.data["RL-msg-0001"]["s1"] == "done"
