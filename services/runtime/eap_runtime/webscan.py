"""Web scanning for functional contexts: fetch a few pages from the URLs a functional expert
listed, extract readable text, and hand them to the knowledge store (pgvector).

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
MAX_PAGES_TOTAL = 50
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote"}


class ScanError(Exception):
    pass


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
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

    async def scan(self, sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        docs: list[dict[str, Any]] = []
        report: list[dict[str, Any]] = []
        seen: set[str] = set()
        for src in sources:
            start = urldefrag(src["url"])[0]
            limit = min(int(src.get("max_pages", 5)), 25)
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
