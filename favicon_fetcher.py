"""Favicon fetching + best-candidate selection.

Implements the same algorithm as `tests/test_favicons_best.py`, adapted for the
extension runtime:
- Only returns the single best icon per domain.
- Ranking: prefer SVG, then larger estimated resolution, then byte size.
- Exception: skip monochrome (all-black or all-white) SVG in favor of next best.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


# ------------------------ HTTP helpers ------------------------

def _request(url: str, accept: str = "image/*,*/*;q=0.8") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
        },
    )


def fetch_url_bytes(url: str, timeout: float = 10.0) -> Tuple[Optional[bytes], Dict[str, str], Optional[str]]:
    """Fetch URL and return (bytes|None, headers(lower-case dict), final_url|None)."""
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
            data = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            final_url = getattr(resp, "url", None)
            if not data:
                return None, headers, final_url
            return data, headers, final_url
    except Exception:
        return None, {}, None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------ Parsing, dimensions, scoring ------------------------

@dataclass(frozen=True)
class IconCandidate:
    domain: str
    method: str
    url: str
    content: bytes
    mime: str
    ext: str
    is_svg: bool
    width: Optional[int]
    height: Optional[int]
    bytes_len: int
    sha256: str

    @property
    def max_side(self) -> int:
        return max(self.width or 0, self.height or 0)


def _guess_ext(mime: str, url: str) -> str:
    mime = (mime or "").lower().split(";")[0].strip()
    path = urlparse(url).path.lower()
    if path.endswith(".svg") or mime in {"image/svg+xml", "image/svg"}:
        return "svg"
    if path.endswith(".png") or mime == "image/png":
        return "png"
    if path.endswith(".ico") or mime in {"image/x-icon", "image/vnd.microsoft.icon"}:
        return "ico"
    if path.endswith(".jpg") or path.endswith(".jpeg") or mime == "image/jpeg":
        return "jpg"
    if path.endswith(".webp") or mime == "image/webp":
        return "webp"
    return "bin"


def _sniff_mime_from_bytes(data: bytes) -> Optional[str]:
    lower = data[:512].lower()
    if data.startswith(b"<svg") or b"<svg" in lower:
        return "image/svg+xml"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    return None


def _parse_png_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None, None
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    except Exception:
        return None, None


def _parse_ico_biggest_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Return largest (w,h) from ICO directory entries (no image decoding)."""
    try:
        if len(data) < 6:
            return None, None
        if data[2:4] != b"\x01\x00":  # type=1
            return None, None
        count = int.from_bytes(data[4:6], "little")
        if count <= 0:
            return None, None
        max_w = 0
        max_h = 0
        offset = 6
        for _ in range(count):
            if offset + 16 > len(data):
                break
            w = data[offset]
            h = data[offset + 1]
            w = 256 if w == 0 else w
            h = 256 if h == 0 else h
            if w * h > max_w * max_h:
                max_w = w
                max_h = h
            offset += 16
        if max_w == 0 or max_h == 0:
            return None, None
        return max_w, max_h
    except Exception:
        return None, None


def determine_dimensions(mime: str, ext: str, data: bytes) -> Tuple[Optional[int], Optional[int]]:
    if ext == "png" or mime == "image/png":
        return _parse_png_size(data)
    if ext == "ico" or mime in {"image/x-icon", "image/vnd.microsoft.icon"}:
        return _parse_ico_biggest_size(data)
    return None, None


def quality_key(c: IconCandidate) -> Tuple[int, int, int]:
    return (1 if c.is_svg else 0, c.max_side, c.bytes_len)


def _extract_svg_color_tokens(svg_text: str) -> List[str]:
    """Extract a conservative set of tokens to detect monochrome SVGs."""
    s = svg_text.lower()
    tokens: List[str] = []

    tokens += re.findall(r"#[0-9a-f]{3,8}", s)
    tokens += re.findall(r"rgba?\([^\)]*\)", s)
    if "black" in s:
        tokens.append("black")
    if "white" in s:
        tokens.append("white")

    return tokens


def _is_svg_monochrome_black_or_white(svg_bytes: bytes) -> bool:
    try:
        text = svg_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return False

    tokens = _extract_svg_color_tokens(text)
    if not tokens:
        return False

    has_black = False
    has_white = False
    has_other = False

    for t in tokens:
        tt = t.strip()
        if tt in {"black", "#000", "#000000", "#000000ff", "#000f", "#00000000"}:
            has_black = True
            continue
        if tt in {"white", "#fff", "#ffffff", "#ffffffff", "#ffff", "#ffffff00"}:
            has_white = True
            continue

        if tt.startswith("#"):
            h = tt[1:]
            if len(h) in {3, 4}:
                r = int(h[0] * 2, 16)
                g = int(h[1] * 2, 16)
                b = int(h[2] * 2, 16)
            elif len(h) in {6, 8}:
                r = int(h[0:2], 16)
                g = int(h[2:4], 16)
                b = int(h[4:6], 16)
            else:
                has_other = True
                continue

            mx = max(r, g, b)
            mn = min(r, g, b)
            if mx <= 16 and mn <= 16:
                has_black = True
            elif mx >= 239 and mn >= 239:
                has_white = True
            else:
                has_other = True
            continue

        if tt.startswith("rgb"):
            has_other = True
            continue

        has_other = True

    if has_other:
        return False
    return (has_black and not has_white) or (has_white and not has_black)


# ------------------------ Methods (fetchers) ------------------------

class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.icons: List[str] = []
        self.manifest: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "link":
            return

        d = {k.lower(): (v or "") for k, v in attrs}
        rel = d.get("rel", "").lower()
        href = d.get("href")
        if not href:
            return

        if "manifest" in rel:
            self.manifest = href
        if "icon" in rel:
            self.icons.append(href)


def _root_fallback(domain: str) -> Optional[str]:
    parts = domain.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return None


def fetch_duckduckgo(domain: str) -> List[Tuple[str, bytes, str]]:
    urls = [f"https://icons.duckduckgo.com/ip3/{domain}.ico"]
    rd = _root_fallback(domain)
    if rd:
        urls.append(f"https://icons.duckduckgo.com/ip3/{rd}.ico")

    out: List[Tuple[str, bytes, str]] = []
    for u in urls:
        data, headers, final_url = fetch_url_bytes(u, timeout=8)
        if data:
            mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
            out.append((final_url or u, data, mime))
    return out


def fetch_google_s2(domain: str) -> List[Tuple[str, bytes, str]]:
    sizes = [16, 32, 64, 128, 256]
    out: List[Tuple[str, bytes, str]] = []
    for sz in sizes:
        u = f"https://www.google.com/s2/favicons?domain={domain}&sz={sz}"
        data, headers, final_url = fetch_url_bytes(u, timeout=8)
        if data:
            mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
            out.append((final_url or u, data, mime))

    rd = _root_fallback(domain)
    if rd and rd != domain:
        for sz in sizes:
            u = f"https://www.google.com/s2/favicons?domain={rd}&sz={sz}"
            data, headers, final_url = fetch_url_bytes(u, timeout=8)
            if data:
                mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
                out.append((final_url or u, data, mime))

    return out


def fetch_google_v2(domain: str) -> List[Tuple[str, bytes, str]]:
    sizes = [32, 64, 128, 256]
    out: List[Tuple[str, bytes, str]] = []
    for sz in sizes:
        u = (
            "https://t2.gstatic.com/faviconV2"
            "?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL"
            f"&url=https://{domain}&size={sz}"
        )
        data, headers, final_url = fetch_url_bytes(u, timeout=8)
        if data:
            mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
            out.append((final_url or u, data, mime))
    return out


def fetch_direct(domain: str) -> List[Tuple[str, bytes, str]]:
    urls = [f"https://{domain}/favicon.ico", f"https://{domain}/favicon.png"]
    rd = _root_fallback(domain)
    if rd and rd != domain:
        urls += [f"https://{rd}/favicon.ico", f"https://{rd}/favicon.png"]

    out: List[Tuple[str, bytes, str]] = []
    for u in urls:
        data, headers, final_url = fetch_url_bytes(u, timeout=8)
        if data:
            mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
            if (mime or "").startswith("image/"):
                out.append((final_url or u, data, mime))
    return out


def fetch_html_icons(domain: str) -> List[Tuple[str, bytes, str]]:
    base = f"https://{domain}"
    html_bytes, _, base_final = fetch_url_bytes(base, timeout=10)
    if not html_bytes:
        rd = _root_fallback(domain)
        if rd and rd != domain:
            return fetch_html_icons(rd)
        return []

    html = html_bytes.decode("utf-8", errors="ignore")
    parser = LinkParser()
    parser.feed(html)

    icons = list(dict.fromkeys(parser.icons))

    out: List[Tuple[str, bytes, str]] = []
    for href in icons:
        icon_url = urljoin(base_final or base, href)
        data, headers, final_url = fetch_url_bytes(icon_url, timeout=10)
        if not data:
            continue
        mime = headers.get("content-type") or _sniff_mime_from_bytes(data) or "application/octet-stream"
        if (mime or "").startswith("image/"):
            out.append((final_url or icon_url, data, mime))

    return out


def fetch_manifest_icons(domain: str) -> List[Tuple[str, bytes, str]]:
    base = f"https://{domain}"
    html_bytes, _, base_final = fetch_url_bytes(base, timeout=10)
    if not html_bytes:
        rd = _root_fallback(domain)
        if rd and rd != domain:
            return fetch_manifest_icons(rd)
        return []

    html = html_bytes.decode("utf-8", errors="ignore")
    parser = LinkParser()
    parser.feed(html)

    if not parser.manifest:
        return []

    manifest_url = urljoin(base_final or base, parser.manifest)
    mbytes, _, mfinal = fetch_url_bytes(manifest_url, timeout=10)
    if not mbytes:
        return []

    try:
        data = json.loads(mbytes.decode("utf-8", errors="ignore"))
    except Exception:
        return []

    icons = data.get("icons")
    if not isinstance(icons, list) or not icons:
        return []

    out: List[Tuple[str, bytes, str]] = []
    for icon in icons:
        if not isinstance(icon, dict):
            continue
        src = icon.get("src")
        if not src:
            continue
        icon_url = urljoin(mfinal or manifest_url, src)
        ibytes, headers, ifinal = fetch_url_bytes(icon_url, timeout=10)
        if not ibytes:
            continue
        mime = headers.get("content-type") or _sniff_mime_from_bytes(ibytes) or "application/octet-stream"
        if (mime or "").startswith("image/"):
            out.append((ifinal or icon_url, ibytes, mime))

    return out


METHODS: List[Tuple[str, Callable[[str], List[Tuple[str, bytes, str]]]]] = [
    ("duckduckgo", fetch_duckduckgo),
    ("google_s2", fetch_google_s2),
    ("google_v2", fetch_google_v2),
    ("direct", fetch_direct),
    ("html", fetch_html_icons),
    ("manifest", fetch_manifest_icons),
]


# ------------------------ Orchestration ------------------------

def build_candidates(domain: str, method: str, triples: Iterable[Tuple[str, bytes, str]]) -> List[IconCandidate]:
    out: List[IconCandidate] = []
    for url, content, mime in triples:
        sniffed = _sniff_mime_from_bytes(content)
        mime2 = (mime or "").lower().split(";")[0].strip() or (sniffed or "application/octet-stream")
        ext = _guess_ext(mime2, url)
        is_svg = ext == "svg" or mime2.startswith("image/svg")
        w, h = determine_dimensions(mime2, ext, content)
        out.append(
            IconCandidate(
                domain=domain,
                method=method,
                url=url,
                content=content,
                mime=mime2,
                ext=ext,
                is_svg=is_svg,
                width=w,
                height=h,
                bytes_len=len(content),
                sha256=_sha256_bytes(content),
            )
        )
    return out


def pick_best_with_exception(domain: str, candidates: List[IconCandidate]) -> Tuple[Optional[IconCandidate], Optional[str]]:
    if not candidates:
        return None, None

    ordered = sorted(candidates, key=quality_key, reverse=True)
    best = ordered[0]

    if best.is_svg and _is_svg_monochrome_black_or_white(best.content):
        for nxt in ordered[1:]:
            if nxt.sha256 != best.sha256:
                msg = (
                    f"Monochrome SVG skipped for {domain}: method={best.method}, url={best.url} "
                    f"-> picked {nxt.method} ({nxt.ext})"
                )
                return nxt, msg
        msg = f"Monochrome SVG detected for {domain} but no alternative candidate was available."
        return best, msg

    return best, None


def fetch_best_favicon(domain: str) -> Tuple[Optional[IconCandidate], Optional[str]]:
    """Fetch all candidates for a domain and pick the best.

    Returns (best_candidate|None, exception_message|None).
    """
    all_candidates: List[IconCandidate] = []
    for method, fn in METHODS:
        triples = fn(domain)
        all_candidates.extend(build_candidates(domain, method, triples))

    return pick_best_with_exception(domain, all_candidates)
