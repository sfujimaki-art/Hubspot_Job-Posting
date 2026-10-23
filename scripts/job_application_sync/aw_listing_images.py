"""求人の巡回と一緒に、AirWork の公開ページから求人票の画像の URL を HubSpot に書く (2026-10-16)。

なぜ (ユーザー指示「画像は、というより求人と一緒にが正しい」):
  画像の URL は HR_HR の scripts/job_copy_airwork_image_urls.py を手で 1 回流しただけで
  (10/12)、変わった画像・新しい求人は反映されていなかった。社の求人を取り込んだ直後に、
  その社の「公開中」の求人だけ公開ページを読んで、値が変わったものだけ書く。

公開ページの読み方・画像の判定・書く値は HR_HR の job_copy_airwork_images.py /
job_copy_airwork_image_urls.py と同じ (そこから必要な部分だけ写した)。
  - 公開ページ https://arwrk.net/recruit/<slug>/<求人ID>/ を 1 秒に 1 件まで読む。ログインしない。
  - 画像は cdn.arwrk.net/images/rct/ の URL だけ。画像そのものは取得しない。
  - 写真の無い求人は NO_IMAGES、ページが無い (404/410) か読めない求人は書かない。
  - 値が同じなら書かない。
"""
from __future__ import annotations

import json
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

HUBSPOT = "https://api.hubapi.com"
LISTINGS = "0-420"
PROPERTY = "aw_kyuujinhyou_gazou"
NO_IMAGES = "NO_IMAGES"
PAGE_INTERVAL = float(os.environ.get("JAS_AW_IMAGE_PAGE_INTERVAL_SEC", "1.0"))
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 100


class CaptureError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def own_site(url, media_id):
    """求人の url_airwork から採用サイト (https://arwrk.net/recruit/<slug>)。形が違えば None。"""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    parts = urlsplit(url)
    m = re.fullmatch(r"/recruit/([A-Za-z0-9_-]+)/([0-9]{1,30})/?", parts.path)
    if (parts.scheme != "https" or parts.netloc != "arwrk.net" or parts.query or parts.fragment
            or not m or m.group(2) != media_id):
        return None
    return "https://arwrk.net/recruit/" + m.group(1)


def site_from_recruit_url(url):
    """巡回で取れた採用サイトの URL (https://arwrk.net/recruit/<slug>/...) から採用サイト。"""
    if not isinstance(url, str):
        return None
    m = re.match(r"https://arwrk\.net/recruit/([A-Za-z0-9_-]+)(/|$)", url.strip())
    return "https://arwrk.net/recruit/" + m.group(1) if m else None


def job_url(site, job):
    if not re.fullmatch(r"[0-9]{1,30}", str(job or "")):
        raise CaptureError("invalid_record_id")
    return site.rstrip("/") + "/" + str(job) + "/"


class Gallery(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.canonicals, self.images = [], [], []
        self.has_gallery = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        ancestry = [frame[1] for frame in self.stack] + [classes]
        in_gallery = any("job-img" in parent and any("slider" in child for child in ancestry[i + 1:])
                         for i, parent in enumerate(ancestry))
        self.has_gallery |= in_gallery
        if tag == "link" and "canonical" in (attributes.get("rel") or "").split():
            self.canonicals.append(attributes.get("href", ""))
        if tag == "img" and in_gallery and not any("is-clone" in c for c in ancestry):
            self.images.append(attributes.get("src") or attributes.get("data-src") or "")
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                       "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def image_url(value, page_url):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise CaptureError("image_url_denied")
    resolved = urljoin(page_url, value)
    parsed = urlsplit(resolved)
    if (parsed.scheme != "https" or parsed.netloc != "cdn.arwrk.net" or parsed.fragment
            or not re.fullmatch(r"/images/rct/[A-Za-z0-9_./-]+", parsed.path)
            or ".." in parsed.path.split("/") or "\\" in value):
        raise CaptureError("image_url_denied")
    return resolved


def _split_arguments(text):
    parts, depth, start, quote, escaped = [], 0, 0, None, False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def nuxt_images_empty(html_text):
    """埋め込みの求人データが「画像 0 件」とはっきり書いているときだけ True。"""
    pos = html_text.find("window.__NUXT__")
    if pos < 0:
        return False
    end = html_text.find("</script>", pos)
    script = html_text[pos:end if end >= 0 else len(html_text)]
    found = re.search(r"jobOverViewSection:\{.*?\bimages:(\[[^\]]*\]|[A-Za-z_$][\w$]*)", script, re.S)
    if not found:
        return False
    value = found.group(1)
    if value.startswith("["):
        return value.replace(" ", "") == "[]"
    function = re.match(r"window\.__NUXT__=\(function\(([^)]*)\)\{", script)
    call = re.search(r"\}\((.*)\)\);?\s*$", script, re.S)
    if not function or not call:
        return False
    names = [n.strip() for n in function.group(1).split(",")]
    values = _split_arguments(call.group(1))
    return value in names and len(names) == len(values) and values[names.index(value)].replace(" ", "") == "[]"


def next_images_empty(html_text):
    """Next.js 版のページで、埋め込みの求人データが「画像 0 件」とはっきり書いているときだけ True。

    ★2026-10-23: 公開ページの一部が Nuxt (window.__NUXT__) から Next.js (__NEXT_DATA__ の JSON) に
      変わっていた。画像の無い求人をこの形で読めず「画像の並び方を読み取れない」と数えていた
      (10/23 朝の巡回で 1 件だけの社の公開ページを確かめた: jobOverViewSection.images が [])。
    """
    m = re.search(r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return False
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return False
    found = []

    def walk(v):
        if isinstance(v, dict):
            sec = v.get("jobOverViewSection")
            if isinstance(sec, dict) and "images" in sec:
                found.append(sec["images"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return bool(found) and all(x == [] for x in found)


def parse_gallery(html, page_url):
    gallery = Gallery()
    try:
        html_text = html.decode("utf-8", errors="strict")
        gallery.feed(html_text)
    except UnicodeError:
        raise CaptureError("invalid_page_encoding") from None
    if gallery.canonicals != [page_url]:
        raise CaptureError("page_identity_mismatch")
    if not gallery.has_gallery:
        if nuxt_images_empty(html_text) or next_images_empty(html_text):
            return []
        raise CaptureError("gallery_layout_unrecognized")
    if len(gallery.images) > MAX_IMAGES:
        raise CaptureError("gallery_image_limit")
    return [image_url(src, page_url) for src in gallery.images]


def get_page(session, url):
    try:
        with session.get(url, stream=True, allow_redirects=False, timeout=(10, 30)) as r:
            if r.status_code in (404, 410):
                raise CaptureError("public_page_unavailable")
            if 300 <= r.status_code < 400:
                raise CaptureError("redirect_denied")
            if r.status_code != 200:
                raise CaptureError("upstream_http_failure")
            mime = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if mime not in ("text/html", "application/xhtml+xml"):
                raise CaptureError("invalid_page_mime")
            raw = bytearray()
            for chunk in r.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_PAGE_BYTES:
                    raise CaptureError("response_size_limit")
            return bytes(raw)
    except requests.RequestException:
        raise CaptureError("upstream_transport_failure") from None


def clean_urls(urls):
    """画像の URL だけ (?thumb などは外す)。順番は保つ。"""
    return [urlsplit(u)._replace(query="", fragment="").geturl() for u in urls]


def field_value(urls):
    return "\n".join(urls) if urls else NO_IMAGES


def same_value(current, new):
    lines = [x.strip() for x in (current or "").replace("\r\n", "\n").split("\n") if x.strip()]
    return lines == new.split("\n")


class HubSpot:
    """この処理で使う HubSpot の読み書き (1 秒に 2 回まで・429 は待ってやり直す)。"""

    def __init__(self, token, session=None, sleep=time.sleep, clock=time.monotonic, interval=0.5):
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        self.sleep, self.clock, self.interval, self.last = sleep, clock, interval, None

    def call(self, method, path, body=None, attempts=6):
        for attempt in range(attempts):
            if self.last is not None:
                wait = self.interval - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            r = self.session.request(method, HUBSPOT + path, headers=self.headers, json=body, timeout=60)
            if r.status_code == 429:
                try:
                    delay = float(r.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                self.sleep(max(delay, 1.0))
                continue
            if r.status_code >= 500 and attempt < attempts - 1:
                self.sleep(2.0)
                continue
            return r
        raise RuntimeError("hubspot_rate_limited")

    def open_listings(self, login_id):
        """この社の「公開中」の AirWork の求人 (id, id_airwork, url_airwork, 画像の現在値)。"""
        out, last = [], 0
        while True:
            body = {"filterGroups": [{"filters": [
                {"propertyName": "airwork_account_login_id", "operator": "EQ", "value": login_id},
                {"propertyName": "kyuujin_status", "operator": "EQ", "value": "公開中"},
                {"propertyName": "id_airwork", "operator": "HAS_PROPERTY"},
                {"propertyName": "hs_object_id", "operator": "GT", "value": str(last)}]}],
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                "properties": ["id_airwork", "url_airwork", PROPERTY], "limit": 100}
            r = self.call("POST", f"/crm/v3/objects/{LISTINGS}/search", body)
            if r.status_code != 200:
                raise RuntimeError(f"hubspot_search_failed_{r.status_code}")
            res = r.json().get("results", [])
            out.extend(res)
            if len(res) < 100:
                return out
            last = int(res[-1]["id"])

    def batch_update(self, inputs):
        r = self.call("POST", f"/crm/v3/objects/{LISTINGS}/batch/update", {"inputs": inputs})
        return r.status_code in (200, 201, 204, 207) and not (
            r.status_code == 207 and r.json().get("errors"))


def update_company_images(login_id, token, recruit_url="", hubspot=None, session=None,
                          sleep=time.sleep, clock=time.monotonic, dry_run=False) -> dict:
    """1 社分。返り値は件数だけ (社名・URL・ID は入れない)。"""
    stats = {"targets": 0, "written": 0, "same": 0, "no_site": 0, "page_failed": 0,
             "write_failed": 0}
    hubspot = hubspot or HubSpot(token)
    session = session or requests.Session()
    fallback = site_from_recruit_url(recruit_url)
    pending, last_page = [], None
    for rec in hubspot.open_listings(login_id):
        props = rec.get("properties") or {}
        stats["targets"] += 1
        media = (props.get("id_airwork") or "").strip()
        site = own_site(props.get("url_airwork"), media) or fallback
        if not site:
            stats["no_site"] += 1
            continue
        if last_page is not None:
            wait = PAGE_INTERVAL - (clock() - last_page)
            if wait > 0:
                sleep(wait)
        last_page = clock()
        try:
            url = job_url(site, media)
            value = field_value(clean_urls(parse_gallery(get_page(session, url), url)))
        except CaptureError as e:
            # ★理由ごとにも数える (2026-10-19)。10/18 の夜は 1,411 件中 187 件が読めず、
            #   公開を終えた求人か、画面の作りが変わったのかを見分けられなかった。
            stats["page_failed"] += 1
            why = stats.setdefault("page_failed_reasons", {})
            why[str(e)] = why.get(str(e), 0) + 1
            continue
        if same_value(props.get(PROPERTY), value):
            stats["same"] += 1
            continue
        pending.append({"id": rec["id"], "properties": {PROPERTY: value}})
        if len(pending) >= 100:
            _flush(hubspot, pending, stats, dry_run)
    _flush(hubspot, pending, stats, dry_run)
    return stats


def _flush(hubspot, pending, stats, dry_run):
    if not pending:
        return
    if dry_run or hubspot.batch_update(list(pending)):
        stats["written"] += len(pending)
    else:
        stats["write_failed"] += len(pending)
    pending.clear()
