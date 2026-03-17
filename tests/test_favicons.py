import os
import sqlite3
import urllib.request
from urllib.parse import urlparse
import tempfile
import configparser
from concurrent.futures import ThreadPoolExecutor
import shutil

# --- Added for Extract_Favicon comparison + reporting ---
import json
import hashlib
from typing import Optional, Tuple, Dict, Any

try:
    # Optional dependency. Install in a venv or via pipx.
    import extract_favicon  # type: ignore
except Exception:
    extract_favicon = None  # type: ignore


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _save_icon_bytes(output_dir: str, domain: str, content: bytes) -> None:
    # Keep your existing naming for easy inspection.
    with open(os.path.join(output_dir, f"{domain}.ico"), "wb") as f:
        f.write(content)

# Reuse logic from history.py
def searchPlaces(firefox_path):
    firefox_path = os.path.expanduser(firefox_path)
    if not firefox_path.endswith("/"):
        firefox_path += "/"
    conf_path = os.path.join(firefox_path, 'profiles.ini')
    profile = configparser.RawConfigParser()
    profile.read(conf_path)
    if not profile.has_section("Profile0"):
        return None
    prof_path = profile.get("Profile0", "Path")
    sql_path = os.path.join(firefox_path, prof_path)
    return os.path.join(sql_path, 'places.sqlite')

def get_all_domains(db_path):
    # Firefox places.sqlite is often locked, so we copy it first
    temp_db_path = tempfile.mktemp()
    shutil.copyfile(db_path, temp_db_path)
    
    conn = sqlite3.connect(temp_db_path, check_same_thread=False)
    query = 'SELECT url FROM moz_bookmarks AS A JOIN moz_places AS B ON(A.fk = B.id)'
    cursor = conn.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()
    
    domains = set()
    for row in rows:
        url = row[0]
        if url:
            domain = urlparse(url).netloc
            if domain:
                domains.add(domain)
    conn.close()
    
    try:
        os.remove(temp_db_path)
    except OSError:
        pass
        
    return list(domains)

def test_fetch_duckduckgo(domain):
    favicon_url = f"https://icons.duckduckgo.com/ip3/{domain}.ico"
    req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            content = response.read()
            if len(content) > 0:
                return domain, content
            return domain, None
    except Exception:
        # Try root domain fallback
        parts = domain.split('.')
        if len(parts) > 2:
            root_domain = '.'.join(parts[-2:])
            favicon_url = f"https://icons.duckduckgo.com/ip3/{root_domain}.ico"
            req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    content = response.read()
                    if len(content) > 0:
                        return domain, content
            except Exception:
                pass
        return domain, None

def test_fetch_google(domain):
    favicon_url = f"https://www.google.com/s2/favicons?domain={domain}&sz=64"
    req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            content = response.read()
            if len(content) > 0:
                return domain, content
            return domain, None
    except Exception:
        # Try root domain fallback
        parts = domain.split('.')
        if len(parts) > 2:
            root_domain = '.'.join(parts[-2:])
            favicon_url = f"https://www.google.com/s2/favicons?domain={root_domain}&sz=64"
            req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    content = response.read()
                    if len(content) > 0:
                        return domain, content
            except Exception:
                pass
        return domain, None

def test_fetch_google_v2(domain):
    favicon_url = f"https://t2.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=64"
    req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            content = response.read()
            if len(content) > 0:
                return domain, content
            return domain, None
    except Exception:
        return domain, None

def test_fetch_iconhorse(domain):
    favicon_url = f"https://icon.horse/icon/{domain}"
    req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            content = response.read()
            if len(content) > 0:
                return domain, content
            return domain, None
    except Exception:
        return domain, None

def test_fetch_direct(domain):
    import urllib.request
    favicon_url = f"https://{domain}/favicon.ico"
    req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.getcode() == 200 and "image" in response.headers.get("content-type", ""):
                return domain, response.read()
            return domain, None
    except Exception:
        # Try root domain fallback
        parts = domain.split('.')
        if len(parts) > 2:
            root_domain = '.'.join(parts[-2:])
            favicon_url = f"https://{root_domain}/favicon.ico"
            req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.getcode() == 200 and "image" in response.headers.get("content-type", ""):
                        return domain, response.read()
            except Exception:
                pass
        return domain, None

from html.parser import HTMLParser

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.icons = []
        self.manifest = None

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            attrs_dict = dict(attrs)
            rel = attrs_dict.get("rel", "").lower()
            if "icon" in rel:
                if "href" in attrs_dict:
                    self.icons.append(attrs_dict["href"])
            elif "manifest" in rel:
                if "href" in attrs_dict:
                    self.manifest = attrs_dict["href"]

def test_fetch_html(domain):
    import urllib.request
    from urllib.parse import urljoin
    try:
        url = f"https://{domain}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8'})
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
        parser = LinkParser()
        parser.feed(html)
        
        for href in parser.icons:
            icon_url = urljoin(url, href)
            # Remove method='HEAD' to actually download it
            ireq = urllib.request.Request(icon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'})
            try:
                with urllib.request.urlopen(ireq, timeout=5) as ir:
                    if ir.getcode() == 200:
                        return domain, ir.read()
            except Exception:
                pass
        return domain, None
    except Exception:
        # Try root domain fallback
        parts = domain.split('.')
        if len(parts) > 2:
            root_domain = '.'.join(parts[-2:])
            res = test_fetch_html(root_domain)
            if res[1] is not None:
                return domain, res[1]
        return domain, None

def test_fetch_manifest(domain):
    import urllib.request
    from urllib.parse import urljoin
    import json
    try:
        url = f"https://{domain}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8'})
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
        parser = LinkParser()
        parser.feed(html)
        
        if parser.manifest:
            manifest_url = urljoin(url, parser.manifest)
            mreq = urllib.request.Request(manifest_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(mreq, timeout=5) as mr:
                data = json.loads(mr.read().decode('utf-8'))
                if "icons" in data and len(data["icons"]) > 0:
                    icon_url = urljoin(manifest_url, data["icons"][0].get("src", ""))
                    ireq = urllib.request.Request(icon_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    try:
                        with urllib.request.urlopen(ireq, timeout=5) as ir:
                            if ir.getcode() == 200:
                                return domain, ir.read()
                    except Exception:
                        pass
        return domain, None
    except Exception:
        # Try root domain fallback
        parts = domain.split('.')
        if len(parts) > 2:
            root_domain = '.'.join(parts[-2:])
            res = test_fetch_manifest(root_domain)
            if res[1] is not None:
                return domain, res[1]
        return domain, None

def _domain_to_url(domain: str) -> str:
    # Extract_Favicon expects a URL; our dataset is domains.
    return f"https://{domain}"


def _run_extract_favicon(domain: str) -> Tuple[str, Optional[bytes]]:
    """Return (domain, icon_bytes) using Extract_Favicon if available."""
    if extract_favicon is None:
        return domain, None

    # extract_favicon (via dependencies like tldextract) can be very noisy on stderr.
    # Keep our test output readable by suppressing stderr for this call.
    import contextlib
    import io

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            best = extract_favicon.get_best_favicon(_domain_to_url(domain))  # type: ignore[attr-defined]
            if not best:
                return domain, None

            # download() returns Favicon objects where 'image' is a PIL Image
            real = extract_favicon.download([best], mode="largest", sleep_time=0)  # type: ignore[attr-defined]
            if not real:
                return domain, None

            fav = real[0]

        # SVGs are stored/validated but we don't get raw bytes in the object.
        # If the selected icon URL is an SVG, fetch bytes directly.
        try:
            url = getattr(fav, "url", "")
            if isinstance(url, str) and url.lower().split("?")[0].endswith(".svg"):
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                    return domain, data if data else None
        except Exception:
            pass

        img = getattr(fav, "image", None)
        if img is not None:
            import io as _io

            buf = _io.BytesIO()
            # Use PNG as stable output format for hashing/comparison.
            try:
                img.save(buf, format="PNG")
            except Exception:
                buf = _io.BytesIO()
                img.save(buf, format="ICO")
            return domain, buf.getvalue()

        return domain, None
    except Exception:
        return domain, None


def _run_our_sequential(domain: str,
                        baseline_success: set,
                        html_success: set,
                        direct_success: set,
                        final_icons: Dict[str, bytes]) -> Tuple[str, Optional[bytes]]:
    """Return the same icon bytes our current script would pick in its sequential simulation."""
    # Step1: baseline (DDG+Google). Our `final_icons` already stores the first icon
    # encountered across all methods in execution order, but we want to simulate the
    # sequential algorithm: baseline -> html -> direct.
    if domain in baseline_success:
        return domain, final_icons.get(domain)

    if domain in html_success:
        # `final_icons` may already contain an HTML icon or another method; try to
        # fetch explicitly to ensure correct source.
        _, content = test_fetch_html(domain)
        return domain, content

    if domain in direct_success:
        _, content = test_fetch_direct(domain)
        return domain, content

    return domain, None

if __name__ == '__main__':
    import time
    firefox_path = "~/.mozilla/firefox/"
    db_path = searchPlaces(firefox_path)
    
    if not db_path or not os.path.exists(db_path):
        print(f"Could not find places.sqlite using path {firefox_path}")
        exit(1)

    print("Fetching domains from Firefox history...")
    domains = get_all_domains(db_path)
    total = len(domains)
    print(f"Found {total} unique domains in bookmarks.")
    
    final_icons = {}

    def run_tests(name, func):
        print(f"\nTesting {name}...")
        start_time = time.time()
        success_domains = set()
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = executor.map(func, domains)
            for domain, content in results:
                if content is not None:
                    success_domains.add(domain)
                    if domain not in final_icons:
                        final_icons[domain] = content
        elapsed_time = time.time() - start_time
        print(f"[{name}] took {elapsed_time:.2f} seconds")
        return success_domains, elapsed_time
        
    ddg_success_domains, ddg_time = run_tests("DuckDuckGo favicon API", test_fetch_duckduckgo)
    google_success_domains, google_time = run_tests("Google favicon API", test_fetch_google)
    google_v2_success_domains, google_v2_time = run_tests("Google Favicon V2 API", test_fetch_google_v2)
    iconhorse_success_domains, iconhorse_time = run_tests("IconHorse API", test_fetch_iconhorse)
    direct_success_domains, direct_time = run_tests("Direct /favicon.ico fetch", test_fetch_direct)
    html_success_domains, html_time = run_tests("HTML <link rel='icon'> parsing", test_fetch_html)
    manifest_success_domains, manifest_time = run_tests("Manifest icons parsing", test_fetch_manifest)
    
    baseline_success_domains = ddg_success_domains | google_success_domains
    
    combined_success_domains = (baseline_success_domains | 
                              google_v2_success_domains | iconhorse_success_domains |
                              direct_success_domains | html_success_domains | 
                              manifest_success_domains)
    
    print(f"\nFinal Results (Found / Time):")
    print(f"DuckDuckGo: {len(ddg_success_domains)}/{total} found ({ddg_time:.2f}s)")
    print(f"Google: {len(google_success_domains)}/{total} found ({google_time:.2f}s)")
    print(f"Google V2: {len(google_v2_success_domains)}/{total} found ({google_v2_time:.2f}s)")
    print(f"IconHorse: {len(iconhorse_success_domains)}/{total} found ({iconhorse_time:.2f}s)")
    print(f"Direct /favicon.ico: {len(direct_success_domains)}/{total} found ({direct_time:.2f}s)")
    print(f"HTML <link rel='icon'>: {len(html_success_domains)}/{total} found ({html_time:.2f}s)")
    print(f"Manifest icons: {len(manifest_success_domains)}/{total} found ({manifest_time:.2f}s)")
    
    print(f"\n--- Incremental Value Over Baseline (DDG + Google S2 = {len(baseline_success_domains)} domains) ---")
    print(f"Google V2 adds: {len(google_v2_success_domains - baseline_success_domains)} unique new domains")
    print(f"IconHorse adds: {len(iconhorse_success_domains - baseline_success_domains)} unique new domains")
    print(f"Direct /favicon.ico adds: {len(direct_success_domains - baseline_success_domains)} unique new domains")
    print(f"HTML parsing adds: {len(html_success_domains - baseline_success_domains)} unique new domains")
    print(f"Manifest icons add: {len(manifest_success_domains - baseline_success_domains)} unique new domains")

    print(f"\nCombined (All methods): {len(combined_success_domains)}/{total} found.")

    print("\n--- Final Sequential Algorithm Simulation ---")
    step1_domains = baseline_success_domains
    print(f"Step 1 (Baseline): DDG + Google S2 found {len(step1_domains)}/{total} domains.")
    
    missing_after_step1 = set(domains) - step1_domains
    html_found_in_missing = html_success_domains.intersection(missing_after_step1)
    step2_domains = step1_domains | html_found_in_missing
    print(f"Step 2: HTML parsing on missing domains found {len(html_found_in_missing)} additional domains. Total: {len(step2_domains)}/{total}.")
    
    missing_after_step2 = set(domains) - step2_domains
    direct_found_in_missing = direct_success_domains.intersection(missing_after_step2)
    step3_domains = step2_domains | direct_found_in_missing
    print(f"Step 3: Direct /favicon.ico on still missing domains found {len(direct_found_in_missing)} additional domains. Total: {len(step3_domains)}/{total}.")
    
    if total > 0:
        print(f"Final Algorithm Coverage: {len(step3_domains)/total*100:.1f}%")

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_favicons_output")
    os.makedirs(output_dir, exist_ok=True)

    print(f"\nSaving {len(final_icons)} distinct favicons to {output_dir} ...")
    for domain, content in final_icons.items():
        _save_icon_bytes(output_dir, domain, content)
    print("Done!")

    # --- Extract_Favicon comparison ---
    if extract_favicon is None:
        print("\n--- Extract_Favicon comparison ---")
        print("extract_favicon is not installed; skipping comparison.")
        print("Install via a venv or pipx, e.g.:")
        print("  python -m venv .venv && source .venv/bin/activate && pip install extract-favicon")
    else:
        print("\n--- Extract_Favicon comparison ---")
        print("Running Extract_Favicon.get_best_favicon on the same domains...")

        ef_icons: Dict[str, bytes] = {}
        ef_duration_by_domain: Dict[str, float] = {}

        def _ef_worker(domain: str) -> Tuple[str, Optional[bytes], float]:
            import time as _time

            t0 = _time.time()
            d, content = _run_extract_favicon(domain)
            return d, content, _time.time() - t0

        start_ef = time.time()
        with ThreadPoolExecutor(max_workers=10) as executor:
            completed = 0
            last_log = time.time()
            for d, content, dt in executor.map(_ef_worker, domains):
                completed += 1
                ef_duration_by_domain[d] = dt
                if content is not None and d not in ef_icons:
                    ef_icons[d] = content

                # Live progress logs every ~2s
                now = time.time()
                if now - last_log >= 2.0:
                    rate = completed / max(0.001, (now - start_ef))
                    print(f"[extract_favicon] {completed}/{total} processed ({rate:.1f}/s), found={len(ef_icons)}")
                    last_log = now
        ef_time = time.time() - start_ef

        print(f"Extract_Favicon: {len(ef_icons)}/{total} found ({ef_time:.2f}s)")

        # Build our sequential-picked icons for a fair comparison
        our_seq_icons: Dict[str, bytes] = {}
        for d in domains:
            _, b = _run_our_sequential(d, baseline_success_domains, html_success_domains, direct_success_domains, final_icons)
            if b is not None:
                our_seq_icons[d] = b

        # Hash maps
        our_hash = {d: _sha256_bytes(b) for d, b in our_seq_icons.items()}
        ef_hash = {d: _sha256_bytes(b) for d, b in ef_icons.items()}

        our_found = set(our_seq_icons.keys())
        ef_found = set(ef_icons.keys())
        both_found = our_found & ef_found

        identical = sum(1 for d in both_found if our_hash.get(d) == ef_hash.get(d))

        comparison = {
            "meta": {
                "total_domains": total,
                "workers": 10,
            },
            "our_current": {
                "found": len(combined_success_domains),
                "note": "Union of all methods executed (not sequential pick).",
            },
            "our_sequential_pick": {
                "found": len(our_found),
                "coverage": (len(our_found) / total if total else 0.0),
                "strategy": ["duckduckgo", "google_s2", "html", "direct"],
            },
            "extract_favicon": {
                "found": len(ef_found),
                "coverage": (len(ef_found) / total if total else 0.0),
                "elapsed_seconds": ef_time,
                "per_domain_seconds": {
                    "p50": sorted(ef_duration_by_domain.values())[len(ef_duration_by_domain) // 2] if ef_duration_by_domain else None,
                    "p90": sorted(ef_duration_by_domain.values())[max(0, int(len(ef_duration_by_domain) * 0.90) - 1)] if ef_duration_by_domain else None,
                },
            },
            "overlap": {
                "both_found": len(both_found),
                "only_ours": len(our_found - ef_found),
                "only_extract_favicon": len(ef_found - our_found),
                "identical_hash_among_both": identical,
                "identical_ratio_among_both": (identical / len(both_found) if both_found else 0.0),
            },
            "sets": {
                "only_ours": sorted(list(our_found - ef_found))[:200],
                "only_extract_favicon": sorted(list(ef_found - our_found))[:200],
            },
        }

        report_path = os.path.join(output_dir, "compare_extract_favicon.json")
        _write_json(report_path, comparison)
        print(f"Wrote comparison report: {report_path}")

        # Optionally save Extract_Favicon bytes separately for manual inspection
        ef_dir = os.path.join(output_dir, "extract_favicon")
        os.makedirs(ef_dir, exist_ok=True)
        for domain, content in ef_icons.items():
            _save_icon_bytes(ef_dir, domain, content)
        print(f"Saved {len(ef_icons)} Extract_Favicon results to: {ef_dir}")
