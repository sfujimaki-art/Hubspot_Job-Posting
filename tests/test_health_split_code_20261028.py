# -*- coding: utf-8 -*-
"""要対応リスト: 生きている取引の計上コードが割れていると、鍵を入れ替えても求人が移らない (2026-10-28)。"""
from scripts.job_application_sync import health_check as hc
from scripts.job_application_sync import deal_stages as DS

LIVE = sorted(DS.WRITABLE_STAGES)[0]
KEIJO = sorted(DS.PIPELINES_KEIJO)[0]


def _stub(monkeypatch, keijo_codes):
    monkeypatch.setattr(hc, "search_all", lambda obj, props, filters, **kw: [
        {"id": "200", "properties": {"dealname": "継続②", "dealstage": LIVE}},
        {"id": "100", "properties": {"dealname": "継続①", "dealstage": "66848546"}}])
    monkeypatch.setattr(hc, "_assoc", lambda f, t, ids: {"200": [f"k{i}" for i in range(len(keijo_codes))]})
    monkeypatch.setattr(hc, "_batch_props", lambda obj, ids, props: {
        f"k{i}": {"pipeline": KEIJO, DS.PROP_CODE: c} for i, c in enumerate(keijo_codes)})


def test_split_live_deal_is_reported(monkeypatch):
    _stub(monkeypatch, ["RL1", "RL2"])
    assert hc._split_live_deals({"RL1"}) == {"RL1": [("200", "継続②", ["RL1", "RL2"])]}


def test_single_code_is_not_reported(monkeypatch):
    _stub(monkeypatch, ["RL1", "RL1"])
    assert hc._split_live_deals({"RL1"}) == {}


def test_read_failure_keeps_old_instruction(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429")
    monkeypatch.setattr(hc, "search_all", boom)
    assert hc._split_live_deals({"RL1"}) == {}
