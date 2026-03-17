import time
from typing import Optional


def _run(domain: str) -> None:
    url = f"https://{domain}"

    try:
        import extract_favicon  # type: ignore
    except Exception as e:
        print("extract_favicon is not available:", e)
        print("Create a venv and install it, e.g.:")
        print("  python -m venv .venv && source .venv/bin/activate && pip install extract-favicon")
        return

    print("Testing Extract_Favicon for:", url)

    # extract_favicon can be very noisy on stderr (e.g., suffix-list/tldextract/browser detection).
    import contextlib
    import io

    t0 = time.time()
    with contextlib.redirect_stderr(io.StringIO()):
        best = extract_favicon.get_best_favicon(url)
    print(f"get_best_favicon took {time.time() - t0:.2f}s")

    if not best:
        print("No favicon found")
        return

    print("Best favicon:")
    print("  url:", best.url)
    print("  valid:", getattr(best, "valid", None))
    print("  reachable:", getattr(best, "reachable", None))
    print("  format:", getattr(best, "format", None))
    print("  size:", getattr(best, "width", None), "x", getattr(best, "height", None))

    t1 = time.time()
    with contextlib.redirect_stderr(io.StringIO()):
        downloaded = extract_favicon.download([best], mode="largest", sleep_time=0)
    print(f"download(mode='largest') took {time.time() - t1:.2f}s")

    if not downloaded:
        print("Download returned empty list")
        return

    fav = downloaded[0]

    # If SVG, the library validates it but doesn't expose bytes; fetch it directly for debugging.
    if isinstance(fav.url, str) and fav.url.lower().split("?")[0].endswith(".svg"):
        import urllib.request

        req = urllib.request.Request(fav.url, headers={"User-Agent": "Mozilla/5.0"})
        t2 = time.time()
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        print(f"fetched svg bytes in {time.time() - t2:.2f}s, bytes={len(data)}")
        return

    img = getattr(fav, "image", None)
    if img is None:
        print("Downloaded favicon has no .image")
        return

    import io as _io

    buf = _io.BytesIO()
    try:
        img.save(buf, format="PNG")
        data = buf.getvalue()
        print("saved png bytes=", len(data))
    except Exception as e:
        print("Failed to save image:", e)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Debug extract_favicon on a single domain")
    parser.add_argument("domain", nargs="?", default="github.com", help="Domain to test (default: github.com)")
    args = parser.parse_args()

    _run(args.domain)
