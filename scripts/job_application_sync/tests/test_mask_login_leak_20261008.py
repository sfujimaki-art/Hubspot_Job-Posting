# -*- coding: utf-8 -*-
"""公開ログ・成果物にAWログインIDとメールを出さない (2026-10-08)。

本番リポジトリは public で、Actions のログと成果物は誰でも読める。
2026-10-05 に伏せた後も、次の2か所が生のまま出ていた:

1. aw_orchestrator の「顧客別 実行結果」サマリ。ログインIDと、`login_id=...` を
   含む例外文がそのまま。
2. health_check の要対応リスト (CSV 成果物)。「入れる鍵」「入れる場所」に
   AWログインIDと管理用メールがそのまま。
"""
from __future__ import annotations

import csv
import io
import re

import pytest

from scripts.job_application_sync import health_check as hc
from scripts.job_application_sync.fetchers import aw_orchestrator as orch

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ---------------------------------------------------------------- 1. orchestrator
def test_顧客別サマリはログインIDと理由を伏せる():
    out = "\n".join(orch._summary_lines([
        {"login_id": "okuser@example.jp", "company_name": "A社", "status": "ok",
         "result": {"creates": 1, "updates": 2}},
        {"login_id": "nguser-plain", "company_name": "B社", "status": "error",
         "error": "ログイン後 dashboards に遷移せず (login_id=nguser-plain, "
                  "last_url=https://example.jp/login) 連絡先 other@example.jp"},
        {"login_id": "", "company_name": "C社", "status": "error"},
    ]))
    assert "okuser@example.jp" not in out and "nguser-plain" not in out
    assert not EMAIL.search(out), "メール形式の値が1つも残らない"
    assert "ok…" in out and "ng…" in out, "どの行か分かる程度に先頭だけ残す"
    assert "新規=1" in out and "更新=2" in out
    assert "遷移せず" in out, "理由そのものは消さない"
    assert "C社 理由=不明" in out


# ---------------------------------------------------------------- 2. health_check
def _listing(login="", shop=""):
    return {"hs_name": "求人", "kyuujin_status": "公開中", "id_shop_hrhakkaa": shop,
            "airwork_account_login_id": login, "url_hrhakkaa": "", "url_airwork": ""}


@pytest.fixture
def rows(monkeypatch):
    hc._reset_caches()
    mails = {"known@example.com": "kanri+aaa@example.com",
             "Case@Example.com": "kanri+bbb@example.com",
             "plainid01": "kanri+ccc@example.com"}
    monkeypatch.setattr(hc, "_company_hint", lambda: {"idx": {}, "ok": True, "hr": {}})
    monkeypatch.setattr(hc, "_manage_mail_hint", lambda: {
        "exact": mails, "lower": {k.lower(): v for k, v in mails.items()}, "ok": True})
    monkeypatch.setattr(hc, "_owner_names", lambda: {})
    props = {"L1": _listing(login="known@example.com"),
             "L2": _listing(login="unknown@example.com"),
             "L3": _listing(login="case@example.com"),       # 大小文字違い
             "L4": _listing(login="plainid01"),              # メール形式でないID
             "L5": _listing(login="kn-other@example.com"),   # 伏字が L1 と衝突する
             "L6": _listing(shop="1234567")}
    apps = [f"a{i}" for i in range(1, 7)]
    monkeypatch.setattr(hc, "search_all", lambda *a, **k: [{"id": x} for x in apps])

    def fake_assoc(frm, to, ids):
        if (frm, to) == ("0-421", "0-420"):
            return {a: [f"L{a[1:]}"] for a in ids}
        return {}
    monkeypatch.setattr(hc, "_assoc", fake_assoc)
    monkeypatch.setattr(hc, "_batch_props",
                        lambda obj, ids, p: {k: v for k, v in props.items() if k in set(ids)})
    yield hc.collect_unlinked_customers()["rows"]
    hc._reset_caches()


def _as_csv(items) -> str:
    cols = []
    for it in items:
        for k in it:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", restval="")
    w.writeheader()
    w.writerows(items)
    return buf.getvalue()


def test_要対応リストにログインIDとメールを残さない(rows):
    text = _as_csv(rows)
    for raw in ("known@example.com", "unknown@example.com", "case@example.com",
                "Case@Example.com", "plainid01", "kanri+aaa@example.com",
                "kanri+bbb@example.com", "kanri+ccc@example.com"):
        assert raw not in text, f"生の値が残っている: {raw[:2]}…"
    assert not EMAIL.search(text), "メール形式の値が1セルも残らない"


def test_HR店舗IDは伏せない_現場が入れる値そのもの(rows):
    hr = [r for r in rows if r["媒体"] == "HRハッカー"]
    assert hr and hr[0]["入れる鍵"] == "1234567"
    assert "1234567" in hr[0]["入れる場所"]


def test_伏せた行でも現場が特定できる(rows):
    aw = [r for r in rows if r["媒体"] == "AirWork"]
    assert len(aw) == 5
    for r in aw:
        assert r["HubSpot求人リンク"].startswith("https://app.hubspot.com/")
        assert "HubSpot求人リンク" in r["入れる場所"] and "顧客管理シート" in r["入れる場所"]


def test_差分キーは伏字で衝突しない(rows):
    """伏字は先頭2文字なので「kn…」が2行ある。差分キーまで伏字にすると
    別の顧客を同じ行とみなし、新規/解消の数が狂う。"""
    masked = [r["入れる鍵"] for r in rows if r["入れる鍵"] == "kn…"]
    assert len(masked) == 2
    keys = hc._item_keys({"item_key": "鍵の照合番号", "items": rows})
    assert len(keys) == len(rows)
    assert not any(EMAIL.search(k) for k in keys)
