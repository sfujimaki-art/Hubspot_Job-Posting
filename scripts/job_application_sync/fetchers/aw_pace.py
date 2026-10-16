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


# ★2026-10-16 (ユーザー承認): 求人の巡回・課金の受け取りは 60〜90 秒あける (応募は 30〜50 秒のまま)。
#   巡回を同時 5 社・30〜50 秒で回した日に、始めてから 25〜55 分で 403 が 2 回出た
#   (16:15・17:10)。応募の取り込みだけの時間帯には 403 は出ていない。
CRAWL_INTERVAL_MIN_SEC = float(os.environ.get("JAS_AW_CRAWL_INTERVAL_MIN_SEC", "60"))
CRAWL_INTERVAL_MAX_SEC = max(CRAWL_INTERVAL_MIN_SEC, float(
    os.environ.get("JAS_AW_CRAWL_INTERVAL_MAX_SEC", "90")))


def company_interval(priority: bool = True) -> float:
    """次の社の始まりまでの間隔 (秒)。応募は 30〜50 秒、巡回・課金は 60〜90 秒の一様乱数。"""
    if priority:
        return random.uniform(COMPANY_INTERVAL_MIN_SEC, COMPANY_INTERVAL_MAX_SEC)
    return random.uniform(max(COMPANY_INTERVAL_MIN_SEC, CRAWL_INTERVAL_MIN_SEC),
                          max(COMPANY_INTERVAL_MAX_SEC, CRAWL_INTERVAL_MAX_SEC))


# ★2026-10-16: 応募の取り込みを先に通す (ユーザーの優先度「応募は速攻で HubSpot へ。求人はゆっくりでよい」)。
#   求人の巡回が同時に 5 社まで順番待ちをするようにした結果、応募の社が巡回の社の後ろに並び、
#   15:10 の回で 141 秒待った。応募の社が待っている間は、巡回・課金の社は順番を譲る
#   (待っている印のファイルを見て、自分の順番の待ちをやめて並び直す)。
PRIORITY_DIR = Path(os.environ.get(
    "JAS_AW_PRIORITY_DIR", str(Path(tempfile.gettempdir()) / "jas_aw_priority")))
PRIORITY_STALE_SEC = 300


def priority_waiting(directory: Path = None) -> bool:
    """応募の社が順番を待っているか (5 分より古い印は落ちたプロセスの残りとして無視)。"""
    d = Path(directory or PRIORITY_DIR)
    try:
        now = time.time()
        return any(now - f.stat().st_mtime < PRIORITY_STALE_SEC for f in d.iterdir())
    except OSError:
        return False


@contextmanager
def _priority_mark():
    d = Path(PRIORITY_DIR)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{os.getpid()}_{threading.get_ident()}"
    f.write_text(str(time.time()), encoding="ascii")
    try:
        yield
    finally:
        try:
            f.unlink()
        except OSError:
            pass


def wait_company_turn(priority: bool = False) -> float:
    """前の社が AirWork へのアクセスを始めてから 30〜50 秒 (乱数) たつまで待つ。PC 全体で共通。

    priority=True (応募の取り込み) は、待っている間 巡回・課金の社に順番を譲らせる。
    """
    if priority:
        with _priority_mark():
            return wait_turn(min_interval=company_interval(), path=COMPANY_PACE_FILE)
    return wait_turn(min_interval=company_interval(priority=False), path=COMPANY_PACE_FILE,
                     yield_to_priority=True)


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

# ★2026-10-16: 鍵はファイルごとに分ける。以前は 1 つの鍵をページの間隔 (1 秒) と
#   社の間隔 (30〜50 秒) で共有しており、社の順番を待つ間 (最大 50 秒) その鍵を持ったまま
#   眠るため、同じプロセスで動いている別の社がページを開けず、45 秒で時間切れになった
#   (14:37 の billing-collect、同時 5 社のうち 4 社が airplf/login の読み込みで時間切れ)。
_thread_locks: dict = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(str(path), threading.Lock())
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


def wait_turn(min_interval: float = None, path: Path = None,
              yield_to_priority: bool = False) -> float:
    """前のアクセスから min_interval 秒たつまで待ち、今を記録する。待った秒数を返す。

    yield_to_priority=True のときは、応募の社が待ち始めたら自分の待ちをやめて
    (時刻を記録せずに鍵を放し) 並び直す。
    """
    gap = MIN_INTERVAL_SEC if min_interval is None else min_interval
    p = path or PACE_FILE
    t0 = time.time()
    while True:
        yielded = False
        with _thread_lock_for(p), _file_lock(p) as fd:
            if not (yield_to_priority and priority_waiting()):
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64).decode("ascii", "ignore").strip("\x00 \r\n")
                try:
                    last = float(raw)
                except ValueError:
                    last = 0.0
                deadline = last + gap
                while time.time() < deadline:
                    if yield_to_priority and priority_waiting():
                        yielded = True
                        break
                    time.sleep(min(0.5, max(0.0, deadline - time.time())))
                if not yielded:
                    stamp = f"{time.time():.6f}".encode("ascii").ljust(32)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, stamp)
                    return time.time() - t0
            else:
                yielded = True
        time.sleep(1.0)   # 鍵を放して、応募の社に先に取らせる


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
