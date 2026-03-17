"""Best-favicon picker.

Based on `tests/test_favicons_hires.py`, but:
- Drops IconHorse.
- Only saves the *best* icon per domain (plus marks it).
- Ranking:
  1) Prefer SVG
  2) Otherwise, prefer higher resolution (estimated)
  3) Otherwise, larger byte size as a tiebreaker
- Exception:
  - If the best SVG is monochrome (only black/only white), skip it and pick
    the next-best candidate. The script prints where this happened.

Output:
- `tests/test_favicons_best_output/<domain>/!<method>__...<ext>` (best icon)
- `tests/test_favicons_best_output/best_report.json`

This is a benchmark/utility script and performs many network requests.
Standard library only.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse


# ------------------------ Firefox domain extraction ------------------------

def searchPlaces(firefox_path: str) -> Optional[str]:
    firefox_path = os.path.expanduser(firefox_path)
    if not firefox_path.endswith("/"):
        firefox_path += "/"

    conf_path = os.path.join(firefox_path, "profiles.ini")
    profile = configparser.RawConfigParser()
    profile.read(conf_path)
    if not profile.has_section("Profile0"):
        return None

    prof_path = profile.get("Profile0", "Path")
    sql_path = os.path.join(firefox_path, prof_path)
    return os.path.join(sql_path, "places.sqlite")


def get_all_domains(db_path: str) -> List[str]:
    # Firefox places.sqlite is often locked, so copy it first.
    temp_db_path = tempfile.mktemp()
    shutil.copyfile(db_path, temp_db_path)

    conn = sqlite3.connect(temp_db_path, check_same_thread=False)
    query = "SELECT url FROM moz_bookmarks AS A JOIN moz_places AS B ON(A.fk = B.id)"
    cursor = conn.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()

    domains = set()
    for (url,) in rows:
        if not url:
            continue
        domain = urlparse(url).netloc
        if domain:
            domains.add(domain)

    conn.close()
    try:
        os.remove(temp_db_path)
    except OSError:
        pass

    return sorted(domains)


# ------------------------ HTTP helpers ------------------------

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


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
    if data.startswith(b"<svg") or b"<svg" in data[:512].lower():
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
    """Extract a conservative set of color tokens from an SVG.

    We look for:
    - hex colors (#rgb/#rrggbb/#rrggbbaa)
    - rgb()/rgba()
    - the keywords black/white

    Purpose: detect clearly monochrome black-or-white SVGs.
    """
    import re

    s = svg_text.lower()

    tokens: List[str] = []

    # Hex colors
    tokens += re.findall(r"#[0-9a-f]{3,8}", s)
    # rgb()/rgba()
    tokens += re.findall(r"rgba?\([^\)]*\)", s)
    # common keywords
    if "black" in s:
        tokens.append("black")
    if "white" in s:
        tokens.append("white")

    # Also consider fill/stroke="none" etc as not a color token.
    return tokens


def _is_svg_monochrome_black_or_white(svg_bytes: bytes) -> bool:
    """Return True if the SVG appears to use only black or only white (plus none/transparent).

    This is heuristic (no full XML/CSS parsing), but good enough for the stated exception.
    """
    try:
        text = svg_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return False

    tokens = _extract_svg_color_tokens(text)
    if not tokens:
        # If we can't find any color tokens, don't treat as monochrome.
        return False

    # Normalize tokens to coarse categories.
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

        # Attempt to treat near-black/near-white as their respective class.
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
            # Too messy to parse fully robustly; treat as "other" to avoid false positives.
            has_other = True
            continue

        has_other = True

    # Monochrome if it only has one of black/white and no other colors.
    if has_other:
        return False
    return (has_black and not has_white) or (has_white and not has_black)


def sanitize_domain(domain: str) -> str:
    return domain.replace("/", "_")


def ensure_output_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_favicons_best_output")


def _candidate_filename(c: IconCandidate, best: bool) -> str:
    # include hash to avoid collisions
    prefix = "!" if best else ""
    return f"{prefix}{c.method}__{c.max_side or 0}x{c.max_side or 0}__{c.sha256[:10]}.{c.ext}"


def save_best_candidate(base_dir: str, c: IconCandidate) -> str:
    dom_dir = os.path.join(base_dir, sanitize_domain(c.domain))
    os.makedirs(dom_dir, exist_ok=True)
    path = os.path.join(dom_dir, _candidate_filename(c, best=True))
    with open(path, "wb") as f:
        f.write(c.content)
    return path


# ------------------------ Methods (fetchers) ------------------------

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


METHODS = [
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


def per_domain_job(domain: str) -> Tuple[str, List[IconCandidate]]:
    all_candidates: List[IconCandidate] = []
    for method, fn in METHODS:
        triples = fn(domain)
        all_candidates.extend(build_candidates(domain, method, triples))
    return domain, all_candidates


def pick_best_with_exception(domain: str, candidates: List[IconCandidate]) -> Tuple[Optional[IconCandidate], Optional[str]]:
    """Pick the best icon, with the monochrome SVG exception.

    Returns (best_candidate|None, exception_message|None)
    """
    if not candidates:
        return None, None

    ordered = sorted(candidates, key=quality_key, reverse=True)
    best = ordered[0]

    if best.is_svg and _is_svg_monochrome_black_or_white(best.content):
        # find next best
        for nxt in ordered[1:]:
            if nxt.sha256 != best.sha256:
                msg = (
                    f"Monochrome SVG skipped for {domain}: method={best.method}, url={best.url} "
                    f"-> picked {nxt.method} ({nxt.ext})"
                )
                return nxt, msg
        # no alternative
        msg = f"Monochrome SVG detected for {domain} but no alternative candidate was available."
        return best, msg

    return best, None


def main() -> int:
    firefox_path = "~/.mozilla/firefox/"
    db_path = searchPlaces(firefox_path)
    if not db_path or not os.path.exists(db_path):
        print(f"Could not find places.sqlite using path {firefox_path}")
        return 1

    print("Fetching domains from Firefox bookmarks...")
    domains = get_all_domains(db_path)
    total = len(domains)
    print(f"Found {total} unique domains in bookmarks.")

    output_dir = ensure_output_dir()
    os.makedirs(output_dir, exist_ok=True)

    print(f"\nPicking best favicons (workers=10), saving to: {output_dir}")
    start = time.time()

    best_by_domain: Dict[str, IconCandidate] = {}
    exception_messages: List[str] = []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(per_domain_job, d) for d in domains]
        done = 0
        last_log = time.time()
        for fut in as_completed(futures):
            d, candidates = fut.result()
            best, msg = pick_best_with_exception(d, candidates)
            if msg:
                exception_messages.append(msg)
            if best is not None:
                best_by_domain[d] = best
                save_best_candidate(output_dir, best)

            done += 1
            now = time.time()
            if now - last_log >= 2.0:
                rate = done / max(0.001, (now - start))
                print(f"Progress: {done}/{total} domains ({rate:.1f}/s), best_found={len(best_by_domain)}")
                last_log = now

    elapsed = time.time() - start

    # Report
    found = len(best_by_domain)
    print("\n--- Summary ---")
    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Domains with a best icon: {found}/{total} ({(found/total*100.0 if total else 0.0):.1f}%)")

    if exception_messages:
        print("\n--- Monochrome SVG exception used ---")
        # Print up to 200 lines to keep output readable.
        for line in exception_messages[:200]:
            print(line)
        if len(exception_messages) > 200:
            print(f"... ({len(exception_messages) - 200} more)")

    report = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_domains": total,
            "workers": 10,
            "elapsed_seconds": elapsed,
        },
        "results": {
            "domains_with_best_icon": found,
            "coverage": (found / total if total else 0.0),
            "monochrome_svg_exception_count": len(exception_messages),
        },
        "exceptions": exception_messages,
        "best_per_domain": {
            d: {
                "method": c.method,
                "url": c.url,
                "mime": c.mime,
                "ext": c.ext,
                "is_svg": c.is_svg,
                "max_side": c.max_side,
                "bytes": c.bytes_len,
                "sha256": c.sha256,
                "file": f"{sanitize_domain(d)}/{_candidate_filename(c, best=True)}",
            }
            for d, c in best_by_domain.items()
        },
    }

    report_path = os.path.join(output_dir, "best_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    print(f"\nWrote report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
