# -*- coding: utf-8 -*-
"""巡回と一緒に画像の URL を書く (2026-10-16)。通信はすべて偽物。"""
from __future__ import annotations

from scripts.job_application_sync import aw_listing_images as I

SITE = "https://arwrk.net/recruit/acme"


def _page(job, imgs=None, empty_nuxt=False):
    url = f"{SITE}/{job}/"
    body = f'<html><head><link rel="canonical" href="{url}"></head><body>'
    if imgs is not None:
        body += '<div class="job-img"><div class="slider">' + "".join(
            f'<img src="https://cdn.arwrk.net/images/rct/{i}.jpg?thumb=1">' for i in imgs) + "</div></div>"
    if empty_nuxt:
        body += "<script>window.__NUXT__={jobOverViewSection:{images:[]}}</script>"
    return (body + "</body></html>").encode("utf-8")


class _Resp:
    def __init__(self, status, body=b""):
        self.status_code, self.body = status, body
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield self.body


class _Session:
    def __init__(self, pages):
        self.pages, self.got = pages, []

    def get(self, url, **k):
        self.got.append(url)
        return _Resp(*self.pages[url]) if url in self.pages else _Resp(404)


class _HS:
    def __init__(self, recs):
        self.recs, self.written = recs, []

    def open_listings(self, login_id):
        return self.recs

    def batch_update(self, inputs):
        self.written.extend(inputs)
        return True


def _rec(i, job, cur=None, url=None):
    return {"id": i, "properties": {"id_airwork": job, "url_airwork": url or f"{SITE}/{job}/",
                                    I.PROPERTY: cur}}


def test_変わった求人だけ書き_同じ値と読めないページは書かない():
    hs = _HS([_rec("1", "11"),                                       # 新しく画像あり
              _rec("2", "22", cur="https://cdn.arwrk.net/images/rct/b.jpg"),  # 同じ
              _rec("3", "33"),                                       # 写真なし
              _rec("4", "44")])                                      # ページ無し (404)
    ses = _Session({f"{SITE}/11/": (200, _page("11", ["a", "a2"])),
                    f"{SITE}/22/": (200, _page("22", ["b"])),
                    f"{SITE}/33/": (200, _page("33", empty_nuxt=True))})
    st = I.update_company_images("L", "t", hubspot=hs, session=ses, sleep=lambda s: None)
    assert hs.written == [
        {"id": "1", "properties": {I.PROPERTY: "https://cdn.arwrk.net/images/rct/a.jpg\n"
                                               "https://cdn.arwrk.net/images/rct/a2.jpg"}},
        {"id": "3", "properties": {I.PROPERTY: I.NO_IMAGES}}]
    assert st == {"targets": 4, "written": 2, "same": 1, "no_site": 0, "page_failed": 1,
                  "page_failed_reasons": {"public_page_unavailable": 1},
                  "write_failed": 0}


def test_url_airwork_が無ければ巡回で取れた採用サイトを使う():
    hs = _HS([_rec("1", "11", url="")])
    ses = _Session({f"{SITE}/11/": (200, _page("11", ["a"]))})
    st = I.update_company_images("L", "t", recruit_url=SITE + "/", hubspot=hs, session=ses,
                                 sleep=lambda s: None)
    assert st["written"] == 1 and ses.got == [f"{SITE}/11/"]


def test_公開ページは1秒に1件まで():
    waits, t = [], [0.0]
    hs = _HS([_rec(str(i), str(10 + i)) for i in range(3)])
    ses = _Session({f"{SITE}/{10 + i}/": (200, _page(str(10 + i), ["x"])) for i in range(3)})
    I.update_company_images("L", "t", hubspot=hs, session=ses,
                            sleep=lambda s: waits.append(s), clock=lambda: t[0])
    assert waits == [1.0, 1.0]


def test_cdn以外の画像は拒否する():
    url = f"{SITE}/11/"
    bad = _page("11", []).replace(b'<div class="slider">',
                                  b'<div class="slider"><img src="https://evil.example/x.jpg">')
    try:
        I.parse_gallery(bad, url)
        assert False
    except I.CaptureError as e:
        assert e.code == "image_url_denied"


def test_巡回の最後に画像の件数と読めなかった理由を合計する():
    from scripts.job_application_sync.fetchers import aw_orchestrator as orch
    tot = orch._images_total([
        {"images": {"targets": 3, "written": 1, "same": 1, "page_failed": 1,
                    "page_failed_reasons": {"public_page_unavailable": 1}}},
        {"images": {"targets": 2, "written": 0, "same": 0, "page_failed": 2,
                    "page_failed_reasons": {"public_page_unavailable": 1,
                                            "gallery_layout_unrecognized": 1}}},
        {"images": {"error": "RuntimeError"}},
        {"status": "error"}])
    assert tot == {"targets": 5, "written": 1, "same": 1, "page_failed": 3,
                   "page_failed_reasons": {"public_page_unavailable": 2,
                                           "gallery_layout_unrecognized": 1}}
