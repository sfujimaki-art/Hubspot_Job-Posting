# -*- coding: utf-8 -*-
"""一次対応の要否を「今の契約」に合わせる (2026-10-02)。

## なぜ直したか

旧実装は、求人に紐付く取引のどれか1件が「必要」なら必要 (true優先) だった。
求人を同じ取引先コードの取引すべてに紐付ける方針 (2026-10-01 定例MTG) では、
旧契約が必要・新契約が不要でも必要のまま残る (同日実測: 33コード)。

## 守る性質

1. 求人に紐付く取引 + 同じ取引先コードの取引の中で、生きている取引の最新の値
2. 旧契約の true は今の契約の false を上書きしない
3. 今の契約の値が空なら触らない (古い値に落ちない)
4. AW経路 (管理用メール) も同じ規則。1つのメールを複数取引が持っていても可
5. 健全性チェックも同じ規則で判定する (誤警報を出さない)
"""
from __future__ import annotations

from scripts.job_application_sync import sync_ichijitaiou as S

LIVE, ENDED = "52016156", "66848546"


def d(stage, start, flag, code="RL1", mail=""):
    return {"dealstage": stage, "contract_start_date": start, "itijitaiou": flag,
            "code_of_customer": code, "kanri_mail_address": mail}


def test_旧契約がtrueでも今の契約がfalseなら不要():
    deals = {"old": d(ENDED, "2025-10-01", "true"), "new": d(LIVE, "2026-04-01", "false")}
    assert S.decide_want(["old", "new"], deals) == "不要"


def test_今の契約が空なら触らない():
    deals = {"old": d(ENDED, "2025-10-01", "true"), "new": d(LIVE, "2026-04-01", None)}
    assert S.decide_want(["old", "new"], deals) is None


def test_生きている取引が無ければ最新の終わった取引():
    deals = {"a": d(ENDED, "2025-01-01", "false"), "b": d(ENDED, "2025-06-01", "true")}
    assert S.decide_want(["a", "b"], deals) == "必要"


def test_同じ取引先コードの取引まで広げる():
    deals = {"old": d(ENDED, "2025-10-01", "true"),
             "new": d(LIVE, "2026-04-01", "false"),
             "other": d(LIVE, "2026-05-01", "true", code="RL2")}
    by_code = {"RL1": ["old", "new"], "RL2": ["other"]}
    got = S.expand_by_code(["old"], deals, by_code)
    assert set(got) == {"old", "new"}
    assert S.decide_want(got, deals) == "不要"


def test_コードの無い取引は広げない():
    deals = {"x": d(LIVE, "2026-01-01", "true", code="")}
    assert S.expand_by_code(["x"], deals, {}) == ["x"]


def test_管理用メールは複数アドレスを分割し全取引を持つ():
    deals = {"a": d(ENDED, "2025-01-01", "true", mail="A@x.jp; b@x.jp"),
             "b": d(LIVE, "2026-01-01", "false", mail="a@x.jp")}
    m = S.build_mail_to_deals(deals)
    assert sorted(m["a@x.jp"]) == ["a", "b"]
    assert m["b@x.jp"] == ["a"]
    assert S.decide_want(m["a@x.jp"], deals) == "不要"


def test_run_旧契約のtrueで必要に戻さない(monkeypatch):
    deals = {"old": d(ENDED, "2025-10-01", "true"), "new": d(LIVE, "2026-04-01", "false")}
    monkeypatch.setattr(S, "list_all", lambda *a, **k: [
        {"id": "L1", "properties": {"ichijitaiounoumu_deforuto": "必要"}}])
    monkeypatch.setattr(S, "_batch_assoc", lambda ids: {"L1": ["old"]})
    monkeypatch.setattr(S, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(S, "_batch_deals", lambda ids: {})
    monkeypatch.setattr(S, "build_login_to_mail", lambda: {})
    sent = []
    monkeypatch.setattr(S, "post_retry", lambda url, body, **k: sent.append(body) or {})
    r = S.run(dry_run=False)
    assert r["to_update"] == 1
    assert sent[0]["inputs"][0]["properties"]["ichijitaiounoumu_deforuto"] == "不要"


def test_run_AW経路も今の契約に合わせる(monkeypatch):
    deals = {"old": d(ENDED, "2025-10-01", "true", mail="m@x.jp"),
             "new": d(LIVE, "2026-04-01", "false", mail="m@x.jp")}
    monkeypatch.setattr(S, "list_all", lambda *a, **k: [
        {"id": "L1", "properties": {"airwork_account_login_id": "acc1"}}])
    monkeypatch.setattr(S, "_batch_assoc", lambda ids: {})
    monkeypatch.setattr(S, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(S, "_batch_deals", lambda ids: {})
    monkeypatch.setattr(S, "build_login_to_mail", lambda: {"acc1": "m@x.jp"})
    r = S.run(dry_run=True)
    assert r["aw_matched"] == 1 and r["to_update"] == 1


def test_健全性チェックも同じ規則で食い違いを数える(monkeypatch):
    from scripts.job_application_sync import health_check as HC
    deals = {"old": d(ENDED, "2025-10-01", "true"), "new": d(LIVE, "2026-04-01", "false")}
    monkeypatch.setattr(HC, "search_all", lambda *a, **k: [
        {"id": "L1", "properties": {"ichijitaiounoumu_deforuto": "不要"}},
        {"id": "L2", "properties": {"ichijitaiounoumu_deforuto": "必要"}}])
    monkeypatch.setattr(HC, "_assoc", lambda f, t, ids: {"L1": ["old"], "L2": ["old"]})
    monkeypatch.setattr(S, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(HC, "_batch_props", lambda *a, **k: {})
    r = HC.check_ichijitaiou_sync()
    # 旧規則 (true優先) なら L1 が食い違い。新規則では L2 だけが食い違い
    assert r["value"] == 1


def test_run_別会社の取引に同時に紐付いた求人は触らない(monkeypatch):
    deals = {"a": dict(d(LIVE, "2026-04-01", "true"), code_of_customer="RL1"),
             "b": dict(d(LIVE, "2026-04-01", "false"), code_of_customer="RL2")}
    monkeypatch.setattr(S, "list_all", lambda *a, **k: [
        {"id": "L1", "properties": {"ichijitaiounoumu_deforuto": ""}}])
    monkeypatch.setattr(S, "_batch_assoc", lambda ids: {"L1": ["a", "b"]})
    monkeypatch.setattr(S, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(S, "_batch_deals", lambda ids: {})
    monkeypatch.setattr(S, "build_login_to_mail", lambda: {})
    r = S.run(dry_run=True)
    assert r["to_update"] == 0 and r["other_company_skipped"] == 1


def test_run_管理用メールを別会社と共有する求人は触らない(monkeypatch):
    deals = {"a": dict(d(LIVE, "2026-04-01", "true", mail="m@x.jp"), code_of_customer="RL1"),
             "b": dict(d(LIVE, "2026-04-01", "false", mail="m@x.jp"), code_of_customer="RL2")}
    monkeypatch.setattr(S, "list_all", lambda *a, **k: [
        {"id": "L1", "properties": {"airwork_account_login_id": "acc1"}}])
    monkeypatch.setattr(S, "_batch_assoc", lambda ids: {})
    monkeypatch.setattr(S, "load_pipeline_deals", lambda: dict(deals))
    monkeypatch.setattr(S, "_batch_deals", lambda ids: {})
    monkeypatch.setattr(S, "build_login_to_mail", lambda: {"acc1": "m@x.jp"})
    r = S.run(dry_run=True)
    assert r["to_update"] == 0 and r["other_company_skipped"] == 1
