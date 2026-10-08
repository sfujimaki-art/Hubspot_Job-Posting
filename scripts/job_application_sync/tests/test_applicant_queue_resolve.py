"""応募者の顧客突合 (AccountResolver) の回帰テスト.

守る挙動は2つ。どちらも 2026-08-07 に実データで見つけた欠陥に対応する。

1. C列の**2つ目以降のメール**でも突合する
   C列は「担当者 <実ドメイン>, 担当者 <リクロジのエイリアス>」の形で、
   どちらが先かは行によって違う。1つ目しか見ていなかったため、
   会社名だけが頼りになり事業所を決められず未突合になっていた。
   実測: 未突合35件 → 13件が新たに突合 (ミホン産業→栃木支店 等)。

2. **共用メールキーでは勝手に1社を選ばない**
   顧客管理シート 2,017行のうち59キーが2社以上で共用されている。
   従来は dict の後勝ちで「シートの後ろの行」が根拠なく勝っていた。
   例 cZq81Kp+AAa11Bb@example.com = 例示WMS 西部PC/中部PC/東部PC。
   会社名で1社に絞れなければ突合しない (未突合として人へ回す)。
   誤配より未突合が安全。うち14キーは解約済と稼働中が混在しており、
   後勝ちで解約済が勝つと稼働中の顧客の応募が恒久SKIPされる。
"""
from __future__ import annotations

import pytest

from scripts.job_application_sync import applicant_queue as aq

from scripts.job_application_sync.applicant_queue import (
    AccountResolver,
    QueueItem,
    _all_emails,
    _norm,
)

# 顧客管理シートの列位置 (テスト用の最小構成)
COLS = {"closed": 0, "reclog": 1, "aid": 2, "bid": 3, "bpw": 4,
        "alias": 5, "comp": 6}


def _row(comp: str, alias: str = "", reclog: str = "", bid: str = "",
         closed: bool = False) -> list:
    r = [""] * 7
    r[COLS["closed"]] = "TRUE" if closed else "FALSE"
    r[COLS["reclog"]] = reclog
    r[COLS["bid"]] = bid
    r[COLS["alias"]] = alias
    r[COLS["comp"]] = comp
    return r


def _resolver(rows: list[list]) -> AccountResolver:
    """build() は Sheets を叩くので、index を直に組んで同じ状態を作る."""
    r = AccountResolver()
    r.cols = dict(COLS)
    for row in rows:
        g = lambda i: row[i] if len(row) > i else ""  # noqa: E731
        for a in [x.strip() for x in g(COLS["alias"]).split(",") if x.strip()]:
            if "@" in a:
                r.idx_alias[_norm(a)] = row
                r._add_mail(_norm(a), row)
        for b in [x.strip() for x in g(COLS["bid"]).split(",") if x.strip()]:
            r.idx_bid[_norm(b)] = row
            r._add_mail(_norm(b), row)
        if g(COLS["reclog"]).strip():
            r.idx_reclog[_norm(g(COLS["reclog"]))] = row
            r._add_mail(_norm(g(COLS["reclog"])), row)
        if g(COLS["comp"]).strip():
            r.idx_comp[_norm(g(COLS["comp"]))] = row
            r.idx_comp_norm.setdefault(_norm(g(COLS["comp"])), []).append(row)
    return r


def _item(company: str, emails: list[str]) -> QueueItem:
    # media は media_type から導出されるプロパティ (AW と判定される文字列を渡す)
    return QueueItem(
        row_id="t1", media_type="Airワーク",
        login_id=emails[0] if emails else "",
        login_ids=list(emails), company=company,
    )


# --------------------------------------------------------------------------
# 1. C列の全メール抽出
# --------------------------------------------------------------------------
def test_all_emails_出現順に全部拾う():
    s = "高橋 <y-sample@example.co.jp>, 高橋 <cZq81Kp+CCc22Dd@example.com>"
    assert _all_emails(s) == [
        "y-sample@example.co.jp", "cZq81Kp+CCc22Dd@example.com"]


def test_all_emails_重複は1つに畳む():
    s = "A <x@y.jp>, B <x@y.jp>"
    assert _all_emails(s) == ["x@y.jp"]


def test_all_emails_山括弧なしでも拾う():
    assert _all_emails("problem@example.co.jp のみ") == [
        "problem@example.co.jp"]


def test_all_emails_空なら空リスト():
    assert _all_emails("") == [] and _all_emails(None) == []


# --------------------------------------------------------------------------
# 2. 2つ目以降のメールで突合できる
# --------------------------------------------------------------------------
def test_2つ目のエイリアスで事業所まで決まる():
    """ミホン産業の実例。1つ目=実ドメイン(シートに無い)、2つ目=エイリアス。

    会社名「ミホン産業株式会社」だけでは栃木支店か決められないが、
    エイリアスが栃木支店の行にしか無いので一意に決まる。
    """
    rows = [_row("ミホン産業株式会社　栃木支店",
                 alias="cZq81Kp+CCc22Dd@example.com")]
    acc = _resolver(rows).resolve(
        _item("ミホン産業株式会社",
              ["y-sample@example.co.jp", "cZq81Kp+CCc22Dd@example.com"]))
    assert acc is not None
    assert acc.company == "ミホン産業株式会社　栃木支店"
    assert "2件目以降" in acc.matched_by


def test_1つ目で引けるなら従来どおり():
    """既存の突合順は動かさない。1つ目で引けたらそれを使う."""
    rows = [_row("A社", alias="first@example.com"),
            _row("B社", alias="second@example.com")]
    acc = _resolver(rows).resolve(
        _item("不一致な会社名", ["first@example.com", "second@example.com"]))
    assert acc is not None and acc.company == "A社"
    assert acc.matched_by == "alias"


def test_どのメールでも引けなければ未突合():
    rows = [_row("A社", alias="other@example.com")]
    assert _resolver(rows).resolve(
        _item("知らない会社", ["x@x.jp", "y@y.jp"])) is None


# --------------------------------------------------------------------------
# 3. 共用キーで勝手に1社を選ばない (誤配防止)
# --------------------------------------------------------------------------
def test_共用キーは会社名で絞れなければ突合しない():
    """例示WMSの実例。同じエイリアスが3事業所にぶら下がっている。

    会社名「株式会社例示WMS」はどの事業所とも一致しないので、
    機械では決められない → None (人へ回す)。
    従来は dict の後勝ちで西部PCが根拠なく選ばれていた。
    """
    shared = "cZq81Kp+AAa11Bb@example.com"
    rows = [_row("株式会社例示WMS東部PC", alias=shared),
            _row("株式会社例示WMS中部PC", alias=shared),
            _row("株式会社例示WMS西部PC", alias=shared)]
    assert _resolver(rows).resolve(
        _item("株式会社例示WMS", [shared])) is None


def test_共用キーでも会社名が完全一致すれば決まる():
    shared = "shared@example.com"
    rows = [_row("株式会社例示WMS東部PC", alias=shared),
            _row("株式会社例示WMS西部PC", alias=shared)]
    acc = _resolver(rows).resolve(_item("株式会社例示WMS西部PC", [shared]))
    assert acc is not None and acc.company == "株式会社例示WMS西部PC"


def test_共用キーで解約済が後勝ちしない():
    """解約済と稼働中が同じキーを共用する14件への対策.

    後勝ちだと稼働中の顧客の応募が「解約済」判定で恒久SKIPされる。
    絞れないなら None を返し、人が判断する。
    """
    shared = "marukyo@example.com"
    rows = [_row("例示丸運輸株式会社 東京営業所", alias=shared, closed=False),
            _row("例示丸運輸株式会社堺センター", alias=shared, closed=True)]
    acc = _resolver(rows).resolve(_item("例示丸運輸株式会社", [shared]))
    assert acc is None, "絞れないのに解約済を掴んではいけない"


def test_単独キーなら従来どおり解約済を解約済と判定する():
    """解約済の判定自体は壊さない (11件は正しく解約済のまま)."""
    rows = [_row("解約した会社", alias="alone@example.com", closed=True)]
    acc = _resolver(rows).resolve(_item("解約した会社", ["alone@example.com"]))
    assert acc is not None and acc.closed is True


def test_共用キーで絞れなくても別のキーで引ければ突合する():
    """reclog が共用でも、b_id が単独なら b_id で決まる (経路を潰さない)."""
    shared = "shared@example.com"
    rows = [_row("X社甲事業所", reclog=shared, bid="bid-x"),
            _row("X社乙事業所", reclog=shared, bid="bid-y")]
    it = _item("X社", [shared, "bid-y"])
    acc = _resolver(rows).resolve(it)
    assert acc is not None and acc.company == "X社乙事業所"


@pytest.mark.parametrize("n_shared", [2, 3, 10])
def test_共用数が何社でも絞れなければNone(n_shared):
    shared = "many@example.com"
    rows = [_row(f"共用{i}社", alias=shared) for i in range(n_shared)]
    assert _resolver(rows).resolve(_item("どれでもない会社", [shared])) is None


# --------------------------------------------------------------------------
# 会社名列の特定 (2026-08-24 本番停止の再発防止)
#
# ★現場がシートのC列へ「企業名」というヘッダを付けたところ、応募取込が
#   5分毎に落ち続けた。列判定が「ヘッダが空の列」しか候補にしていなかったため。
#   ヘッダ名が付くかどうかは現場の都合で変わる。名前でも中身でも引けること。
# --------------------------------------------------------------------------
def _hdr_real():
    """2026-08-24 実測のヘッダ (C列に「企業名」が付いた後)."""
    return ["", "企業番号", "権限共有", "企業名", "HS名", "担当コンサル",
            "engage\nアカウント", "クローズ", "リクロジアドレス", "PW",
            "AirWork ID", "AirワークPW", "Airワーク\n会社情報変更",
            "Airワーク\nアドレス変更", "", "企業AirWork ID", "企業Airwork PW",
            "エイリアス\nアドレス\n", "エイリアス\nパス", "engage ID"]


def _row_real(comp="株式会社サンプル", mail="rpo.sample@example.com"):
    r = [""] * 20
    r[3] = comp
    r[8] = mail
    return r


def test_企業名ヘッダがあれば会社名列を引ける():
    """★これが本番停止の直接原因。ヘッダが付いた瞬間に候補から外れていた."""
    cols = aq._resolve_account_columns(_hdr_real(), [_row_real()])
    assert cols["comp"] == 3


def test_ヘッダが空でも中身で引ける():
    """従来どおりヘッダ無しでも動くこと (現場が名前を消しても壊れない)."""
    h = _hdr_real()
    h[3] = ""
    cols = aq._resolve_account_columns(h, [_row_real() for _ in range(3)])
    assert cols["comp"] == 3


def test_HS名を会社名と取り違えない():
    """HS名はHubSpot側の名前で別物。ヘッダ候補に入れてはいけない."""
    h = _hdr_real()
    h[3] = ""                      # 企業名ヘッダを外す
    rows = []
    for _ in range(5):
        r = _row_real()
        r[4] = "株式会社サンプル(HS)"   # HS名にも社名が入る
        rows.append(r)
    cols = aq._resolve_account_columns(h, rows)
    assert cols["comp"] == 3, "内容が同数なら先に出る方(企業名列)を採る"


def test_会社名列が無ければ明示エラー():
    """位置依存へ黙って落とさない。落ちれば原因がログに出る."""
    h = _hdr_real()
    h[3] = ""
    r = [""] * 20
    r[8] = "rpo.sample@example.com"
    with pytest.raises(RuntimeError, match="列を特定できず"):
        aq._resolve_account_columns(h, [r])


def test_他の用途の列を会社名にしない():
    """リクロジアドレス列等が社名を含んでも、そちらを会社名にしない."""
    h = _hdr_real()
    h[3] = ""
    rows = []
    for _ in range(3):
        r = _row_real()
        r[8] = "株式会社まぎらわしい@example.com"
        rows.append(r)
    cols = aq._resolve_account_columns(h, rows)
    assert cols["comp"] == 3 and cols["reclog"] == 8


# --------------------------------------------------------------------------
# 4. 共用キーでも、候補のログイン認証がすべて同じなら突合する (2026-10-06)
#
# ★取り込みで行から使うのはログイン認証と表示用の会社名だけで、拠点は
#   応募の求人(media_job_id→LISTING)から決まる。認証が同じならどの行を
#   選んでも結果は同じ。例: 拠点3行が同じ別名・同じログイン → 未突合に
#   していたのは取りこぼし。認証が1つでも違えば従来どおり人へ回す。
# --------------------------------------------------------------------------
def _row_login(comp: str, alias: str, bid: str, bpw: str,
               closed: bool = False) -> list:
    r = _row(comp, alias=alias, bid=bid, closed=closed)
    r[COLS["bpw"]] = bpw
    return r


def test_同一認証の3行は先頭行で突合する():
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 丙拠点", shared, "bid-1", "pw-1")]
    acc = _resolver(rows).resolve(_item("サンプル物流", [shared]))
    assert acc is not None
    assert acc.company == "サンプル物流 甲拠点", "シート上で先頭の行"
    assert acc.b_ids == ["bid-1"] and acc.b_pw == "pw-1"
    assert acc.matched_by == "alias(同一認証の複数行)"


def test_同一認証は企業IDの並び順と大小文字の違いを同じとみなす():
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1\nbid-2", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "BID-2, bid-1", "pw-1")]
    acc = _resolver(rows).resolve(_item("サンプル物流", [shared]))
    assert acc is not None and acc.company == "サンプル物流 甲拠点"


@pytest.mark.parametrize("bid, bpw", [("bid-9", "pw-1"), ("bid-1", "pw-9")])
def test_認証が1行だけ違えば突合しない(bid, bpw):
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 丙拠点", shared, bid, bpw)]
    assert _resolver(rows).resolve(_item("サンプル物流", [shared])) is None


def test_解約済の印が行で違えば突合しない():
    """解約済かどうかで SKIP が変わるので、同じ結果にならない."""
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "pw-1",
                       closed=True)]
    assert _resolver(rows).resolve(_item("サンプル物流", [shared])) is None


@pytest.mark.parametrize("bids", [("", ""), ("bid-1", ""), ("", "bid-1")])
def test_企業IDが空の行が混じれば突合しない(bids):
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, bids[0], "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, bids[1], "pw-1")]
    assert _resolver(rows).resolve(_item("サンプル物流", [shared])) is None


def test_PWが空なら同じでも突合しない():
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", ""),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "")]
    assert _resolver(rows).resolve(_item("サンプル物流", [shared])) is None


def test_会社名で1行に絞れるなら従来の行を選ぶ():
    """同一認証でも、会社名が完全一致する行があればそちらが優先."""
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "pw-1")]
    acc = _resolver(rows).resolve(_item("サンプル物流 乙拠点", [shared]))
    assert acc is not None and acc.company == "サンプル物流 乙拠点"
    assert acc.matched_by == "alias"


def test_2件目以降のメールでも同一認証なら突合する():
    shared = "branch-alias@example.com"
    rows = [_row_login("サンプル物流 甲拠点", shared, "bid-1", "pw-1"),
            _row_login("サンプル物流 乙拠点", shared, "bid-1", "pw-1")]
    acc = _resolver(rows).resolve(
        _item("サンプル物流", ["unknown@example.co.jp", shared]))
    assert acc is not None and acc.company == "サンプル物流 甲拠点"
    assert acc.matched_by == "alias(2件目以降)(同一認証の複数行)"


def test_会社名の表記ゆれ一致で複数候補でも同一認証なら突合する():
    rows = [_row_login("サンプル物流", "", "bid-1", "pw-1"),
            _row_login("サンプル物流", "", "bid-1", "pw-1")]
    r = _resolver(rows)
    r.idx_comp.clear()   # 完全一致の経路を外し、表記ゆれ一致だけを試す
    acc = r.resolve(_item("サンプル物流", []))
    assert acc is not None and acc.matched_by == "company_normalized(同一認証の複数行)"


def test_会社名の表記ゆれ一致で複数候補かつ認証違いは触らない():
    rows = [_row_login("サンプル物流", "", "bid-1", "pw-1"),
            _row_login("サンプル物流", "", "bid-2", "pw-1")]
    r = _resolver(rows)
    r.idx_comp.clear()
    assert r.resolve(_item("サンプル物流", [])) is None
