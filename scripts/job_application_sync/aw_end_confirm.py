# -*- coding: utf-8 -*-
"""エアワークの一覧から消えた求人を、公開ページで 2 回確かめてから公開終了にする (2026-10-23)。

★なぜ要るか
  1 口座で「公開中」の 20% を超える求人がエクスポートから消えると、壊れた取得の疑いとして
  公開終了を見送っていた (end_vanished_listings.MAX_END_RATIO)。10/20〜23 の巡回では、
  見送った件数が「公開ページが開けない」件数と毎回一致しており、多くは本当に終わった求人と
  考えられた (推測)。ユーザー決定 (2026-10-23、案 A): ネット回線が原因の可能性もあるので、
  次の 2 つがそろったときだけ公開終了にする。
    1. 同じ回に、その社のほかの公開ページ (エクスポートに載っている求人。無ければ採用サイトの
       トップ) は開けている (= ネットは生きている)。
    2. CONFIRM_AFTER_HOURS 時間以上あけて (次の夜) もう一度見ても、ページが無い (404/410)。
  さらに「復活した時の処理も踏まえる」(同日): 公開終了にした求人がエクスポートに再び
  「公開中」で載れば、通常の更新で公開中に戻る (airwork_import.build_update_props)。
  ここではそれを数えて Slack の対象一覧に出す (reopened)。

台帳: data/job_application_sync/aw_end_pending.json (JAS_AW_END_PENDING で変更可)
  {"pending": {hubspot_id: {"login_id", "first_gone"}},   1 回目に「ページが無い」と見た時刻
   "ended":   {hubspot_id: {"login_id", "at"}}}           この仕組みで公開終了にした時刻
  ページが開いた・エクスポートに戻った求人は pending から外す。ended は ENDED_KEEP_DAYS 日で消す。
公開ページは 1 秒に 1 件まで (aw_listing_images と同じ)。ログインはしない。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from scripts.job_application_sync import aw_listing_images as _im
from scripts.job_application_sync.fetchers.aw_pace import _file_lock

LEDGER_PATH = Path(os.environ.get(
    "JAS_AW_END_PENDING", "data/job_application_sync/aw_end_pending.json"))
CONFIRM_AFTER_HOURS = float(os.environ.get("JAS_AW_END_CONFIRM_HOURS", "12"))
ENDED_KEEP_DAYS = 60
MAX_CHECK = int(os.environ.get("JAS_AW_END_CONFIRM_MAX_PAGES", "200"))


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("pending", {})
    d.setdefault("ended", {})
    return d


def _write(p: Path, d: dict, now: float) -> None:
    cutoff = now - ENDED_KEEP_DAYS * 86400
    d["ended"] = {k: v for k, v in d["ended"].items() if v.get("at", 0) >= cutoff}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _page_url(props: dict, fallback_site: str):
    media = (props.get("id_airwork") or "").strip()
    site = _im.own_site(props.get("url_airwork"), media) or fallback_site
    if not site:
        return None
    try:
        return _im.job_url(site, media)
    except _im.CaptureError:
        return None


def check_pages(candidates: list, controls: list, recruit_site_url: str = "",
                session=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """公開ページを見る。{"net_ok", "gone": set(id), "alive": set(id), "unknown": n}。

    candidates / controls は HubSpot の求人 {id, properties{id_airwork, url_airwork}}。
    control が 1 件も開けなければ net_ok=False で、候補は見ない (何も決めない)。
    """
    session = session or requests.Session()
    fallback = _im.site_from_recruit_url(recruit_site_url)
    last = [None]

    def fetch(url):
        if last[0] is not None:
            wait = _im.PAGE_INTERVAL - (clock() - last[0])
            if wait > 0:
                sleep(wait)
        last[0] = clock()
        return _im.get_page(session, url)

    out = {"net_ok": False, "gone": set(), "alive": set(), "unknown": 0}
    tried = 0
    # 1) ネットが生きているか: エクスポートに載っている求人のページを 1 件 (最大 3 件まで試す)。
    #    無ければ採用サイトのトップ。
    control_urls = [u for u in (_page_url(c.get("properties") or {}, fallback)
                                for c in controls) if u][:3]
    site_top = None
    for c in candidates:
        media = ((c.get("properties") or {}).get("id_airwork") or "").strip()
        site_top = _im.own_site((c.get("properties") or {}).get("url_airwork"), media) or fallback
        if site_top:
            break
    if not control_urls and site_top:
        control_urls = [site_top + "/"]
    for u in control_urls:
        tried += 1
        try:
            fetch(u)
            out["net_ok"] = True
            break
        except _im.CaptureError:
            continue
    if not out["net_ok"]:
        return out
    # 2) 候補のページ。404/410 だけを「無い」とする。それ以外の失敗は決めない。
    for c in candidates[:MAX_CHECK]:
        url = _page_url(c.get("properties") or {}, fallback)
        if not url:
            out["unknown"] += 1
            continue
        try:
            fetch(url)
            out["alive"].add(str(c["id"]))
        except _im.CaptureError as e:
            if str(e) == "public_page_unavailable":
                out["gone"].add(str(c["id"]))
            else:
                out["unknown"] += 1
    out["unknown"] += max(0, len(candidates) - MAX_CHECK)
    return out


def decide(login_id: str, candidate_ids, checked: dict, now: float = None,
           path: Path = None, dry_run: bool = False) -> dict:
    """台帳と照らして、今回公開終了にしてよい求人 ID を決める。

    返り値: {"confirm": set(id), "first": n (1 回目として記録), "alive": n}
    """
    now = time.time() if now is None else now
    p = Path(path or LEDGER_PATH)
    cand = {str(x) for x in candidate_ids}
    confirm, first = set(), 0
    with _file_lock(p.with_suffix(".lock")):
        d = _read(p)
        pend = d["pending"]
        before = json.dumps(pend, sort_keys=True)
        # この口座の記録のうち、今回の候補に無い (エクスポートに戻った・公開中でなくなった) ものは外す
        for hid in [k for k, v in pend.items() if v.get("login_id") == login_id and k not in cand]:
            del pend[hid]
        if checked.get("net_ok"):
            for hid in checked.get("alive", ()):
                pend.pop(hid, None)
            for hid in checked.get("gone", ()):
                e = pend.get(hid)
                if e and now - float(e.get("first_gone", now)) >= CONFIRM_AFTER_HOURS * 3600:
                    confirm.add(hid)
                elif not e:
                    pend[hid] = {"login_id": login_id, "first_gone": now}
                    first += 1
        if not dry_run and json.dumps(pend, sort_keys=True) != before:
            _write(p, d, now)
    return {"confirm": confirm, "first": first, "alive": len(checked.get("alive", ()))}


def mark_ended(login_id: str, ids, now: float = None, path: Path = None) -> None:
    """公開終了を書けた求人を pending から ended へ移す。"""
    ids = [str(x) for x in ids]
    if not ids:
        return
    now = time.time() if now is None else now
    p = Path(path or LEDGER_PATH)
    with _file_lock(p.with_suffix(".lock")):
        d = _read(p)
        for hid in ids:
            d["pending"].pop(hid, None)
            d["ended"][hid] = {"login_id": login_id, "at": now}
        _write(p, d, now)


def note_reopened(ids, path: Path = None, dry_run: bool = False) -> int:
    """この仕組みで公開終了にした求人が、エクスポートで再び公開中になった数。台帳からも外す。"""
    ids = {str(x) for x in ids}
    p = Path(path or LEDGER_PATH)
    if not ids or not p.exists():
        return 0
    with _file_lock(p.with_suffix(".lock")):
        d = _read(p)
        hit = [hid for hid in ids if hid in d["ended"]]
        dropped = [hid for hid in ids if hid in d["pending"]]
        for hid in dropped:
            del d["pending"][hid]
        for hid in hit:
            del d["ended"][hid]
        if not dry_run and (hit or dropped):
            _write(p, d, time.time())
    return len(hit)
