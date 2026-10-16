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
import random
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MIN_INTERVAL_SEC = float(os.environ.get("JAS_AW_MIN_INTERVAL_SEC", "1.0"))
PACE_FILE = Path(os.environ.get(
    "JAS_AW_PACE_FILE", str(Path(tempfile.gettempdir()) / "jas_aw_pace.txt")))
# ★AirWork の CloudFront が 403 を返したら、この PC からのアクセスを止める (2026-10-13)。
#   18:41 の回では 403 の後も残りの社がアクセスを続けていた。止めた時刻は
#   ファイルに残し、次の回 (5分ごと) や求人の巡回 (別プロセス) も見る。
BLOCK_FILE = Path(os.environ.get(
    "JAS_AW_BLOCK_FILE", str(Path(tempfile.gettempdir()) / "jas_aw_blocked_until.txt")))
BLOCK_COOLDOWN_MIN = float(os.environ.get("JAS_AW_BLOCK_COOLDOWN_MIN", "30"))
_AW_HOST_SUFFIXES = ("airwork.net", "airregi.jp")
# ★社と社の間の最低間隔 (秒、PC 全体で共通)。2026-10-13 の実測:
#   1社ずつでも間を空けずに続けた回 (約4分20秒・AirWork/AirRegi 宛て最大4,467件) で 403、
#   5社を約2分 (同 1,405件) では 403 なし、1社に数分かけていた回では一度も 403 なし。
#   既定 30 秒 = 5分で最大10社。
# ★2026-10-16 ユーザー指示「同時に1社は拡大してよい。ただし、ほぼ同時が正しい。
#   30秒〜50秒くらいランダムにずらして作業し続ける」。社の始まりを 30〜50 秒の乱数で
#   ずらし、前の社が終わるのは待たない (同時に動く社の数は呼び出し側の上限で決める)。
#   AirWork へのリクエストの量は始まりの間隔で決まる (1社あたり約265件 ÷ 平均40秒 ≒ 7件/秒。
#   403 になった回は約17件/秒、403 にならなかった5社/2分の回は約12件/秒)。
#   JAS_AW_COMPANY_INTERVAL_SEC を指定したときは従来どおり固定の間隔。
_FIXED = os.environ.get("JAS_AW_COMPANY_INTERVAL_SEC", "")
COMPANY_INTERVAL_MIN_SEC = float(_FIXED or os.environ.get("JAS_AW_COMPANY_INTERVAL_MIN_SEC", "30"))
COMPANY_INTERVAL_MAX_SEC = max(COMPANY_INTERVAL_MIN_SEC, float(
    _FIXED or os.environ.get("JAS_AW_COMPANY_INTERVAL_MAX_SEC", "50")))
COMPANY_INTERVAL_SEC = COMPANY_INTERVAL_MIN_SEC   # 互換 (表示用)
COMPANY_PACE_FILE = Path(os.environ.get(
    "JAS_AW_COMPANY_PACE_FILE", str(Path(tempfile.gettempdir()) / "jas_aw_company_pace.txt")))


def company_interval() -> float:
    """次の社の始まりまでの間隔 (秒)。最小〜最大の一様乱数。"""
    return random.uniform(COMPANY_INTERVAL_MIN_SEC, COMPANY_INTERVAL_MAX_SEC)


def wait_company_turn() -> float:
    """前の社が AirWork へのアクセスを始めてから 30〜50 秒 (乱数) たつまで待つ。PC 全体で共通。"""
    return wait_turn(min_interval=company_interval(), path=COMPANY_PACE_FILE)


def blocked_until(path: Path = None) -> float:
    """アクセスを止めている期限 (epoch 秒)。止めていなければ 0。"""
    try:
        return float((path or BLOCK_FILE).read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def is_blocked(path: Path = None) -> bool:
    return time.time() < blocked_until(path)


def mark_blocked(minutes: float = None, path: Path = None) -> float:
    """今から minutes 分、AirWork へのアクセスを止める。期限を返す。"""
    until = time.time() + 60 * (BLOCK_COOLDOWN_MIN if minutes is None else minutes)
    p = path or BLOCK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    if until > blocked_until(p):
        p.write_text(f"{until:.0f}", encoding="ascii")
    return until


def is_cloudfront_block(status: int, url: str, headers: dict) -> bool:
    """AirWork / AirRegi の CloudFront が返した 403 か。"""
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    server = str((headers or {}).get("server", "")).lower()
    return (status == 403 and host.endswith(_AW_HOST_SUFFIXES)
            and "cloudfront" in server)

_thread_lock = threading.Lock()
# 観測用: このプロセスで AirWork 側へ送った / 止めたリクエストの数
stats = {"sent": 0, "blocked": 0, "navigations": 0, "waited_sec": 0.0,
         "cloudfront_403": 0}
by_kind: dict = {}     # "ホスト|種類" → 件数
_stats_lock = threading.Lock()


def _count(key: str, n=1) -> None:
    with _stats_lock:
        stats[key] += n


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


# 読み込まない種類 (2026-10-13)。10社同時にアクセスした回で、AirWork の CloudFront が
# 「403 Request blocked」を返した (18:41〜18:44、約45分で解除)。1社ずつにしたうえで、
# 画面の判定に使わない画像・フォント・動画は読み込まず、1社あたりの通信量を減らす。
# スタイルシートは読む (応募者一覧のボタンは画面の幅で出たり消えたりする)。
BLOCK_RESOURCE_TYPES = {"image", "font", "media"}


def _is_main_frame_navigation(request) -> bool:
    try:
        return (request.is_navigation_request()
                and request.resource_type == "document"
                and request.frame.parent_frame is None)
    except Exception:  # noqa: BLE001
        return False


async def install(ctx) -> None:
    """Playwright のコンテキストに、ページの移動ごとの順番待ちと、不要な読み込みの停止を付ける。"""
    if not hasattr(ctx, "route"):   # テストの偽コンテキスト
        return

    def _on_response(response):
        try:
            if is_cloudfront_block(response.status, response.url, response.headers):
                mark_blocked()
                _count("cloudfront_403")
        except Exception:  # noqa: BLE001
            pass
    ctx.on("response", _on_response)

    async def _handler(route, request):
        if is_blocked():          # 403 を受けた後は何も送らない
            _count("blocked")
            await route.abort()
            return
        if request.resource_type in BLOCK_RESOURCE_TYPES:
            _count("blocked")
            await route.abort()
            return
        try:
            if _is_main_frame_navigation(request):
                _count("navigations")
                _count("waited_sec", await asyncio.to_thread(wait_turn))
        finally:
            _count("sent")
            try:
                from urllib.parse import urlsplit
                k = f"{urlsplit(request.url).hostname}|{request.resource_type}"
            except Exception:  # noqa: BLE001
                k = "?"
            with _stats_lock:
                by_kind[k] = by_kind.get(k, 0) + 1
            await route.continue_()
    await ctx.route("**/*", _handler)
