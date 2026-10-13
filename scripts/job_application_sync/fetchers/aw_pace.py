"""AirWork へのページアクセスの間隔を、PC 全体で最低 N 秒あける (既定 1 秒)。

なぜ (2026-10-13 ユーザー指示):
  GitHub Actions は海外のサーバーとして AirWork に弾かれ、専用PCでの実行に移した。
  応募の取り込みを社ごとに並列にしたので、ページを開く瞬間が重ならないようにする
  (「全くの同時実行は避ける。少なくてもページのアクセスは1秒ずらす」)。

対象: ブラウザのページの移動 (document の読み込み。ログイン・画面の移動・リダイレクト先)。
  画像・CSS・API などページの中の通信は対象にしない。
範囲: 同じプロセスのスレッド (社ごとの並列) と、別のプロセス (求人の巡回など) の両方。
  直前のアクセス時刻を 1 つのファイルに書き、ファイルの鍵で順番にする。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MIN_INTERVAL_SEC = float(os.environ.get("JAS_AW_MIN_INTERVAL_SEC", "1.0"))
PACE_FILE = Path(os.environ.get(
    "JAS_AW_PACE_FILE", str(Path(tempfile.gettempdir()) / "jas_aw_pace.txt")))

_thread_lock = threading.Lock()


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield fd
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def wait_turn(min_interval: float = None, path: Path = None) -> float:
    """前のページアクセスから min_interval 秒たつまで待ち、今を記録する。待った秒数を返す。"""
    gap = MIN_INTERVAL_SEC if min_interval is None else min_interval
    p = path or PACE_FILE
    with _thread_lock, _file_lock(p) as fd:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("ascii", "ignore").strip("\x00 \r\n")
        try:
            last = float(raw)
        except ValueError:
            last = 0.0
        waited = max(0.0, last + gap - time.time())
        if waited:
            time.sleep(waited)
        stamp = f"{time.time():.6f}".encode("ascii").ljust(32)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, stamp)
        return waited


async def install(ctx) -> None:
    """Playwright のコンテキストに、ページの移動ごとの順番待ちを付ける。"""
    if not hasattr(ctx, "route"):   # テストの偽コンテキスト
        return

    async def _handler(route, request):
        try:
            if request.is_navigation_request() and request.resource_type == "document":
                await asyncio.to_thread(wait_turn)
        finally:
            await route.continue_()
    await ctx.route("**/*", _handler)
