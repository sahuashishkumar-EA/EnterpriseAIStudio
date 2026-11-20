"""Web scanning for functional contexts: fetch pages from the URLs a functional expert listed,
extract readable text, and hand them to the knowledge store (pgvector).

Two modes per source:
  * page - the page and a few pages it links to (breadth first, max 25)
  * site - a smart crawl of the whole site (max 200): robots.txt is respected, the sitemap is read
    first, every candidate URL is ranked by relevance to the context (its role, goal, topics and
    starters, sent as `hints`) using the URL and the link text, include/exclude patterns narrow
    the crawl, and pages are fetched a few at a time.

Safety (the runtime sits inside the VPC):
  * http(s) only, standard ports, no credentials in URLs
  * every hostname (including redirect targets) must resolve to public IPs only -
    blocks localhost, private ranges, link-local and the metadata server (SSRF)
  * page count, size and time limits; HTML / plain text only
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

MAX_BYTES = 2_000_000
MAX_PAGES_TOTAL = 300
SITEMAP_MAX_URLS = 5000
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|ico|zip|gz|mp4|mp3|wav|css|js|json|xml|rss|woff2?)(\?|$)", re.I)
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_)", re.I)
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote"}


class ScanError(Exception):
    pass


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self.anchors: dict[str, str] = {}  # href -> link text
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._atext: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
                self._href, self._atext = href, []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href:
            self.anchors[self._href] = (self.anchors.get(self._href, "") + " " + " ".join(self._atext)).strip()[:200]
            self._href = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._atext.append(data.strip())
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n\n", t)
        return t.strip()


def extract(html: str) -> tuple[str, str, list[str]]:
    p = _Extractor()
    p.feed(html)
    return p.title.strip(), p.text(), p.links


def extract_full(html: str) -> tuple[str, str, dict[str, str]]:
    """(title, text, {href: link text})"""
    p = _Extractor()
    p.feed(html)
    links = {h: p.anchors.get(h, "") for h in p.links}
    return p.title.strip(), p.text(), links


WORD = re.compile(r"[a-zA-Z\u00C0-\u024F]{3,}")
STOP = {"the", "and", "for", "you", "your", "with", "our", "are", "this", "that", "from", "about", "what", "how",
        "can", "who", "will", "all", "not", "have", "has", "use", "via", "www", "http", "https", "html", "htm",
        "php", "aspx", "index", "page", "home", "een", "het", "van", "voor", "met", "der", "die", "und"}


def keywords(hints: list[str]) -> set[str]:
    return {w.lower() for h in hints for w in WORD.findall(h) if w.lower() not in STOP}


def relevance(url: str, link_text: str, kw: set[str]) -> float:
    """How promising a link is for this context: keyword hits in the URL path and the link text,
    a little preference for shallow pages."""
    path = urlparse(url).path.lower()
    words = set(WORD.findall(path.replace("-", " ").replace("_", " ").replace("/", " "))) | \
        {w.lower() for w in WORD.findall(link_text)}
    hits = len({w for w in words if w in kw or any(w.startswith(k[:6]) for k in kw if len(k) >= 6)})
    depth = path.strip("/").count("/")
    return hits * 2.0 + (1.0 / (1 + depth))


def matches(url: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch

    return any((p in url) if not any(ch in p for ch in "*?[") else fnmatch(url, p) for p in patterns)


def normalize(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode

    u = urlparse(urldefrag(url)[0])
    q = urlencode([(k, v) for k, v in parse_qsl(u.query) if not TRACKING.match(k)])
    return u._replace(query=q).geturl()


def robots_disallows(text: str) -> list[str]:
    """Disallow rules for User-agent: * (simple prefix rules)."""
    rules, applies = [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if k.lower() == "user-agent":
            applies = v == "*"
        elif k.lower() == "disallow" and applies and v:
            rules.append(v)
    return rules


def _is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or (a.version == 6 and a.ipv4_mapped and not _is_public(str(a.ipv4_mapped))))


async def check_public(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ScanError(f"unsupported scheme in {url}")
    if u.username or u.password:
        raise ScanError("credentials in URLs are not allowed")
    if u.port not in (None, 80, 443):
        raise ScanError(f"port {u.port} not allowed")
    host = u.hostname or ""
    if host in ("localhost", "metadata.google.internal") or host.endswith(".internal"):
        raise ScanError(f"host {host} not allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ScanError(f"cannot resolve {host}") from e
    for info in infos:
        if not _is_public(info[4][0]):
            raise ScanError(f"host {host} resolves to a non-public address")


class WebScanner:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, ssrf_protection: bool = True):
        self.client = httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False,
                                        headers={"User-Agent": "eap-knowledge-scanner/0.1"})
        self.ssrf = ssrf_protection

    async def fetch(self, url: str) -> tuple[str, str]:
        """Returns (final_url, body) for HTML/plain-text pages."""
        for _ in range(4):  # follow up to 3 redirects, re-checking each hop
            if self.ssrf:
                await check_public(url)
            async with self.client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ScanError(f"HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "")
                if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
                    raise ScanError(f"unsupported content type {ctype or 'unknown'}")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return url, body.decode(r.encoding or "utf-8", errors="replace")
        raise ScanError("too many redirects")

    async def _text(self, url: str) -> str | None:
        try:
            if self.ssrf:
                await check_public(url)
            r = await self.client.get(url)
            if r.status_code >= 400 or len(r.content) > MAX_BYTES:
                return None
            return r.text
        except (ScanError, httpx.HTTPError):
            return None

    async def sitemap_urls(self, origin: str, robots: str | None) -> list[str]:
        maps = [line.split(":", 1)[1].strip() for line in (robots or "").splitlines()
                if line.lower().startswith("sitemap:")] or [origin + "/sitemap.xml"]
        out: list[str] = []
        seen: set[str] = set()
        while maps and len(out) < SITEMAP_MAX_URLS and len(seen) < 20:
            m = maps.pop(0)
            if m in seen:
                continue
            seen.add(m)
            body = await self._text(m)
            if not body or "<" not in body:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            if "<sitemapindex" in body:
                maps += locs[:20]
            else:
                out += locs
        return out[:SITEMAP_MAX_URLS]

    async def crawl_site(self, src: dict[str, Any], kw: set[str], seen: set[str], room: int,
                         docs: list[dict[str, Any]], report: list[dict[str, Any]]) -> None:
        import heapq

        start = normalize(src["url"])
        u = urlparse(start)
        origin, host = f"{u.scheme}://{u.netloc}", u.hostname
        limit = min(int(src.get("max_pages", 50)), 200, room)
        include, exclude = src.get("include") or [], src.get("exclude") or []
        robots = await self._text(origin + "/robots.txt")
        disallow = robots_disallows(robots or "")

        def allowed(url: str) -> bool:
            pu = urlparse(url)
            if pu.scheme not in ("http", "https") or SKIP_EXT.search(pu.path):
                return False
            if src.get("same_domain_only", True) and pu.hostname != host:
                return False
            if any(pu.path.startswith(d) for d in disallow):
                return False
            if exclude and matches(url, exclude):
                return False
            return url == start or not include or matches(url, include)

        heap: list[tuple[float, int, str]] = []
        counter = 0

        def push(url: str, text: str = "", bonus: float = 0.0) -> None:
            nonlocal counter
            url = normalize(url)
            if url in seen or not allowed(url):
                return
            counter += 1
            heapq.heappush(heap, (-(relevance(url, text, kw) + bonus), counter, url))

        push(start, bonus=100)  # the start page first
        sm = await self.sitemap_urls(origin, robots)
        for x in sm:
            push(x, bonus=0.5)  # listed in the sitemap: a real page
        report.append({"url": origin, "ok": True, "info": f"site mode: {len(sm)} sitemap URL(s), "
                                                           f"{len(disallow)} robots rule(s), keywords: {', '.join(sorted(kw)[:12])}"})
        fetched = 0
        while heap and fetched < limit:
            batch = []
            size = 1 if fetched == 0 else min(4, limit - fetched)  # start page alone: its links steer the rest
            while heap and len(batch) < size:
                _, _, url = heapq.heappop(heap)
                if url not in seen:
                    seen.add(url)
                    batch.append(url)
            results = await asyncio.gather(*(self._page(url) for url in batch))
            for url, res in zip(batch, results):
                if isinstance(res, str):
                    report.append({"url": url, "ok": False, "error": res})
                    continue
                final, title, text, links = res
                fetched += 1
                seen.add(normalize(final))
                if len(text) >= 80:
                    docs.append({"id": final, "title": title or final, "uri": final, "text": text[:200_000]})
                report.append({"url": final, "ok": True, "title": title, "chars": len(text)})
                for href, anchor in links.items():
                    push(urljoin(final, href), anchor)

    async def _page(self, url: str) -> tuple[str, str, str, dict[str, str]] | str:
        try:
            final, body = await self.fetch(url)
        except (ScanError, httpx.HTTPError) as e:
            return str(e)[:200]
        if "<" in body[:2000] and "html" in body[:2000].lower():
            title, text, links = extract_full(body)
        else:
            title, text, links = "", body, {}
        return final, title, text, links

    async def scan(self, sources: list[dict[str, Any]], hints: list[str] | None = None
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        docs: list[dict[str, Any]] = []
        report: list[dict[str, Any]] = []
        seen: set[str] = set()
        kw = keywords(hints or [])
        for src in sources:
            if src.get("mode") == "site":
                await self.crawl_site(src, kw, seen, MAX_PAGES_TOTAL - len(docs), docs, report)
                continue
            start = urldefrag(src["url"])[0]
            limit = min(int(src.get("max_pages", 5)), 25)
            if len(docs) >= MAX_PAGES_TOTAL:
                break
            same = src.get("same_domain_only", True)
            host = urlparse(start).hostname
            queue, fetched = [start], 0
            while queue and fetched < limit and len(docs) < MAX_PAGES_TOTAL:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                try:
                    final, body = await self.fetch(url)
                except (ScanError, httpx.HTTPError) as e:
                    report.append({"url": url, "ok": False, "error": str(e)[:200]})
                    continue
                fetched += 1
                if "<" in body[:2000] and "html" in body[:2000].lower():
                    title, text, links = extract(body)
                else:
                    title, text, links = "", body, []
                if len(text) >= 80:
                    docs.append({"id": final, "title": title or final, "uri": final, "text": text[:200_000]})
                report.append({"url": final, "ok": True, "title": title, "chars": len(text)})
                for href in links:
                    nxt = urldefrag(urljoin(final, href))[0]
                    if urlparse(nxt).scheme not in ("http", "https"):
                        continue
                    if same and urlparse(nxt).hostname != host:
                        continue
                    if re.search(r"\.(pdf|jpe?g|png|gif|svg|zip|mp4|mp3|css|js)$", nxt, re.I):
                        continue
                    if nxt not in seen and nxt not in queue:
                        queue.append(nxt)
        return docs, report
