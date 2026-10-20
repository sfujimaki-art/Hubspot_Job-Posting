"""応募の求人が HubSpot に無い社を「すぐ取りに行く」待ち行列 (2026-10-16)。

なぜ (ユーザー指示「求人は応募があったときに未連携なら速攻で。あとは1日に1回巡回できていればOK」):
  応募の取り込み (5分ごと) は求人を取りに行かない (2026-10-13 決定。1社の求人取得に約10分
  かかり、その間ほかの社の応募が止まるため)。代わりに、求人が無かった社をここに積み、
  別のタスク (aw_orchestrator --from-urgent-queue、5分ごと) がその社の求人だけを取って
  紐付け直す。応募の取り込みは待たない。

ファイル: data/job_application_sync/aw_urgent_jobs.json (JAS_AW_URGENT_QUEUE で変更可)
  {"pending": {login_id: {"company", "job_ids", "queued_at", "attempts"}},
   "tried":   {job_id: 取りに行った時刻 (epoch 秒)}}
同じ求人IDは、取りに行ってから TRY_AGAIN_HOURS 時間は積み直さない (媒体の一覧に無い求人で
5分ごとに取り直し続けないため)。ファイルの読み書きは鍵をかけて 1 プロセスずつ。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scripts.job_application_sync.fetchers.aw_pace import _file_lock

QUEUE_PATH = Path(os.environ.get(
    "JAS_AW_URGENT_QUEUE", "data/job_application_sync/aw_urgent_jobs.json"))
TRY_AGAIN_HOURS = float(os.environ.get("JAS_AW_URGENT_TRY_AGAIN_HOURS", "24"))
MAX_ATTEMPTS = 3


def _path(path: Path = None) -> Path:
    return Path(path or QUEUE_PATH)


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("pending", {})
    d.setdefault("tried", {})
    return d


def _write(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - TRY_AGAIN_HOURS * 3600
    d["tried"] = {j: t for j, t in d["tried"].items() if t >= cutoff}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _locked(p: Path):
    return _file_lock(p.with_suffix(".lock"))


def add(login_id: str, company: str, job_ids, path: Path = None) -> list:
    """取りに行く社を積む。積んだ求人IDを返す (最近取りに行った求人IDは除く)。"""
    if not login_id:
        return []
    p = _path(path)
    now = time.time()
    with _locked(p):
        d = _read(p)
        cutoff = now - TRY_AGAIN_HOURS * 3600
        new = [j for j in dict.fromkeys(str(x) for x in job_ids if x)
               if d["tried"].get(j, 0) < cutoff]
        if not new:
            return []
        e = d["pending"].setdefault(login_id, {"company": company, "job_ids": [],
                                               "queued_at": now, "attempts": 0})
        e["job_ids"] = list(dict.fromkeys(list(e["job_ids"]) + new))
        _write(p, d)
        return new


def pending(path: Path = None) -> dict:
    """積まれている社 (古い順)。"""
    d = _read(_path(path))
    return dict(sorted(d["pending"].items(), key=lambda kv: kv[1].get("queued_at", 0)))


def finish(login_id: str, ok: bool, path: Path = None) -> None:
    """取りに行った結果を記録する。成功か、MAX_ATTEMPTS 回失敗したら外す。

    外した社の求人IDは「取りに行った」として TRY_AGAIN_HOURS 時間は積み直さない。
    取れた後も応募の求人が無いなら、媒体の一覧に無い求人 (終わった求人など) と考える。
    """
    p = _path(path)
    with _locked(p):
        d = _read(p)
        e = d["pending"].get(login_id)
        if not e:
            return
        e["attempts"] = int(e.get("attempts", 0)) + 1
        if ok or e["attempts"] >= MAX_ATTEMPTS:
            now = time.time()
            for j in e.get("job_ids", []):
                d["tried"][j] = now
            del d["pending"][login_id]
        _write(p, d)
