# -*- coding: utf-8 -*-
"""求人の転記先シートを今の契約の1つに決める (2026-10-02)。

## なぜ要るか

求人を同じ取引先コードの取引すべてに紐付ける方針 (2026-10-01 定例MTG) に
すると、旧契約と新契約でシートURLが違う求人は両方のシートから辿られ、
**同じ応募が旧シートと新シートの両方に転記される** (同日実測: 4コード)。

## 守る性質

1. 転記先 = シートURLを持つ取引のうち latest_live (生きている取引を優先)
2. 今の契約のシートが別なら、旧シートからは外す
3. 取引側にURLが無い求人は残す (移行期の求人票側の経路)
4. 生きている取引にURLが無く、終わった取引にだけURLがあるならそのシート
"""
from __future__ import annotations

from scripts.job_application_sync import customer_sheet_sync as C

OLD = "1OLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDO"
NEW = "1NEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWN"
LIVE, ENDED = "52016156", "66848546"


def url(s):
    return f"https://docs.google.com/spreadsheets/d/{s}/edit"


def test_旧契約と新契約でシートが違えば新しい方だけ():
    deals = {"old": {"dealstage": ENDED, "contract_start_date": "2025-10-01",
                     "customer_sheet_url": url(OLD)},
             "new": {"dealstage": LIVE, "contract_start_date": "2026-04-01",
                     "customer_sheet_url": url(NEW)}}
    l2d = {"L1": ["old", "new"]}
    assert C.filter_listings_for_sheet(NEW, ["L1"], l2d, deals) == {"L1"}
    assert C.filter_listings_for_sheet(OLD, ["L1"], l2d, deals) == set()


def test_取引側にURLが無い求人は移行期の経路として残す():
    deals = {"d": {"dealstage": LIVE, "customer_sheet_url": ""}}
    assert C.filter_listings_for_sheet(OLD, ["L1"], {"L1": ["d"]}, deals) == {"L1"}
    assert C.filter_listings_for_sheet(OLD, ["L2"], {}, deals) == {"L2"}


def test_生きた取引にURLが無ければ終わった取引のシート():
    deals = {"old": {"dealstage": ENDED, "customer_sheet_url": url(OLD)},
             "new": {"dealstage": LIVE, "customer_sheet_url": ""}}
    assert C.owner_sheet_url(["old", "new"], deals) == url(OLD)


def test_同じシートを共有する取引群なら外さない():
    deals = {"a": {"dealstage": ENDED, "customer_sheet_url": url(NEW)},
             "b": {"dealstage": LIVE, "customer_sheet_url": url(NEW)}}
    assert C.filter_listings_for_sheet(NEW, ["L1"], {"L1": ["a", "b"]}, deals) == {"L1"}


def test_本体でも旧シートには転記しない(monkeypatch):
    deals = {"old": {"dealstage": ENDED, "contract_start_date": "2025-10-01",
                     "customer_sheet_url": url(OLD)},
             "new": {"dealstage": LIVE, "contract_start_date": "2026-04-01",
                     "customer_sheet_url": url(NEW)}}

    def fake(u, body, **_k):
        if u.endswith("/search"):
            obj = u.split("/objects/")[1].split("/")[0]
            return {"results": [{"id": "old"}] if obj == "0-3" else []}
        if "associations/0-3/0-420" in u:
            return {"results": [{"from": {"id": "old"}, "to": [{"toObjectId": "L1"}]}]}
        if "associations/0-420/0-3" in u:
            return {"results": [{"from": {"id": "L1"},
                                 "to": [{"toObjectId": "old"}, {"toObjectId": "new"}]}]}
        if u.endswith("/0-3/batch/read"):
            return {"results": [{"id": x["id"], "properties": deals[x["id"]]}
                                for x in body["inputs"]]}
        if "associations/0-420/0-421" in u:
            raise AssertionError("旧シートなのに応募を取りに行った")
        raise AssertionError(u)

    monkeypatch.setattr(C, "_post_hs", fake)
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    assert C.fetch_applicants(OLD, "2026-09-01T00:00:00Z") == []
