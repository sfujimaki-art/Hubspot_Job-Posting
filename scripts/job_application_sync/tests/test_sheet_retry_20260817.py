"""Google Sheets の一時障害に対する再試行の回帰テスト (2026-08-17)。

## なぜ作ったか

**本番が落ちていた。** 応募者同期(Applicant Sync)は5分毎に走るが、
シートを読む呼び出しに再試行が無く、Googleが 503 を返すとその回の
応募取込が丸ごと落ちていた。

実測 (GitHub Actions の失敗を `gh api ".../actions/runs?status=failure"` で列挙):

| 日付 | 失敗回数 |
|---|---|
| 2026-08-16 | 15 |
| 2026-08-06 | 28 |
| 2026-08-05 | 10 |
| 7/08以降 | ほぼ毎日 1〜3 |

合計99件。うち86件が Applicant Sync、13件が Job Daily。ログはいずれも:

    vals = ws.get(_S1_READ_RANGE)
    gspread.exceptions.APIError: [503]: The service is currently unavailable.

クライアント**取得**側 (`applicant_queue._sheets_client`) には既に再試行が
あったのに、**データを読む**呼び出しには無かった、という取りこぼしだった。

## このテストが守る性質

1. 5xx / 429 / 通信断は再試行する（一時障害は待てば直る）
2. 403 / 404 は**再試行しない**（権限やIDの設定ミス。何度やっても直らないし、
   再試行すると原因が見えなくなる）
3. リトライを尽くしたら**必ず落ちる**。空リストを返して「応募0件でした」と
   無言で成功扱いにしてはいけない
4. 待ち時間は指数で伸びる（即時連打でGoogle側をさらに叩かない）
5. 「タブが無い」(WorksheetNotFound) は5xxではないので即座に上げる
   → health_check が一時障害を「タブが無い」と誤認して重複タブを作らない
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync.fetchers import account_loader as al


class _Resp:
    """gspread.APIError が読む最小限のレスポンス模造."""

    def __init__(self, code: int):
        self.status_code = code
        self.text = f"error {code}"

    def json(self):
        return {"error": {"code": self.status_code, "message": "x",
                          "status": "ERR"}}


def _api_error(code: int) -> al.gspread.exceptions.APIError:
    return al.gspread.exceptions.APIError(_Resp(code))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """待ち時間を潰す。待つこと自体は別テストで検証する."""
    monkeypatch.setattr(al.time, "sleep", lambda s: None)


# --------------------------------------------------------------------------
# 一時障害は再試行して回復する
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
def test_一時障害は再試行して回復する(code):
    """本番で出ていたのは503。500系と429は同じ扱いにする."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _api_error(code)
        return [["ok"]]

    assert al.sheet_retry(flaky) == [["ok"]]
    assert calls["n"] == 3, "成功するまで呼び直す"


def test_通信断も再試行する():
    """DNS解決失敗やコネクション切れ。5分毎の実行では普通に起きる."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("getaddrinfo failed")
        return "ok"

    assert al.sheet_retry(flaky) == "ok"


def test_初回で成功すれば1回しか呼ばない():
    """正常時に余計なAPIを叩かない（Sheetsにはレート上限がある）."""
    calls = {"n": 0}

    def fine():
        calls["n"] += 1
        return "ok"

    al.sheet_retry(fine)
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# 設定ミスは再試行しない（原因を隠さない）
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code", [400, 403, 404])
def test_設定ミスは即座に上げる(code):
    """権限不足・シートID誤りは待っても直らない。
    再試行すると本当の原因(共有設定を忘れた等)が5回分の待ちに埋もれる。"""
    calls = {"n": 0}

    def denied():
        calls["n"] += 1
        raise _api_error(code)

    with pytest.raises(al.gspread.exceptions.APIError):
        al.sheet_retry(denied)
    assert calls["n"] == 1, "1回で諦める"


def test_タブが無いエラーは再試行しない():
    """WorksheetNotFound は5xxではない。
    ★health_check はこれを捕まえてタブを新規作成する。ここで再試行や
      握り潰しをすると、一時障害を「タブが無い」と誤認して重複タブを作る。"""
    calls = {"n": 0}

    def missing():
        calls["n"] += 1
        raise al.gspread.WorksheetNotFound("要対応")

    with pytest.raises(al.gspread.WorksheetNotFound):
        al.sheet_retry(missing)
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# 尽きたら必ず落ちる（無言で成功にしない）
# --------------------------------------------------------------------------
def test_リトライ上限で例外を上げる():
    """★ここが最重要。空を返すと「新着応募0件」として正常終了し、
    その回の応募が無言で消える。落ちれば Actions が失敗しメールが飛ぶ。"""
    def dead():
        raise _api_error(503)

    with pytest.raises(RuntimeError, match="リトライ上限"):
        al.sheet_retry(dead, retries=2)


def test_試行回数はretries回の再試行を意味する():
    """retries=2 なら 初回 + 再試行2回 = 3回呼ぶ."""
    calls = {"n": 0}

    def dead():
        calls["n"] += 1
        raise _api_error(503)

    with pytest.raises(RuntimeError):
        al.sheet_retry(dead, retries=2)
    assert calls["n"] == 3


def test_最後の例外の内容が残る():
    """何で落ちたか分からないと調査できない."""
    def dead():
        raise _api_error(503)

    with pytest.raises(RuntimeError) as ei:
        al.sheet_retry(dead, retries=1)
    assert "APIError" in str(ei.value) or "503" in str(ei.value)


# --------------------------------------------------------------------------
# 待ち方
# --------------------------------------------------------------------------
def test_待ち時間が指数で伸びる(monkeypatch):
    """即時連打はGoogle側の障害中にさらに負荷をかける."""
    waits: list[float] = []
    monkeypatch.setattr(al.time, "sleep", waits.append)

    def dead():
        raise _api_error(503)

    with pytest.raises(RuntimeError):
        al.sheet_retry(dead, retries=4)
    assert waits == sorted(waits) and waits[0] < waits[-1], f"単調増加でない: {waits}"


def test_待ち時間に上限がある(monkeypatch):
    """5分毎の実行なので、1回の試行が次の実行を追い越すほど待ってはいけない."""
    waits: list[float] = []
    monkeypatch.setattr(al.time, "sleep", waits.append)

    def dead():
        raise _api_error(503)

    with pytest.raises(RuntimeError):
        al.sheet_retry(dead, retries=8)
    assert max(waits) <= 30
    assert sum(waits) < 180, f"合計{sum(waits)}秒は長すぎる(5分間隔の実行を圧迫)"


# --------------------------------------------------------------------------
# 引数の受け渡し
# --------------------------------------------------------------------------
def test_引数をそのまま渡す():
    """ws.get(_S1_READ_RANGE) や ws.update(rows, value_input_option=...) の形."""
    got = {}

    def f(a, b, *, kw=None):
        got.update(a=a, b=b, kw=kw)
        return "ok"

    assert al.sheet_retry(f, 1, 2, kw="x") == "ok"
    assert got == {"a": 1, "b": 2, "kw": "x"}


def test_retriesは呼び出し先に渡らない():
    """retries はヘルパ自身の引数。Sheets API に混入させない."""
    def f(**kw):
        assert "retries" not in kw
        return "ok"

    assert al.sheet_retry(f, retries=3) == "ok"


def test_lambdaで接続確立ごと包める():
    """★open_by_key() と worksheet() 自体もAPIを叩く。
    `sheet_retry(gc.open_by_key(id).worksheet(t).get_all_values)` と書くと
    open_by_key/worksheet が再試行の**外**で評価されるため意味がない。
    実装は lambda で全体を包んでいる。その形が動くことを固定する。"""
    calls = {"n": 0}

    class _WS:
        def get_all_values(self):
            return [["a"]]

    class _SH:
        def worksheet(self, t):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _api_error(503)      # 接続確立の途中で落ちる
            return _WS()

    sh = _SH()
    assert al.sheet_retry(lambda: sh.worksheet("t").get_all_values()) == [["a"]]
    assert calls["n"] == 2, "接続確立からやり直せている"


# --------------------------------------------------------------------------
# 呼び出し側に漏れが無いこと（実ソースを走査する）
# --------------------------------------------------------------------------
def test_シートを触る箇所に再試行漏れが無い():
    """★個別に包み忘れると、そこだけ本番で落ち続ける。
    新しいシート呼び出しを足したときに気づけるよう、ソースを機械的に見る。"""
    import re
    from pathlib import Path

    root = Path(al.__file__).resolve().parent.parent
    pat = re.compile(r"\b(gc|sh|ws)\.(get_all_values|get|update|batch_update"
                     r"|clear|worksheet|open_by_key|get_worksheet)\(")
    naked = []
    for f in root.rglob("*.py"):
        if "tests" in f.parts:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and "sheet_retry" not in line:
                naked.append(f"{f.name}:{i}: {line.strip()[:60]}")
    assert not naked, "再試行に包まれていないシート呼び出し:\n" + "\n".join(naked)
