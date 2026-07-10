"""HubSpot Note(エンゲージメント) 操作の共有ヘルパー (WBS 1.11.9)。

用途:
  ③ 新規LISTING作成時に暗黙知テンプレNoteを付与 (attach_template_note)
  ② 応募がLISTINGに紐づいた時、LISTINGの暗黙知テンプレNote本文を応募へ複製
     (copy_listing_note_to_appointment)

設計:
  - association typeId は直書きせず **default association** エンドポイントを使う。
    Note↔LISTING(=899)/Note↔APPOINTMENT の内部IDに依存しない (2026-07-10)。
  - TEMPLATE_BODY はここを SSOT とする (旧 pin_anmokuchi_template.py と同一本文)。
  - 冪等性は pin プロパティだけに依存せず、Note本文の署名(TEMPLATE_SIGNATURE)で判定。
    → pin patch が部分失敗しても二重付与しない (2026-07-10 逆証明で発見・是正)。
  - 書込(create/associate/patch/delete)は 429/5xx を指数バックオフでリトライ。
  - associate 失敗時は作成済みNoteを削除(ロールバック)し孤児Noteを残さない。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.exceptions import RequestException

BASE = "https://api.hubapi.com"
LISTING = "0-420"
APPOINTMENT = "0-421"

# ③ 暗黙知入力テンプレート本文 (2026-05-17 初版, HubSpot Note は HTML 可)。
# CSMgr/コンサルが求人ごとの暗黙知を記入するためのテンプレート。顧客非開示。
TEMPLATE_BODY = """<h2>📋 暗黙知入力テンプレート（求人ごと）</h2>
<p><i>CSMgr/コンサルが求人ごとの暗黙知を記入するためのテンプレートです。<br>
プロパティ欄（暗黙知タブ）と併用してください。<br>
顧客には開示しない情報（社内専用）です。</i></p>
<hr>

<h3>■ 足切り基準（顧客非開示）</h3>
<ul>
<li><b>年齢</b>: </li>
<li><b>経験年数</b>: </li>
<li><b>必須資格</b>: </li>
<li><b>学歴NG</b>: </li>
<li><b>前職業界NG</b>: </li>
<li><b>前職企業NG</b>: </li>
<li><b>その他</b>: </li>
<li><b>自由記述</b>: </li>
</ul>

<h3>■ 一次対応ヒアリング項目（BPOが応募者に確認）</h3>
<ul>
<li>① 保有資格: </li>
<li>② 職歴（前職について）: </li>
<li>③ 転職理由・転職可能時期: </li>
<li>④ 連絡可能時間帯: </li>
<li>⑤ 面接希望時期（曜日・時間帯）: </li>
<li>⑥ 備考: </li>
</ul>

<h3>■ 二次面接ヒアリング項目（顧客固有）</h3>
<ul>
<li>確認事項: </li>
</ul>

<h3>■ 書類回収ルール</h3>
<ul>
<li><b>必要書類</b>: 履歴書 / 職務経歴書 / 卒業証明書 / 資格証明 / その他</li>
<li><b>回収タイミング</b>: 応募時 / 一次面接前 / 二次面接前 / 内定前 / 内定後</li>
<li><b>提出形式</b>: メール添付 / 郵送 / 対面 / その他</li>
<li><b>確認担当</b>: BPO / コンサル / 顧客</li>
</ul>

<h3>■ 優先順位ルール</h3>
<ul>
<li><b>採用優先順位</b>: 最優先 / 高 / 中 / 低</li>
<li><b>急ぎ度</b>: 緊急 / 通常 / 長期</li>
<li><b>推薦時の注意点</b>: </li>
</ul>

<h3>■ 顧客が重視するポイント（書類選考時の評価軸）</h3>
<p></p>

<h3>■ 顧客固有の質問事項</h3>
<p></p>

<h3>■ 想定NG・特殊事情</h3>
<p></p>

<h3>■ コンサル所感（社内向け詳細）</h3>
<p></p>

<hr>
<p><small>※入力後、関連するプロパティ（暗黙知タブ）にも反映してください。<br>
※このテンプレートは新規求人レコード作成時に自動付与されます。</small></p>"""

# ③テンプレNoteの署名。冪等判定/②のコピー元特定に使う (本文の一意な見出し)。
# コンサルが記入してもこの見出しは残る前提。
TEMPLATE_SIGNATURE = "暗黙知入力テンプレート"

# ②複製Noteの識別マーカー。既存ワークフローが新規応募に別テンプレNote
# (「応募者対応メモテンプレート」)を自動付与するため、「Noteが1件でもあるか」
# では二重コピー判定できない。自分の複製だけをこのマーカーで見分ける。
COPIED_NOTE_MARKER = "【求人由来の暗黙知（自動コピー）"


def _headers(token: Optional[str] = None) -> dict:
    token = token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _send(method: str, url: str, *, token: Optional[str] = None,
          json_body: Optional[dict] = None, timeout: int = 30,
          retries: int = 3):
    """429/5xx・接続例外を指数バックオフでリトライする HTTP 送信 (F3-03)。

    書込(POST/PUT/PATCH/DELETE)も読取(GET)もこれを通す。冪等判定の読取を
    リトライしないと一過性エラーでフェイルオープンし二重作成を招く(F2-01)。
    """
    fn = getattr(requests, method.lower())
    r = None
    for attempt in range(retries):
        try:
            if json_body is not None:
                r = fn(url, headers=_headers(token), json=json_body,
                       timeout=timeout)
            else:
                r = fn(url, headers=_headers(token), timeout=timeout)
        except RequestException:  # 接続/タイムアウト (R2-NOTES-02)
            r = None
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
            continue
        sc = getattr(r, "status_code", 0)
        if (sc == 429 or sc >= 500) and attempt < retries - 1:  # R2-NOTES-03
            time.sleep(min(2 ** attempt, 8))
            continue
        return r
    return r


def create_note(body: str, *, token: Optional[str] = None,
                timeout: int = 30) -> Optional[str]:
    """Note作成 (関連付けなし)。note_id or None。"""
    r = _send("POST", f"{BASE}/crm/v3/objects/notes", token=token,
              json_body={"properties": {"hs_note_body": body,
                                        "hs_timestamp": str(_utc_now_ms())}},
              timeout=timeout)
    if r is not None and r.status_code in (200, 201):
        return r.json()["id"]
    return None


def associate_note(note_id: str, obj_type: str, obj_id: str, *,
                   token: Optional[str] = None, timeout: int = 30) -> bool:
    """Note を任意オブジェクトへ default association で紐付け (typeId不要)。"""
    url = (f"{BASE}/crm/v4/objects/notes/{note_id}"
           f"/associations/default/{obj_type}/{obj_id}")
    r = _send("PUT", url, token=token, timeout=timeout)
    return r is not None and r.status_code in (200, 201)


def delete_note(note_id: str, *, token: Optional[str] = None,
                timeout: int = 30) -> bool:
    """Note削除 (associate失敗時のロールバック用)。"""
    try:
        r = _send("DELETE", f"{BASE}/crm/v3/objects/notes/{note_id}",
                  token=token, timeout=timeout)
        return r is not None and r.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def _note_body(note_id: str, *, token: Optional[str] = None,
               timeout: int = 30) -> Optional[str]:
    """Note本文(hs_note_body)を返す。取得失敗(削除済等)は None。"""
    rn = _send("GET",
               f"{BASE}/crm/v3/objects/notes/{note_id}?properties=hs_note_body",
               token=token, timeout=timeout)
    if rn is not None and rn.status_code == 200:
        return (rn.json().get("properties") or {}).get("hs_note_body")
    return None


def _listing_note_ids(listing_id: str, *, token: Optional[str] = None,
                      timeout: int = 30) -> list:
    """LISTINGに紐づくNote IDの一覧 (pin優先で先頭に置く)。ページング対応(F2-03)。"""
    ids: list = []
    r = _send("GET", f"{BASE}/crm/v3/objects/{LISTING}/{listing_id}"
              f"?properties=hs_pinned_engagement_id", token=token,
              timeout=timeout)
    if r is not None and r.status_code == 200:
        pin = (r.json().get("properties") or {}).get("hs_pinned_engagement_id")
        if pin:
            ids.append(str(pin))
    after = None
    while True:
        url = (f"{BASE}/crm/v4/objects/{LISTING}/{listing_id}"
               f"/associations/notes?limit=500")
        if after:
            url += f"&after={after}"
        ra = _send("GET", url, token=token, timeout=timeout)
        if ra is None or ra.status_code != 200:
            break
        j = ra.json()
        for x in j.get("results", []):
            nid = str(x.get("toObjectId"))
            if nid not in ids:
                ids.append(nid)
        after = (j.get("paging", {}).get("next", {}) or {}).get("after")
        if not after:
            break
    return ids


def get_listing_template_note_body(listing_id: str, *,
                                   token: Optional[str] = None,
                                   timeout: int = 30) -> Optional[str]:
    """LISTINGの「暗黙知テンプレ署名を含むNote」本文を返す (無ければ None)。

    pin留めが署名を含めばそれを、そうでなければ関連Noteを走査して署名一致を採用。
    署名で限定することで無関係なNoteを誤って応募へ複製しない (F4)。dangling pin
    (削除済Noteを指すpin)は本文取得に失敗するため自然に関連Noteへフォールバック(F3)。
    """
    for nid in _listing_note_ids(listing_id, token=token, timeout=timeout):
        body = _note_body(nid, token=token, timeout=timeout)
        if body and TEMPLATE_SIGNATURE in body:
            return body
    return None


def listing_has_template_note(listing_id: str, *, token: Optional[str] = None,
                              timeout: int = 30) -> bool:
    """LISTINGに暗黙知テンプレNote(署名一致)が既にあれば True (③冪等判定)。

    pinの有無ではなく本文署名で判定するため、pin patch が部分失敗して未pinでも
    署名付きNoteを検知して二重付与を防ぐ (F3-01/F3-05)。
    """
    return get_listing_template_note_body(
        listing_id, token=token, timeout=timeout) is not None


def has_copied_note(appointment_id: str, *, token: Optional[str] = None,
                    timeout: int = 30) -> bool:
    """応募に②複製Note(マーカー付き)が既にあれば True (二重コピー防止)。

    既存ワークフローの「応募者対応メモテンプレート」Noteとは区別する。
    判定不能(association GETが最終的に非200=一過性エラー)は **フェイルクローズ**
    = True を返して複製を見送る(二重作成を避ける、次回リトライで拾う, F2-01)。
    """
    ra = _send("GET", f"{BASE}/crm/v4/objects/{APPOINTMENT}/{appointment_id}"
               f"/associations/notes", token=token, timeout=timeout)
    if ra is None or ra.status_code != 200:
        return True  # 判定不能 → 安全側(複製しない)
    for x in ra.json().get("results", []):
        body = _note_body(str(x.get("toObjectId")), token=token, timeout=timeout)
        if body and COPIED_NOTE_MARKER in body:
            return True
    return False


# attach_template_note の「既に有り=skip」を失敗(None)と区別する番兵 (F3R2-02)。
SKIPPED = "__skipped__"


def attach_template_note(listing_id: str, *, dry_run: bool = True,
                         token: Optional[str] = None,
                         skip_if_present: bool = True) -> Optional[str]:
    """③ LISTINGに暗黙知テンプレNoteを付与 + hs_pinned_engagement_id設定。

    冪等: 既に署名付きテンプレNoteがあれば何もしない(pin部分失敗にも強い)。
    associate失敗時は作成済Noteを削除しロールバック(孤児Noteを残さない)。
    pin設定はbest-effort(Noteは関連付けで既に可視、冪等は署名で担保)。
    戻り: 付与=note_id / dry_run="dry-run" / 既存skip=SKIPPED / 書込失敗=None。
    """
    if skip_if_present and listing_has_template_note(listing_id, token=token):
        return SKIPPED
    if dry_run:
        return "dry-run"
    nid = create_note(TEMPLATE_BODY, token=token)
    if not nid:
        return None  # create失敗 (F3R2-02: 呼び出し側で failed 計上)
    if not associate_note(nid, LISTING, listing_id, token=token):
        # ロールバック: 孤児Noteを残さない (F3-02)。削除失敗はwarn (F3R2-03)。
        if not delete_note(nid, token=token):
            print(f"[warn] ③孤児Note削除失敗 note={nid} listing={listing_id} "
                  f"(要手動掃除)", flush=True)
        return None
    # pin は best-effort (失敗しても署名で冪等・Noteは可視)
    _send("PATCH", f"{BASE}/crm/v3/objects/{LISTING}/{listing_id}",
          token=token, json_body={"properties":
                                  {"hs_pinned_engagement_id": nid}})
    return nid


def attach_template_notes(listing_ids, *, dry_run: bool = False,
                          token: Optional[str] = None):
    """③ 複数LISTINGにテンプレNoteを付与。(付与件数, 失敗listing_id一覧) を返す。

    失敗を握り潰さず呼び出し側へ返す(サイレント失敗防止, F3-04/F3R2-02)。
    既存skipは失敗ではないので failed に入れない。create/associate 失敗のみ failed。
    """
    ok = 0
    failed: list = []
    for lid in listing_ids:
        try:
            r = attach_template_note(str(lid), dry_run=dry_run, token=token)
            if r is None:
                failed.append(str(lid))          # create/associate 失敗
            elif r != SKIPPED:
                ok += 1                            # note_id or "dry-run"
        except Exception:  # noqa: BLE001
            failed.append(str(lid))
        time.sleep(0.15)
    return ok, failed


def copy_listing_note_to_appointment(listing_id: str, appointment_id: str, *,
                                     dry_run: bool = True,
                                     token: Optional[str] = None,
                                     skip_if_has_note: bool = True
                                     ) -> Optional[str]:
    """② LISTINGの暗黙知テンプレNote本文を応募へ同文Noteとして複製・紐付け。

    一次対応コーラーが応募画面で足切り基準・ヒアリング項目を見られるようにする。
    応募が既に②複製Note(マーカー)を持つ場合はskip。LISTINGに署名付きNoteが無ければ
    None。associate失敗時は作成済Noteを削除しロールバック(孤児Noteを残さない, F1)。
    戻り: 複製note_id / dry_run="dry-run" / skip・該当なし・失敗=None。
    """
    if skip_if_has_note and not dry_run:
        if has_copied_note(appointment_id, token=token):
            return None
    body = get_listing_template_note_body(listing_id, token=token)
    if not body:
        return None
    header = (f"<p><small>{COPIED_NOTE_MARKER} "
              f"listing={listing_id}】</small></p>")
    body2 = header + body
    if dry_run:
        return "dry-run"
    nid = create_note(body2, token=token)
    if not nid:
        return None
    if not associate_note(nid, APPOINTMENT, appointment_id, token=token):
        # ロールバック: 孤児Noteを残さない (F1)。削除失敗はwarn (F3R2-03)。
        if not delete_note(nid, token=token):
            print(f"[warn] ②孤児Note削除失敗 note={nid} appt={appointment_id} "
                  f"(要手動掃除)", flush=True)
        return None
    return nid
