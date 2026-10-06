"""店舗IDの補完 (通知先メール → 取引) を、人の振り分けを崩さない形にする (2026-10-05)。

## 守る不変条件

1. メールが1つの取引先コードだけを指すときだけ足す
2. 足す先はそのコードの生きている今の契約。終わった取引には足さない
3. 別の取引先コードの取引に既に入っている店舗IDは足さない (人の振り分けを崩さない)
4. 既に入っている店舗IDは二重に足さない。外すことはしない
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import backfill_deal_shop_id_via_email as B  # noqa: E402

LIVE = "52016156"
ENDED = "66848546"


def d(stage, code, mails="", shops="", start="2026-04-01"):
    return {"dealstage": stage, "code_of_customer": code, "kanri_mail_address": mails,
            "hrhacker_shop_ids": shops, "contract_start_date": start, "createdate": start,
            "dealname": ""}


def test_メールが1社を指すときだけ生きた今の契約へ足す():
    deals = {"old": d(ENDED, "RL1", mails="rpo+a@x", start="2025-01-01"),
             "new": d(LIVE, "RL1", mails="rpo+a@x", start="2026-04-01")}
    add, stat = B.plan_additions({"rpo+a@x": {"S1"}}, deals)
    assert dict(add) == {"new": {"S1"}}          # 終わった old には足さない


def test_メールが複数の取引先コードを指せば足さない():
    deals = {"a": d(LIVE, "RL1", mails="rpo+g@x"), "b": d(LIVE, "RL2", mails="rpo+g@x")}
    add, stat = B.plan_additions({"rpo+g@x": {"S1"}}, deals)
    assert dict(add) == {}
    assert stat["メールが複数の取引先コードを指す(自動で足さない)"] == 1


def test_別の取引先コードに入っている店舗IDは足さない_人の振り分けを崩さない():
    deals = {"a": d(LIVE, "RL1", mails="rpo+a@x"), "b": d(LIVE, "RL2", shops="S9")}
    add, stat = B.plan_additions({"rpo+a@x": {"S9", "S1"}}, deals)
    assert dict(add) == {"a": {"S1"}}


def test_既に入っている店舗IDは足さない():
    deals = {"a": d(LIVE, "RL1", mails="rpo+a@x", shops="S1")}
    add, _ = B.plan_additions({"rpo+a@x": {"S1"}}, deals)
    assert dict(add) == {}


def test_生きている取引が無いコードには足さない():
    deals = {"a": d(ENDED, "RL1", mails="rpo+a@x")}
    add, stat = B.plan_additions({"rpo+a@x": {"S1"}}, deals)
    assert dict(add) == {} and stat["そのコードに生きている取引が無い"] == 1


def test_取引に無いメールは何もしない():
    add, stat = B.plan_additions({"other@x": {"S1"}}, {"a": d(LIVE, "RL1", mails="rpo+a@x")})
    assert dict(add) == {} and stat["メールが取引に無い"] == 1


# ---- 会社名の表記ゆれ: 統轄/統括 (2026-10-05) ---------------------------------

def test_会社名の統轄と統括を同じとみなす():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
    from scripts.job_application_sync import applicant_queue as AQ
    assert AQ._norm_company("株式会社A 名古屋統轄本部") == AQ._norm_company("株式会社A　名古屋統括本部")
    assert AQ._norm_company("株式会社A 名古屋本部") != AQ._norm_company("株式会社A 岡山本部")


# ---- 公開ログにログインID・パスワードを出さない (2026-10-05) ----------------------

def test_ログインIDとパスワードを伏字にする():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
    from scripts.job_application_sync import applicant_sync as S
    msg = "secretid@example.com: RuntimeError: ログイン失敗 (login_id=secretid@example.com, pw=Pass1234)"
    out = S.scrub_secrets(msg, ["secretid@example.com", "Pass1234"])
    assert "secretid@example.com" not in out and "Pass1234" not in out
    assert "se…" in out and "Pa…" in out
    assert S.scrub_secrets("ok", ["", None, "ab"]) == "ok"      # 短すぎる値・空は触らない


def test_取得処理の結果はログインIDを伏せて返す(monkeypatch):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
    from scripts.job_application_sync import applicant_sync as S
    monkeypatch.setattr(S, "_process_aw_account_raw",
                        lambda *a, **k: {"ok": False, "error": "myloginid: 失敗 PW=topsecret",
                                         "login_id": "myloginid"})
    r = S.process_aw_account("A社", ["myloginid"], "topsecret", None)
    assert "myloginid" not in r["error"] and "topsecret" not in r["error"]
    assert r["login_id"] == "my…"
