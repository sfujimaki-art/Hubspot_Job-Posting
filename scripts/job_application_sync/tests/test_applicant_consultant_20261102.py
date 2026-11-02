"""応募者の「担当コンサルタント」(dandangkonsarutanto) に、今の契約のコンサル担当の名前を写す (2026-11-02)。

応募者の担当者 (hubspot_owner_id) = 取引担当者 はそのまま。値・氏名はすべてダミー (公開リポジトリ)。
"""
from __future__ import annotations

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import deal_stages as DS

LIVE = sorted(DS.WRITABLE_STAGES)[0]


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body

    def json(self):
        return self._b


def _deals():
    return {   # 生きている取引 (開始が古い) と、解約済の取引 (開始が新しい)
        "D1": {"dealname": "取引A", "dealstage": LIVE, "contract_start_date": "2025-01-01",
               "consultant": "O-LIVE", "hubspot_owner_id": "O-SALES"},
        "D2": {"dealname": "取引B", "dealstage": "52016159", "contract_start_date": "2026-04-01",
               "consultant": "O-ENDED"},
    }


def test_今の契約の取引のコンサル担当を選ぶ():
    d = _deals()
    assert ai.consultant_owner_id(list(d), d) == "O-LIVE"


def test_コンサル担当が空か取引が無ければNone():
    d = {"D1": {"dealstage": LIVE, "contract_start_date": "2025-01-01", "consultant": ""}}
    assert ai.consultant_owner_id(list(d), d) is None
    assert ai.consultant_owner_id([], {}) is None


def test_名前は姓名を空白なしでつなぎNFKCにする():
    assert ai.owner_display_name({"lastName": "見⽊", "firstName": "太郎"}) == "見木太郎"
    assert ai.owner_display_name({"lastName": "例示 ", "firstName": "　花子"}) == "例示花子"
    assert ai.owner_display_name({"lastName": "", "firstName": ""}) is None


def _client(monkeypatch, deals, owners):
    cli = ai.RealHubSpotClient("dummy")
    calls: list = []

    def get(url, **k):
        calls.append(url)
        for oid, rec in owners.items():
            if f"/owners/{oid}?" in url:
                return _Resp(200, rec)
        if "/owners/" in url:
            return _Resp(404, {})
        return _Resp(200, {"properties": {}})
    monkeypatch.setattr(cli._requests, "get", get)
    monkeypatch.setattr(cli, "_shop_is_shared", lambda shop: False)
    monkeypatch.setattr(cli, "_resolve_deal_group", lambda l, m, lo: (list(deals), deals))
    return cli, calls


def test_応募先の項目に担当コンサルタントが入る_担当者は触らない(monkeypatch):
    cli, calls = _client(monkeypatch, _deals(),
                         {"O-LIVE": {"id": "O-LIVE", "lastName": "見本", "firstName": "太郎"}})
    props = cli.get_oubosaki_props("L-1", "HRハッカー", "", "123")
    assert props["dandangkonsarutanto"] == "見本太郎"
    assert props["oubosaki_torihiki_name"] == "取引A"   # 取引名と同じ取引
    assert "hubspot_owner_id" not in props
    assert all("dummy" not in u for u in calls)


def test_コンサル担当の名前が取れなければ入れない(monkeypatch):
    cli, _ = _client(monkeypatch, _deals(), {})
    assert "dandangkonsarutanto" not in cli.get_oubosaki_props("L-1", "HRハッカー", "", "123")


def test_姓は従来どおり_同じownerは1回だけ引く(monkeypatch):
    cli, calls = _client(monkeypatch, _deals(),
                         {"O-LIVE": {"id": "O-LIVE", "lastName": "見本", "firstName": "太郎"}})
    assert cli.get_owner_last_name("O-LIVE") == "見本"
    assert cli.get_owner_full_name("O-LIVE") == "見本太郎"
    assert sum("/owners/O-LIVE" in u for u in calls) == 1
