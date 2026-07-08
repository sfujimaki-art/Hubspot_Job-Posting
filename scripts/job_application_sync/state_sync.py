"""状態ファイルの GCS ↔ ローカル 同期 (クラウド実行用).

WBS 1.11.9 クラウド移行。Cloud Run Jobs のコンテナは揮発するため、
状態ファイル (AWキュー / HRスナップショット / storage_state) を GCS に永続化する。

方針: 既存コードはローカルパス依存のまま無改修。コンテナ境界で pull/push する。
  entrypoint:  state_sync pull  →  <実ジョブ>  →  state_sync push

同期対象 (ローカル data/job_application_sync/ 配下):
  - aw_pending_queue.json          (Phase1→Phase2 引き継ぎ)
  - hr_snapshots/*.json.gz         (HR差分基準)
  - hr_storage_state.json          (HRセッション再利用)

環境変数:
  JAS_GCS_BUCKET   : 同期先バケット名 (未設定なら no-op = ローカル運用)
  JAS_STATE_PREFIX : バケット内プレフィックス (既定 "state/job_application_sync")

CLI:
  python -m scripts.job_application_sync.state_sync pull
  python -m scripts.job_application_sync.state_sync push
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# 同期するローカルディレクトリ (このディレクトリ配下を丸ごとGCSと同期)
LOCAL_STATE_DIR = _REPO / "data" / "job_application_sync"

# GCSに載せる相対パス (ディレクトリはprefixで再帰)
SYNC_TARGETS = [
    "aw_pending_queue.json",
    "hr_storage_state.json",
    "hr_snapshots",   # ディレクトリ (中の *.json.gz を全て)
    # 応募連携 (2026-07-07): 処理台帳 + AWアカウント別セッション
    "applicant_ledger.json",
    "aw_sessions",    # ディレクトリ (アカウント別 storage_state)
]


def _bucket():
    """GCSバケット取得。未設定なら None (no-op)。"""
    name = os.environ.get("JAS_GCS_BUCKET", "").strip()
    if not name:
        return None
    from google.cloud import storage  # 遅延 import (ローカルで未install許容)
    return storage.Client().bucket(name)


def _prefix() -> str:
    return os.environ.get("JAS_STATE_PREFIX", "state/job_application_sync").strip("/")


def _iter_local_files():
    """同期対象のローカルファイルを (相対パス, Path) で列挙。"""
    for t in SYNC_TARGETS:
        p = LOCAL_STATE_DIR / t
        if p.is_file():
            yield t, p
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    yield str(f.relative_to(LOCAL_STATE_DIR)).replace("\\", "/"), f


def pull() -> int:
    """GCS → ローカル。ダウンロードしたファイル数を返す。no-opなら0。"""
    bkt = _bucket()
    if bkt is None:
        print("[state_sync] JAS_GCS_BUCKET 未設定 → pull skip (ローカル運用)")
        return 0
    pref = _prefix()
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for blob in bkt.list_blobs(prefix=pref + "/"):
        rel = blob.name[len(pref) + 1:]
        if not rel:
            continue
        dest = LOCAL_STATE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))
        n += 1
    print(f"[state_sync] pull: {n} files from gs://{bkt.name}/{pref}/")
    return n


def push() -> int:
    """ローカル → GCS。アップロードしたファイル数を返す。no-opなら0。"""
    bkt = _bucket()
    if bkt is None:
        print("[state_sync] JAS_GCS_BUCKET 未設定 → push skip (ローカル運用)")
        return 0
    pref = _prefix()
    n = 0
    for rel, path in _iter_local_files():
        blob = bkt.blob(f"{pref}/{rel}")
        blob.upload_from_filename(str(path))
        n += 1
    print(f"[state_sync] push: {n} files to gs://{bkt.name}/{pref}/")
    return n


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else ""
    if cmd == "pull":
        pull()
    elif cmd == "push":
        push()
    else:
        print("usage: state_sync.py [pull|push]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
