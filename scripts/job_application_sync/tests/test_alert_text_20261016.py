"""通知の文面で原因が分かること (2026-10-16)。外部には一切アクセスしない。"""
from __future__ import annotations

from scripts.job_application_sync import alert_text as A
from scripts.job_application_sync import applicant_queue as aq
from scripts.job_application_sync import applicant_sync as S


def test_出どころはActionsかPCか(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert A.source_tag() == "[PC]"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert A.source_tag() == "[Actions]"


def test_IDかPWの誤りを言い当てる(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    e = ("ka…: RuntimeError: ログイン後 dashboards に遷移せず (login_id=ka…, "
         "last_url=https://connect.airregi.jp/login?login_failed=true&client_id=AWR")
    cause, action = A.classify_aw_error(e)
    assert "ID か PW が違います" in cause and "顧客管理シート" in action


def test_403はIDやPWの問題ではないと書く(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cause, _ = A.classify_aw_error("AirWork が403を返したため 10:00 までアクセスを止めています")
    assert "403" in cause and "ID や PW の問題ではありません" in cause
    cause, _ = A.classify_aw_error("「応募者一覧をダウンロード」ボタン未検出", blocked_403=True)
    assert "403" in cause


def test_Actionsの未検出はアクセスできないと書く(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cause, action = A.classify_aw_error("AirRegi SSO のID/PWフィールド未検出 (login_id=in…)")
    assert "GitHub Actions からは AirWork にアクセスできません" in cause and "専用PC" in action


def test_PCの未検出は画面の確認を促す(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cause, action = A.classify_aw_error("「応募者一覧をダウンロード」ボタン未検出 (login_id=ca…)")
    assert "ボタンがありません" in cause and "debug_" in action


def test_失敗の通知に原因と全文が入る(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    m = A.aw_failure_message("A社", "ERR_NAME_NOT_RESOLVED at https://x", 3)
    assert m.startswith("⚠️ [PC] 応募登録失敗: A社") and "通信の失敗" in m and "理由の全文" in m


def _resolver(rows):
    r = aq.AccountResolver()
    r.cols = {"alias": 0, "bid": 1, "aid": 2, "reclog": 3, "comp": 4, "bpw": 5, "closed": 6}
    for row in rows:
        for k in ("alias", "bid", "aid", "reclog"):
            v = row[r.cols[k]]
            if v:
                r._add_mail(aq._norm(v), row)
        k = aq._norm_company(row[4])
        if k:
            r.idx_comp_norm.setdefault(k, []).append(row)
    return r


def _item(company, mail):
    return aq.QueueItem(row_id="r", media_type="Airワーク", login_id=mail, company=company,
                        login_ids=[mail])


def test_特定不可の理由_シートに無い():
    r = _resolver([["x@e.com", "", "", "", "別会社", "", ""]])
    why = r.explain(_item("株式会社テスト", "t@e.com"))
    assert "会社名「株式会社テスト」もメール" in why and "ありません" in why


def test_特定不可の理由_事業所を決められない():
    r = _resolver([["", "b1", "", "", "株式会社テスト 東工場", "p", ""],
                   ["", "b2", "", "", "株式会社テスト 西工場", "q", ""]])
    r.idx_comp_norm[aq._norm_company("株式会社テスト")] = r.idx_comp_norm.get(
        aq._norm_company("株式会社テスト 東工場"), []) + r.idx_comp_norm.get(
        aq._norm_company("株式会社テスト 西工場"), [])
    why = r.explain(_item("株式会社テスト", "t@e.com"))
    assert "2行" in why and "事業所を決められません" in why


def test_特定不可の理由_メールは共用で絞れない():
    r = _resolver([["t@e.com", "", "", "", "A社", "", ""], ["t@e.com", "", "", "", "B社", "", ""]])
    why = r.explain(_item("C社", "t@e.com"))
    assert "シートの2行にあります" in why and "1社に絞れません" in why


def test_Slackの先頭に出どころを付け_200以外を記録する(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("SLACK_APPLICANT_ALERT_WEBHOOK", "https://example.invalid/hook")
    sent, logged = [], []

    class R:
        status_code = 404

    monkeypatch.setattr(S.requests, "post", lambda url, json=None, timeout=None: sent.append(json["text"]) or R())
    monkeypatch.setattr(S.plog, "detail", lambda ev, **k: logged.append((ev, k.get("reason"))))
    assert S.slack_notify("⚠️ HR応募登録失敗: x") is False
    assert sent == ["⚠️ [PC] HR応募登録失敗: x"]
    assert ("slack_unsent", "HTTP 404") in logged


def test_HRの失敗は原因と全文を書く(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    m = A.hr_failure_message("HR: 取込エラー 5/5行", 3)
    assert m.startswith("⚠️ [PC] HR応募登録失敗") and "HubSpot への登録が全行で失敗" in m
    assert "理由の全文: HR: 取込エラー 5/5行" in m
    assert "D 列" in A.classify_hr_error("HR項目に有効な日付なし")[0]


def test_同じ失敗の報告は1日1回(tmp_path):
    led = S.Ledger(path=tmp_path / "ledger.json")
    assert S._first_report_today(led, "aw_fail_notified", "k1") is True
    assert S._first_report_today(led, "aw_fail_notified", "k1") is False
    assert S._first_report_today(led, "aw_fail_notified", "k2") is True
    led.set_meta("aw_fail_notified", "k1", day="2000-01-01")  # 前日以前なら また送る
    assert S._first_report_today(led, "aw_fail_notified", "k1") is True
