"""リクロジ (reclogi.jp) の応募を応募者 (0-421) に取り込む (2026-10-20)。

GAS がタブ「リクロジ応募」に1応募1行で書き、Python は「HubSpot登録」が空の行だけを
applicant_import の流れに通して、最後の2列に結果を書き戻す。
値はすべてダミー (公開リポジトリ)。求人ページの fixture は実ページを縮め、
会社名・住所をダミーに置き換えたもの。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.job_application_sync import applicant_import as ai
from scripts.job_application_sync import applicant_queue as aq
from scripts.job_application_sync import applicant_sync as S
from scripts.job_application_sync import customer_sheet_sync as css
from scripts.job_application_sync import reclogi_applicants as rl

FIXTURE = Path(__file__).parent / "fixtures" / "reclogi_job_page.html"

HEADER = ["messageId", "受信日時", "応募ID", "ユーザーID", "応募日時", "応募企業名",
          "応募求人ID", "応募求人名", "HRハッカー求人ID", "店舗id", "職種id", "掲載求人URL",
          "ユーザー名", "姓", "名", "姓（カナ）", "名（カナ）", "性別", "生年月日", "年齢",
          "電話番号", "メールアドレス", "郵便番号", "都道府県", "市区町村", "町域",
          "番地・建物", "最寄駅", "保有資格", "自己PR", "志望動機", "本人希望記入欄",
          "応募ページURL", "HubSpot登録", "HubSpot応募者ID"]


def _row(**over) -> dict:
    v = {h: "" for h in HEADER}
    v.update({
        "messageId": "msg-0001", "受信日時": "2026-10-14 12:55", "応募ID": "A-0001",
        "応募日時": "2026-10-14 12:54", "応募企業名": "株式会社サンプル運輸",
        "応募求人ID": "10756", "HRハッカー求人ID": "11694710", "店舗id": "1154643",
        "掲載求人URL": "https://reclogi.jp/job-data/10756",
        "姓": "見本", "名": "太郎", "姓（カナ）": "ミホン", "名（カナ）": "タロウ",
        "性別": "男性", "生年月日": "1990-07-27", "年齢": "",
        "電話番号": "08000000000", "メールアドレス": "Taro.Sample@Example.com",
        "郵便番号": "000-0000", "都道府県": "新潟県", "市区町村": "長岡市",
        "町域": "見本町", "番地・建物": "1-2-3 サンプルハイツ101",
        "保有資格": "普通自動車免許", "自己PR": "ダミーの自己PR", "志望動機": "ダミーの志望動機",
        "本人希望記入欄": "ダミーの希望",
    })
    v.update(over)
    return v


def _values(rows: list, header=HEADER) -> list:
    return [list(header)] + [[r.get(h, "") for h in header] for r in rows]


class FakeWs:
    """gspread の worksheet の代わり。

    batch_update は本物 (gspread 6.2.1) と同じく、渡された data の range を
    「'タブ名'!A1」に書き換える (書いた件数を後から range で数えると外れる)。
    """

    def __init__(self, values, title="リクロジ応募"):
        self.values = [list(r) for r in values]
        self.title = title
        self.writes: list = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def row_values(self, n):
        return list(self.values[n - 1]) if n <= len(self.values) else []

    def col_values(self, n):
        out = [r[n - 1] if len(r) >= n else "" for r in self.values]
        while out and not out[-1]:
            out.pop()
        return out

    def get(self, rng):
        """'A2:AA10' の範囲 (行ごとに末尾の空は詰める・本物と同じ)。"""
        a, b = rng.split(":")
        (r1, c1), (r2, c2) = self._rc(a), self._rc(b)
        out = []
        for r in self.values[r1 - 1:r2]:
            row = list(r[c1 - 1:c2])
            while row and not str(row[-1]).strip():
                row.pop()
            out.append(row)
        return out

    def batch_get(self, ranges):
        out = []
        for rng in ranges:
            v = self._get(rng)
            out.append([[v]] if v else [])
        return out

    @staticmethod
    def _rc(rng):
        col = "".join(c for c in rng if c.isalpha())
        row = int("".join(c for c in rng if c.isdigit()))
        ci = 0
        for ch in col:
            ci = ci * 26 + (ord(ch) - 64)
        return row, ci

    def _get(self, rng):
        row, ci = self._rc(rng)
        r = self.values[row - 1] if row <= len(self.values) else []
        return r[ci - 1] if ci <= len(r) else ""

    def batch_update(self, data):
        self.writes.append([dict(d) for d in data])
        for d in data:
            rng = d["range"]
            d["range"] = f"'{self.title}'!{rng}"     # 本物と同じ書き換え
            col = "".join(c for c in rng if c.isalpha())
            row = int("".join(c for c in rng if c.isdigit()))
            ci = 0
            for ch in col:
                ci = ci * 26 + (ord(ch) - 64)
            r = self.values[row - 1]
            while len(r) < ci:
                r.append("")
            r[ci - 1] = d["values"][0][0]

    def cell(self, row: int, name: str) -> str:
        i = self.values[0].index(name)
        r = self.values[row - 1]
        return r[i] if i < len(r) else ""


# ---------------------------------------------------------------- 行 → ApplicantRow
def test_行をApplicantRowに変換する():
    _, recs = rl.records_from_values(_values([_row()]))
    a = rl.to_applicant_row(recs[0])
    assert a.media == "リクロジメディア"
    assert a.media_job_id == "10756"               # リクロジ求人ID
    assert a.name == "見本 太郎" and a.kana == "ミホン タロウ"
    assert a.phone == "08000000000"
    assert a.email == "taro.sample@example.com"
    assert a.apply_date == "2026-10-14"
    assert a.gender == "男" and a.birthdate == "1990-07-27"
    assert a.age == "36"                       # 年齢が空なら生年月日と応募日から計算
    assert (a.postal, a.pref, a.city) == ("000-0000", "新潟県", "長岡市")
    assert a.addr_rest == "見本町 1-2-3 サンプルハイツ101"
    assert a.extra == {"hoyuushikaku": "普通自動車免許", "jikopr": "ダミーの自己PR",
                       "oubodouki": "ダミーの志望動機",
                       "oubosaki_kyuujin_url": "https://reclogi.jp/job-data/10756",
                       "reclogi_oubo_id": "A-0001", "honnin_kibou_kinyuuran": "ダミーの希望"}


def test_列の順番が変わっても列を足されても同じ結果():
    shuffled = list(reversed(HEADER)) + ["現場メモ"]
    _, a = rl.records_from_values(_values([_row()]))
    _, b = rl.records_from_values(_values([dict(_row(), 現場メモ="x")], header=shuffled))
    assert rl.to_applicant_row(a[0]) == rl.to_applicant_row(b[0])


def test_空欄の項目は送らない_年齢が書いてあればそれを使う():
    _, recs = rl.records_from_values(_values([_row(
        性別="", 郵便番号="", 年齢="35", 保有資格="", 生年月日="1990/07/27")]))
    a = rl.to_applicant_row(recs[0])
    assert a.gender == "" and a.postal == "" and a.age == "35"
    assert a.birthdate == "1990-07-27"
    assert "hoyuushikaku" not in a.extra
    props = ai.build_appointment_properties(a, linked=True)
    assert "seibetsu" not in props and "yuubinbangou" not in props


def test_先頭の0が落ちた電話番号は0を戻す():
    _, recs = rl.records_from_values(_values([_row(電話番号="8000000000")]))
    assert rl.to_applicant_row(recs[0]).phone == "08000000000"


@pytest.mark.parametrize("status, expected", [
    ("", True), ("  ", True), ("受入済", False), ("求人未特定", False),
    ("エラー: 何か", False), ("対象外", False)])
def test_処理するのはHubSpot登録が空の行だけ(status, expected):
    _, recs = rl.records_from_values(_values([_row(HubSpot登録=status)]))
    assert rl.is_target(recs[0]) is expected


def test_媒体名の正規化表にリクロジメディアがある():
    assert "リクロジメディア" in ai.OUBO_BAITAI_VALUES
    assert ai.normalize_media("リクロジメディア") == "リクロジメディア"
    assert ai.normalize_media("リクロジ") == "リクロジメディア"


# ---------------------------------------------------------------- 求人ページ
def test_求人ページから求人IDと募集要項を読む():
    p = rl.parse_job_page(FIXTURE.read_text(encoding="utf-8"))
    assert (p["reclogi_id"], p["shop_id"], p["hr_job_id"]) == ("10756", "1154643", "11694710")
    assert p["title"] == "大型ドライバー" and p["occupation"] == "大型ドライバー"
    assert p["description"].startswith("大型トラックを使用し")
    assert p["employment"] == "正社員" and p["salary_type"] == "月給"
    assert p["worktime"].startswith("14:00～17:00")
    assert p["holidays"].splitlines()[0] == "・年間休日：107日"
    assert "\n\n" not in p["holidays"]          # <br /> と改行を二重にしない
    assert (p["pref"], p["city"]) == ("新潟県", "長岡市")


def test_求人IDの欄が無いページは読めない扱い():
    assert rl.parse_job_page("<html><body>この求人の掲載は終了しました</body></html>")["hr_job_id"] == ""


def test_リクロジの求人はid_reclogiで作り_HRの鍵は入れない():
    p = rl.build_listing_props(rl.parse_job_page(FIXTURE.read_text(encoding="utf-8")),
                               "https://reclogi.jp/job-data/10756", "2026-10-20T00:00:00Z")
    assert p["id_reclogi"] == "10756"
    assert p["url_reclogi"] == "https://reclogi.jp/job-data/10756"
    assert p["reclogi_moto_hr_kyuujin_id"] == "11694710"     # 参考用
    assert p["baitai_genjoukyou_reclogi"] == "掲載中"
    for k in ("id_hrhakkaa", "id_shop_hrhakkaa", "url_hrhakkaa",
              "saishuu_csv_kenshutsu_bi", "konkai_csv_kenshutsu_flag",
              "baitai_genjoukyou_hrhakkaa", "shuyoushukkoubaitai"):
        assert k not in p, k            # HR取込・持ち主判定・夜間紐付けに拾わせない
    assert p["kyuujin_status"] == "公開中"
    from scripts.job_application_sync import listing_stage
    assert p["hs_pipeline"] == listing_stage.PIPELINE_ID          # HR の求人と同じ
    assert p["hs_pipeline_stage"] == listing_stage.STAGE_BOSHUU
    assert p["hs_name"] == "大型ドライバー" and p["zhizhong"] == "大型ドライバー"
    assert p["shigotonaiyou"].startswith("大型トラック")
    assert p["qinwude"] == "新潟県長岡市" and p["kinmujikan"].startswith("14:00")
    assert p["todoufuken"] == "新潟県" and p["shikuchouson"] == "長岡市"
    assert p["guyongxingtai"] == "full_time" and p["kyuuyokeitai"] == "月給"
    assert p["hissushikaku_shousai"].startswith("【必須条件】")
    assert "nianshouxiaxian" not in p                   # 月給から年収は計算しない
    assert p["kanri_kubun"] == "弊社管理" and p["torikomi_riyuu"] == "リクロジ応募から作成"
    assert p["douki_moto_filename"] == "https://reclogi.jp/job-data/10756"
    assert p["doukisaishuujikoku"] == "2026-10-20T00:00:00Z"


def test_年収は単位が年のときだけ入れる_知らない雇用形態は書かない():
    parsed = dict(rl.parse_job_page(FIXTURE.read_text(encoding="utf-8")),
                  salary_unit="YEAR", salary_min="3000000", salary_max="4000000",
                  employment="嘱託", salary_type="出来高")
    p = rl.build_listing_props(parsed, "https://reclogi.jp/job-data/10756")
    assert (p["nianshouxiaxian"], p["nianshoushangxian"]) == ("3000000", "4000000")
    assert "guyongxingtai" not in p and "kyuuyokeitai" not in p


def test_主要出稿媒体は定数が決まるまで書かない(monkeypatch):
    parsed = rl.parse_job_page(FIXTURE.read_text(encoding="utf-8"))
    assert "shuyoushukkoubaitai" not in rl.build_listing_props(parsed, "u")
    monkeypatch.setattr(rl, "RECLOGI_SHUYOU_BAITAI", "リクロジ")
    assert rl.build_listing_props(parsed, "u")["shuyoushukkoubaitai"] == "リクロジ"


def test_取得するのはリクロジの求人URLだけ(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("取りに行かない"))
    assert rl.fetch_job_page("https://example.com/job-data/1") is None
    assert rl.fetch_job_page("") is None


# ---------------------------------------------------------------- run (シート+HubSpot)
class FakeClient(ai.DryRunClient):
    """DryRunClient + リクロジの求人まわりの読み書き (記録だけ)。"""

    def __init__(self, listings_reclogi=None, listing_deals=None, listing_props=None,
                 shop_deals=None, code_deals=None, inactive=(), **kw):
        super().__init__(**kw)
        self.known_props = {"hoyuushikaku", "jikopr", "oubodouki", "oubosaki_kyuujin_url",
                            "reclogi_oubo_id", "reclogi_oubo_page_url",
                            "honnin_kibou_kinyuuran", "moyori_eki"}
        self.listings_reclogi = dict(listings_reclogi or {})
        self.listing_deals = listing_deals or {}
        self.listing_props = listing_props or {}
        self.shop_deals = shop_deals or {}
        self.code_deals = code_deals or {}
        self.inactive = set(inactive)
        self.created_listings: list = []
        self.deal_links: list = []

    def read_listing(self, listing_id, properties):
        return self.listing_props.get(listing_id, {})

    def get_listing_deals(self, listing_id):
        return list(self.listing_deals.get(listing_id, []))

    def deals_with_shop(self, shop_id):
        return dict(self.shop_deals.get(shop_id, {}))

    def deals_by_code(self, code):
        return dict(self.code_deals.get(code, {}))

    def owner_is_active(self, owner_id):
        return owner_id not in self.inactive

    def create_listing(self, properties):
        self.created_listings.append(properties)
        return f"L-NEW{len(self.created_listings)}"

    def associate_listing_deals(self, listing_id, deal_ids):
        self.deal_links.append((listing_id, list(deal_ids)))
        return len(deal_ids)


def _run(ws, client, ledger=None, dry_run=False, page=None, made=None, **kw):
    made = made if made is not None else []

    def make_listing(client, props):
        made.append(props)
        return "L-NEW"
    return rl.run(dry_run=dry_run, ledger=ledger, ws=ws, client=client,
                  fetch_page=lambda url: page, make_listing=make_listing, **kw)


def test_求人があれば紐付けて受入済と応募者IDを書く(tmp_path):
    ws = FakeWs(_values([_row(), _row(messageId="msg-0002", HubSpot登録="対象外")]))
    cli = FakeClient(listings_reclogi={"10756": "L-1"})
    s = _run(ws, cli, ledger=S.Ledger(tmp_path / "l.json"))
    assert s["targets"] == 1 and s["linked"] == 1
    assert s["written"] == 1      # gspread が range を書き換えても件数を数え損ねない
    assert len(cli.created_appts) == 1
    props = cli.created_appts[0]["properties"]
    assert props["oubobaitaimei"] == "リクロジメディア"
    assert props["oubokyuujinmemo"] == "10756"         # リクロジ求人ID
    assert props["yingmujinglu"] == ai.RECLOGI_YINGMUJINGLU
    assert props["reclogi_oubo_id"] == "A-0001"
    assert props["oubosaki_kyuujin_url"] == "https://reclogi.jp/job-data/10756"
    assert props["jikopr"] == "ダミーの自己PR"
    assert cli.associations == [{"appointment_id": cli.created_appts[0]["id"],
                                 "listing_id": "L-1", "type_id": 5}]
    assert ws.cell(2, "HubSpot登録") == "受入済"
    assert ws.cell(2, "HubSpot応募者ID") == cli.created_appts[0]["id"]
    assert ws.cell(3, "HubSpot登録") == "対象外"   # 現場が書いた行は触らない


def test_求人が無ければページから作って紐付ける(tmp_path):
    ws = FakeWs(_values([_row()]))
    cli = FakeClient()
    made: list = []
    s = _run(ws, cli, ledger=S.Ledger(tmp_path / "l.json"),
             page=FIXTURE.read_text(encoding="utf-8"), made=made)
    assert s["listing_created"] == 1 and s["linked"] == 1
    assert made[0]["id_reclogi"] == "10756" and "id_hrhakkaa" not in made[0]
    # 作った直後は検索に出ないので、作ったIDをそのまま使う
    assert cli.associations[0]["listing_id"] == "L-NEW"
    assert ws.cell(2, "HubSpot登録") == "受入済"


def test_掲載終了でHR求人も無ければ求人未特定で応募者だけ作る(tmp_path):
    ws = FakeWs(_values([_row()]))
    cli = FakeClient()
    made: list = []
    s = _run(ws, cli, ledger=S.Ledger(tmp_path / "l.json"), page=rl.PAGE_GONE, made=made)
    assert made == [] and s["page_unavailable"] == 1 and s["unlinked"] == 1
    assert cli.created_appts[0]["properties"]["kokyakushiitotenkijoukyou"] == "対象外"
    assert ws.cell(2, "HubSpot登録") == "求人未特定"
    assert ws.cell(2, "HubSpot応募者ID") == cli.created_appts[0]["id"]


def test_ページが一時的に取れなければ応募者ごと次の回にやり直す(tmp_path):
    ws = FakeWs(_values([_row()]))
    cli = FakeClient()
    ledger = S.Ledger(tmp_path / "l.json")
    s = _run(ws, cli, ledger=ledger, page=None)
    assert s["deferred"] == 1 and cli.created_appts == [] and ws.writes == []
    assert ledger.data == {}                      # 失敗回数にも数えない


def test_メールとページで求人が違えば求人を作らない(tmp_path):
    ws = FakeWs(_values([_row(店舗id="9999999")]))
    made: list = []
    s = _run(ws, FakeClient(), ledger=S.Ledger(tmp_path / "l.json"),
             page=FIXTURE.read_text(encoding="utf-8"), made=made)
    assert made == [] and s["listing_create_failed"] == 1
    assert ws.cell(2, "HubSpot登録") == "求人未特定"


def test_同じ求人への応募が複数あっても求人は1回だけ作る(tmp_path):
    ws = FakeWs(_values([_row(), _row(messageId="msg-0002", メールアドレス="other@example.com",
                                      電話番号="08000000001")]))
    made: list = []
    s = _run(ws, FakeClient(), ledger=S.Ledger(tmp_path / "l.json"),
             page=FIXTURE.read_text(encoding="utf-8"), made=made)
    assert len(made) == 1 and s["linked"] == 2


def test_dry_runではHubSpotにもシートにも台帳にも書かない(tmp_path):
    ws = FakeWs(_values([_row()]))
    cli = FakeClient()
    ledger = S.Ledger(tmp_path / "l.json")
    made: list = []
    s = _run(ws, cli, ledger=ledger, dry_run=True,
             page=FIXTURE.read_text(encoding="utf-8"), made=made)
    assert s["targets"] == 1 and s["listing_created"] == 1   # 作る予定として数えるだけ
    assert made == [] and cli.created_appts == [] and cli.associations == []
    assert ws.writes == [] and ledger.data == {}


class _ErrClient(FakeClient):
    def create_appointment(self, properties):
        raise RuntimeError("HTTP 500 (test)")


def test_エラーは上限まで空欄のまま再試行し_超えたらエラーを書いて知らせる(tmp_path):
    ws = FakeWs(_values([_row()]))
    ledger = S.Ledger(tmp_path / "l.json")
    sent: list = []
    for i in range(2):
        s = _run(ws, _ErrClient(listings_reclogi={"10756": "L-1"}), ledger=ledger,
                 max_attempts=3, notify=sent.append)
        assert s["error"] == 1 and s["gave_up"] == 0
        assert ws.cell(2, "HubSpot登録") == "" and sent == []
    s = _run(ws, _ErrClient(listings_reclogi={"10756": "L-1"}), ledger=ledger,
             max_attempts=3, notify=sent.append)
    assert s["gave_up"] == 1
    assert ws.cell(2, "HubSpot登録").startswith("エラー: RuntimeError")
    assert len(sent) == 1 and "行 2" in sent[0]


class _FailWriteWs(FakeWs):
    def batch_update(self, data):
        raise RuntimeError("403 (test)")


def test_書き戻しに失敗しても二重に取り込まず_次の回に書き戻しだけやり直す(tmp_path):
    ledger = S.Ledger(tmp_path / "l.json")
    bad = _FailWriteWs(_values([_row()]))
    cli = FakeClient(listings_reclogi={"10756": "L-1"})
    s = _run(bad, cli, ledger=ledger)
    assert s["linked"] == 1 and s["written"] == 0 and len(cli.created_appts) == 1
    good = FakeWs(bad.values)
    cli2 = FakeClient(listings_reclogi={"10756": "L-1"})
    s2 = _run(good, cli2, ledger=ledger)
    assert s2["targets"] == 0 and s2["rewrite"] == 1 and cli2.created_appts == []
    assert good.cell(2, "HubSpot登録") == "受入済"
    assert good.cell(2, "HubSpot応募者ID") == cli.created_appts[0]["id"]


def test_書き戻しは書く直前にmessageIdで行を引き直す(tmp_path):
    ws = FakeWs(_values([_row(messageId="msg-x", HubSpot登録="対象外"), _row()]))
    cli = FakeClient(listings_reclogi={"10756": "L-1"})
    orig = ws.get_all_values
    calls = {"n": 0}

    def get_all_values():
        calls["n"] += 1
        if calls["n"] == 2:            # 読んだ後で先頭の行が消された
            del ws.values[1]
        return orig()
    ws.get_all_values = get_all_values
    _run(ws, cli, ledger=S.Ledger(tmp_path / "l.json"))
    assert ws.values[1][0] == "msg-0001" and ws.cell(2, "HubSpot登録") == "受入済"


def test_タブが無いときは何もしない(monkeypatch):
    monkeypatch.setattr(rl, "open_tab", lambda: (None, "タブ「リクロジ応募」が無い"))
    s = rl.run(dry_run=False, client=FakeClient())
    assert s["skipped"].startswith("タブ") and s["targets"] == 0


def test_シートIDが無いときは何もしない(monkeypatch):
    monkeypatch.delenv("JAS_APPLICANT_QUEUE_SHEET_ID", raising=False)
    monkeypatch.setattr(aq, "QUEUE_SHEET_ID", "")
    s = rl.run(dry_run=False, client=FakeClient())
    assert "未設定" in s["skipped"]


def test_必要な列が無いタブは読まない():
    ws = FakeWs(_values([_row()], header=[h for h in HEADER if h != "HubSpot登録"]))
    s = rl.run(dry_run=False, ws=ws, client=FakeClient())
    assert "列なし" in s["skipped"] and ws.writes == []


# ---------------------------------------------------------------- 求人検索と relink
def test_応募取込の求人検索はリクロジ求人IDでid_reclogiを引く():
    row = ai.ApplicantRow(name="見本 太郎", kana="", phone="08000000000",
                          email="t@example.com", apply_date="2026-10-14",
                          media="リクロジメディア", media_job_id="10756")
    cli = ai.DryRunClient(listings_hr={"10756": "L-HR"})   # HR の求人には紐付けない
    cli.listings_reclogi = {"10756": "L-1"}
    res = ai.process_applicant(row, cli)
    assert res.status == "linked" and res.listing_id == "L-1"
    assert cli.search_log[0] == {"op": "search_listing_reclogi", "key": {"id_reclogi": "10756"}}
    assert cli.created_appts[0]["properties"]["yingmujinglu"] == ai.RECLOGI_YINGMUJINGLU


@pytest.mark.parametrize("media, expected", [
    ("リクロジメディア", ("id_reclogi", "リクロジメディア")),   # 以前は AirWork 扱いだった
    ("HRハッカー", ("id_hrhakkaa", "HRハッカー")),
    ("AirWork", ("id_airwork", "AirWork")),
    ("", ("id_airwork", "AirWork")),
])
def test_relinkはリクロジメディアをid_reclogiで引き直す(media, expected):
    assert S.relink_media(media) == expected


# ---------------------------------------------------------------- applicant_sync.run の中の位置
@pytest.fixture
def harness(monkeypatch, tmp_path):
    order: list = []
    ledger = S.Ledger(tmp_path / "ledger.json")
    lock_cls = S.Lock
    monkeypatch.setattr(S, "Lock", lambda: lock_cls(tmp_path / "lock"))
    monkeypatch.setattr(S, "Ledger", lambda merge_from="": ledger)
    monkeypatch.setattr(aq, "read_new_items_from_sheet1", lambda **k: [])
    monkeypatch.setattr(aq, "AccountResolver",
                        lambda: SimpleNamespace(build=lambda: SimpleNamespace(resolve=lambda i: None)))
    monkeypatch.setattr(S, "SESSION_DIR", tmp_path / "sess")
    monkeypatch.setattr(S, "slack_notify", lambda message="", dry_run=False: None)

    def fake_rl(dry_run=True, ledger=None, max_attempts=3, notify=None):
        order.append(("reclogi", dry_run, ledger is not None))
        return {"linked": 0}
    monkeypatch.setattr(rl, "run", fake_rl)
    monkeypatch.setattr(css, "run_all", lambda dry_run=True: order.append(("sheet",)) or
                        {"sheets": 0, "ok": 0, "fail": 0, "wrote": 0, "errors": []})
    monkeypatch.setattr(S, "relink", lambda **k: order.append(("relink",)) or {"relinked": 0})
    return order


@pytest.mark.parametrize("media, ran", [("HR", True), ("BOTH", True), ("AW", False)])
def test_リクロジはHRを含む回だけ_後段より前に走る(harness, media, ran):
    S.run(dry_run=False, source="sheet1", media_filter=media, hr_cutoff_iso="2026-10-01")
    names = [o[0] for o in harness]
    assert ("reclogi" in names) is ran
    if ran:
        assert names.index("reclogi") < names.index("sheet") < names.index("relink")
        assert harness[0] == ("reclogi", False, True)


def test_リクロジが落ちても本線と後段は続ける(harness, monkeypatch):
    def boom(**k):
        raise RuntimeError("test")
    monkeypatch.setattr(rl, "run", boom)
    s = S.run(dry_run=False, source="sheet1", media_filter="HR", hr_cutoff_iso="2026-10-01")
    assert "relinked" in s and ("sheet",) in harness
