"""応募の重複判定（dedup）の回帰テスト (2026-08-08)。

## 何が壊れていたか

`find_existing_appointment` は引数に `media_job_id` を受け取っていたのに
**検索条件に入れておらず**、かつ **メールがあると電話を一切見なかった**。

要件定義のユニークキーは「(媒体, 媒体求人ID, 応募者キー)」なので、実装が
仕様から2つずれていた。結果として2種類の誤りが同時に起きていた。

実測（応募日 2026-07-25 以降 1,230件、同一媒体内の重複 26組 / 余剰30件）:

1. **同じ求人にメール表記違いで2レコード**（14組）
   indeedemail のエイリアス末尾違い `..._x66@` と `..._f54@`、
   あるいは実メール `kenji19640503@gmail.com` と
   エイリアス `kenji1964050395k73_gwv@indeedemail.com`。
   メール優先で電話を見ないので別人扱いになる。

2. **同一run内の重複**（3組）
   HubSpot の search はインデックス反映に数秒かかるため、同じCSVに同じ人が
   2行あると2件目の dedup 検索が1件目を見つけられない。
   実測: 瀬下達夫 が 08:03:20 と 08:03:24 の **4秒差**で2件作成されていた。

一方、**求人IDが違う9組は重複ではない**（同じ人が同じ日に別の求人へ応募）。
電話照合を単純に足すとこれを誤って潰すため、**求人IDで固定したうえで
メール OR 電話**にする必要がある。

## 守る不変条件

- 媒体求人IDが分かるなら必ず検索条件に入れる（別求人への応募を潰さない）
- メールと電話の**どちらか**が一致すれば重複（片方の表記揺れに耐える）
- 同一run内で既に作ったキーは、APIに聞かずに弾く
- 媒体求人IDが空なら従来動作にフォールバック（全体の充足率は85%）
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import applicant_import as ai


class _SpyClient:
    """検索リクエストのbodyを記録するだけのダミー。"""

    def __init__(self, hit: bool = False):
        self.bodies: list = []
        self._hit = hit

    class _Resp:
        status_code = 200

        def __init__(self, hit):
            self._hit = hit

        def json(self):
            return {"results": [{"id": "999"}] if self._hit else []}

    def _post(self, path, body):
        self.bodies.append(body)
        return self._Resp(self._hit)


def _find(client, **kw):
    kw.setdefault("media", "HRハッカー")
    kw.setdefault("media_job_id", "11666281")
    kw.setdefault("phone", "09012345678")
    kw.setdefault("email", "a@example.com")
    kw.setdefault("apply_date", "2026-08-08")
    return ai.RealHubSpotClient.find_existing_appointment(client, **kw)


def _props(group):
    return {f["propertyName"] for f in group["filters"]}


# --------------------------------------------------------------------------
# 1. 媒体求人IDが検索条件に入る
# --------------------------------------------------------------------------
def test_媒体求人IDが検索条件に入る():
    c = _SpyClient()
    _find(c)
    body = c.bodies[0]
    for g in body["filterGroups"]:
        assert "oubokyuujinmemo" in _props(g), "求人IDが条件に無いと別求人を誤dedupする"


def test_媒体求人IDが空なら条件に入れない():
    """充足率85%。空のときに条件へ入れると既存レコードと一致しなくなる。"""
    c = _SpyClient()
    _find(c, media_job_id="")
    for g in c.bodies[0]["filterGroups"]:
        assert "oubokyuujinmemo" not in _props(g)


# --------------------------------------------------------------------------
# 2. メール OR 電話 の2群になる
# --------------------------------------------------------------------------
def test_メールと電話の両方で照合する():
    c = _SpyClient()
    _find(c)
    groups = c.bodies[0]["filterGroups"]
    assert len(groups) == 2, "メール群と電話群の2つになるはず"
    assert {"meeruadoresu"} <= _props(groups[0])
    assert {"denwabangou"} <= _props(groups[1])


def test_メールだけなら1群():
    c = _SpyClient()
    _find(c, phone="")
    groups = c.bodies[0]["filterGroups"]
    assert len(groups) == 1 and "meeruadoresu" in _props(groups[0])


def test_電話だけなら1群():
    c = _SpyClient()
    _find(c, email="")
    groups = c.bodies[0]["filterGroups"]
    assert len(groups) == 1 and "denwabangou" in _props(groups[0])


def test_メールも電話も無ければdedup不能でNone():
    c = _SpyClient()
    assert _find(c, email="", phone="") is None
    assert c.bodies == [], "検索を投げない"


def test_媒体と応募日は常に条件に入る():
    c = _SpyClient()
    _find(c)
    for g in c.bodies[0]["filterGroups"]:
        assert {"oubobaitaimei", "yingmuri"} <= _props(g)


def test_ヒットしたらIDを返す():
    assert _find(_SpyClient(hit=True)) == "999"


# --------------------------------------------------------------------------
# 3. 同一run内の重複を弾く
# --------------------------------------------------------------------------
def _row(name="山田 太郎", email="a@example.com", phone="09012345678",
         job="11666281", date="2026-08-08", media="HRハッカー", lineno=1):
    return ai.ApplicantRow(name=name, kana="", phone=phone, email=email,
                           apply_date=date, media=media, media_job_id=job,
                           raw_lineno=lineno)


def test_同一run内で同じキーは2件目をスキップする(monkeypatch):
    calls = []

    def fake(row, client, default_login_id=""):
        calls.append(row.raw_lineno)
        return ai.ProcessResult(status="linked", applicant_key=str(row.raw_lineno),
                                media=row.media, media_job_id=row.media_job_id)

    monkeypatch.setattr(ai, "process_applicant", fake)
    rows = [_row(lineno=1), _row(lineno=2)]     # 完全に同じ人・同じ求人・同じ日
    res = ai.run_import(rows, client=None)
    assert [r.status for r in res] == ["linked", "skip_duplicate"]
    assert calls == [1], "2件目はHubSpotへ行かない"


def test_同一run内でも求人IDが違えば別扱い(monkeypatch):
    monkeypatch.setattr(ai, "process_applicant",
                        lambda row, c, default_login_id="": ai.ProcessResult(
                            status="linked", applicant_key="x",
                            media=row.media, media_job_id=row.media_job_id))
    res = ai.run_import([_row(job="111", lineno=1), _row(job="222", lineno=2)],
                        client=None)
    assert [r.status for r in res] == ["linked", "linked"]


def test_同一run内でも応募日が違えば別扱い(monkeypatch):
    monkeypatch.setattr(ai, "process_applicant",
                        lambda row, c, default_login_id="": ai.ProcessResult(
                            status="linked", applicant_key="x",
                            media=row.media, media_job_id=row.media_job_id))
    res = ai.run_import([_row(date="2026-08-08", lineno=1),
                         _row(date="2026-08-09", lineno=2)], client=None)
    assert [r.status for r in res] == ["linked", "linked"]


def test_メールが違っても電話が同じなら同一run内で弾く(monkeypatch):
    """indeedemail のエイリアス末尾違いはこれで潰れる。"""
    monkeypatch.setattr(ai, "process_applicant",
                        lambda row, c, default_login_id="": ai.ProcessResult(
                            status="linked", applicant_key="x",
                            media=row.media, media_job_id=row.media_job_id))
    res = ai.run_import([_row(email="x_x66@indeedemail.com", lineno=1),
                         _row(email="x_f54@indeedemail.com", lineno=2)], client=None)
    assert [r.status for r in res] == ["linked", "skip_duplicate"]


def test_メールも電話も無い行はrun内dedupの対象外(monkeypatch):
    """キーが作れないものを勝手に同一視しない（別人を潰さない）。"""
    monkeypatch.setattr(ai, "process_applicant",
                        lambda row, c, default_login_id="": ai.ProcessResult(
                            status="linked", applicant_key="x",
                            media=row.media, media_job_id=row.media_job_id))
    res = ai.run_import([_row(email="", phone="", lineno=1),
                         _row(email="", phone="", lineno=2)], client=None)
    assert [r.status for r in res] == ["linked", "linked"]
