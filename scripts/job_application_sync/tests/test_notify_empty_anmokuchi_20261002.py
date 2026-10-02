# -*- coding: utf-8 -*-
"""一次対応の8項目が空のまま進んだ取引の通知 (2026-10-02)。

## 守る性質

1. 対象 = 生きている取引 × 要否=必要 × 求人出稿完了以降 × 8項目が空
2. テンプレートのままの足切り条件・応募報告先は「空」
3. 求人出稿完了より前・終わった取引・要否が必要でない取引は対象外
4. 求人出稿完了を通らずに先へ進んだ取引も対象 (並び順で判定する)
5. 送り先は環境変数だけ。未設定なら送らない。既定は dry-run
6. 無効化ユーザーの名前は archived=true で引く (404がHTMLでも落ちない)
"""
from __future__ import annotations

from scripts.job_application_sync import deal_master as DM
from scripts.job_application_sync import notify_empty_anmokuchi as N

ORDER = {"52016153": 0, "52016154": 1, "52016155": 2, "52016156": 6,
         "66848546": 28, "90598807": 25}
TPL = {"ashigirijouken": DM._TEMPLATES["ashigirijouken"],
       "oubohoukokusaki": DM._TEMPLATES["oubohoukokusaki"]}


def deal(i, stage="52016156", flag="true", entered="2026-09-15", **kw):
    p = {"dealname": f"取引{i}", "dealstage": stage, "itijitaiou": flag,
         "hubspot_owner_id": "1", f"hs_v2_date_entered_{N.STAGE_PUBLISHED}": entered}
    p.update(TPL)
    p.update(kw)
    return {"id": str(i), "properties": p}


def test_空で求人出稿完了以降の取引だけ():
    ds = [deal(1),                                   # 対象
          deal(2, stage="52016155"),                 # 求人出稿完了そのもの → 対象
          deal(3, stage="52016154"),                 # 出稿前 → 対象外
          deal(4, flag="false"),                     # 要否が必要でない
          deal(5, stage="66848546"),                 # 継続済 (終わった)
          deal(6, stage="90598807"),                 # 解約済
          deal(7, keikenumukakunin="フォーク経験"),  # 中身あり
          deal(8, entered="")]                       # 出稿完了を通らずに進行 → 対象
    got = {d["id"] for d in N.pick_targets(ds, ORDER)}
    assert got == {"1", "2", "8"}


def test_テンプレートのままは空とみなす():
    assert [d["id"] for d in N.pick_targets([deal(1)], ORDER)] == ["1"]
    filled = deal(2, ashigirijouken=TPL["ashigirijouken"].replace("年齢:", "年齢: 55歳まで"))
    assert N.pick_targets([filled], ORDER) == []


def test_求人出稿完了がパイプラインに無ければ止める():
    import pytest
    with pytest.raises(RuntimeError):
        N.pick_targets([deal(1)], {"52016156": 6})


def test_担当者ごとに件数の多い順():
    ds = [deal(1, hubspot_owner_id="a"), deal(2, hubspot_owner_id="b"),
          deal(3, hubspot_owner_id="b")]
    g = N.group_by_owner(ds, {"a": "Aさん", "b": "Bさん"})
    assert list(g) == ["Bさん", "Aさん"]


def test_メッセージ_メンションは環境変数のIDだけ():
    g = {"Bさん": [deal(1)]}
    msg = N.build_message(g, ["U111", "U222"], "x.csv")
    assert msg.startswith("<@U111> <@U222>")
    assert "1件" in msg and "x.csv" in msg
    assert "<@" not in N.build_message(g, [], "x.csv")


def test_通らずに進んだ取引はメッセージで分かる():
    msg = N.build_message({"B": [deal(1, entered="")]}, [], "x.csv")
    assert "求人出稿完了を通らずに進行" in msg


def test_1担当者の件数が多ければ残りはCSVへ():
    ds = [deal(i) for i in range(N.MAX_LINKS_PER_OWNER + 3)]
    msg = N.build_message({"B": ds}, [], "x.csv")
    assert "ほか3件" in msg


class _Resp:
    def __init__(self, code, ctype, body=None):
        self.status_code, self.headers, self._b = code, {"content-type": ctype}, body

    def json(self):
        return self._b


def test_無効化ユーザーはarchivedで名前を引く(monkeypatch):
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        if not params:
            return _Resp(404, "text/html")           # ★HTMLで返る404
        return _Resp(200, "application/json", {"lastName": "山田", "firstName": "太郎"})

    monkeypatch.setattr(N.requests, "get", fake_get)
    assert N.owner_name("9", {}) == "山田 太郎（無効）"
    assert calls == [None, {"archived": "true"}]


def _run(monkeypatch, tmp_path, env, argv):
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "x")
    for k in (N.ENV_WEBHOOK, N.ENV_MENTIONS):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(N, "stage_order", lambda: ORDER)
    monkeypatch.setattr(N, "search_all_by_id", lambda *a, **k: [deal(1)])
    monkeypatch.setattr(N, "owner_name", lambda oid, c: "Bさん")
    monkeypatch.setattr(N, "OUT_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(N, "send_slack", lambda t, w: sent.append((t, w)) or True)
    rc = N.main(argv)
    return rc, sent


def test_既定はdryrunで送らない(monkeypatch, tmp_path):
    rc, sent = _run(monkeypatch, tmp_path, {N.ENV_WEBHOOK: "https://hooks.example/x"}, [])
    assert rc == 0 and sent == []
    assert list(tmp_path.glob("要対応_*.csv")), "dry-runでもCSVは残す"


def test_送り先が未設定なら送らない(monkeypatch, tmp_path):
    rc, sent = _run(monkeypatch, tmp_path, {}, ["--send"])
    assert rc == 0 and sent == []


def test_sendと送り先があれば送る(monkeypatch, tmp_path):
    rc, sent = _run(monkeypatch, tmp_path,
                    {N.ENV_WEBHOOK: "https://hooks.example/x", N.ENV_MENTIONS: "U1"},
                    ["--send"])
    assert rc == 0 and len(sent) == 1
    assert sent[0][1] == "https://hooks.example/x" and "<@U1>" in sent[0][0]
