# -*- coding: utf-8 -*-
"""sync_sheet_url.plan の回帰テスト (ネットワーク不要)。

実行: python -m pytest tests/test_sync_sheet_url.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.job_application_sync import sync_sheet_url as ssu  # noqa: E402

LIVE = "52016156"      # 定期1 (生きている契約)
OLD = "https://docs.google.com/spreadsheets/d/old"
NEW = "https://docs.google.com/spreadsheets/d/new"


def _deal(url, stage=LIVE, start="2026-01-01", name="サブスク継続"):
    return {"customer_sheet_url": url, "dealstage": stage, "dealname": name,
            "contract_start_date": start, "createdate": start}


def test_fills_only_empty_listing_with_current_contract_url():
    deals = {"d1": _deal(OLD, start="2025-01-01"), "d2": _deal(NEW, start="2026-04-01")}
    l2d = {"l1": ["d1", "d2"]}
    fill, differ, c = ssu.plan(["l1"], l2d, deals, {"l1": {}})
    assert fill == [{"id": "l1", "properties": {"customer_sheet_url": NEW}}]
    assert differ == [] and c["fill"] == 1


def test_does_not_overwrite_a_different_value():
    deals = {"d1": _deal(NEW)}
    fill, differ, c = ssu.plan(["l1"], {"l1": ["d1"]}, deals,
                               {"l1": {"customer_sheet_url": OLD}})
    assert fill == [] and differ == ["l1"] and c["different_skipped"] == 1


def test_same_value_and_blank_deal_are_left_alone():
    deals = {"d1": _deal(NEW), "d2": _deal("")}
    listings = {"l1": {"customer_sheet_url": " " + NEW + " "}, "l2": {}}
    fill, differ, c = ssu.plan(["l1", "l2"], {"l1": ["d1"], "l2": ["d2"]}, deals, listings)
    assert fill == [] and differ == []
    assert c["same"] == 1 and c["no_url"] == 1


def test_option_contract_does_not_beat_live_main_contract():
    deals = {"main": _deal(OLD, start="2025-01-01"),
             "opt": _deal(NEW, stage="1049738304", start="2026-06-01", name="求人追加")}
    fill, _, _ = ssu.plan(["l1"], {"l1": ["main", "opt"]}, deals, {"l1": {}})
    assert fill[0]["properties"]["customer_sheet_url"] == OLD
