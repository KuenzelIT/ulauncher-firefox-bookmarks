from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, SystemExitEvent,PreferencesUpdateEvent, PreferencesEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.RunScriptAction import RunScriptAction
from history import FirefoxHistory
import os
from urllib.parse import urlparse
import struct
import logging

from favicon_fetcher import fetch_best_favicon

logger = logging.getLogger(__name__)

def extract_largest_icon_bytes(data: bytes) -> bytes:
    try:
        # Check if it's an ICO file
        if len(data) < 6:
            return data
            
        reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
        
        # Only process if valid ICO format and has multiple images
        if icon_type != 1 or count <= 1:
            return data
            
        entries = []
        offset = 6
        
        # Parse ICONDIR entries safely
        for i in range(count):
            if offset + 16 > len(data):
                break
                
            width, height, colors, reserved, planes, bpp, size, img_offset = struct.unpack_from(
                "<BBBBHHII", data, offset
            )
            
            # Width and height can be 0, which means 256
            width = 256 if width == 0 else width
            height = 256 if height == 0 else height
            
            # If the entry says it's 48x48 but the size implies something else,
            # this is a malformed entry (common in some web icons)
            # The issue in ulauncher is "Format error decoding Ico: Entry(48, 48) and BMP(16, 16) dimensions do not match"
            
            # Basic validation of offset and size
            if img_offset + size <= len(data):
                entries.append({
                    "width": width,
                    "height": height,
                    "size": size,
                    "offset": img_offset
                })
                
            offset += 16
            
        if not entries:
            return data
            
        # check if it contains any valid PNG payload
        # if not, return the original data. DO NOT extract raw DIBs.
        has_png = False
        for entry in entries:
            img_data = data[entry["offset"]:entry["offset"] + entry["size"]]
            if img_data.startswith(b"\x89PNG\r\n\x1a\n"):
                has_png = True
                break
        
        if not has_png:
            # If there's no PNG layer, the parsing tool we pass it to (Ulauncher/glycin) might fail 
            # if the DIB sizes don't match entry headers, so we just return the original file.
            return data

        # pick largest resolution that is a PNG
        valid_png_entries = []
        for entry in entries:
            img_data = data[entry["offset"]:entry["offset"] + entry["size"]]
            if img_data.startswith(b"\x89PNG\r\n\x1a\n"):
                valid_png_entries.append(entry)

        if not valid_png_entries:
            return data

        best = max(valid_png_entries, key=lambda e: e["width"] * e["height"])
        return data[best["offset"]:best["offset"] + best["size"]]
        
    except Exception:
        # If parsing fails, fall back to returning original data
        return data

class FirefoxHistoryExtension(Extension):
    def __init__(self):
        super(FirefoxHistoryExtension, self).__init__()
        logger.info('FirefoxBookmarks extension starting up')
        #   Firefox History Getter
        #   Delayed initialisation, need to get path from preferences
        self.fh = None
        self.downloading_favicons = set()
        #   Ulauncher Events
        self.subscribe(KeywordQueryEvent,KeywordQueryEventListener())
        self.subscribe(SystemExitEvent,SystemExitEventListener())
        self.subscribe(PreferencesEvent,PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent,PreferencesUpdateEventListener())

        # Try eager initialization if preferences are already available.
        try:
            firefox_path = self.preferences.get('path') if hasattr(self, 'preferences') else None
            if firefox_path:
                logger.info(f'Initializing FirefoxHistory (from preferences path={firefox_path})')
                self.init_fh(firefox_path)
        except Exception as e:
            logger.debug(f'Could not init from preferences yet: {e}')

        # Fallback: manifest default says this needs a restart, but in practice
        # Ulauncher may send PreferencesEvent before preferences are populated.
        # Use the default path to avoid a dead-start.
        if self.fh is None:
            try:
                fallback_path = os.path.expanduser('~/.mozilla/firefox/')
                logger.info(f'Initializing FirefoxHistory (fallback path={fallback_path})')
                self.init_fh(fallback_path)
            except Exception as e:
                logger.debug(f'Fallback init failed: {e}')

    def init_fh(self, firefox_path: str):
        #   Initialise Firefox History Getter with path from preferences
        if self.fh is None:
            logger.info(f'Creating FirefoxHistory using path={firefox_path}')
            self.fh = FirefoxHistory(firefox_path)
            logger.info('FirefoxHistory initialized; starting favicon cache warmup')
            self.cache_all_favicons()

    def cache_all_favicons(self):
        import threading
        def worker():
            logger.info("Starting background best favicon caching process.")
            cache_dir = os.path.expanduser('~/.cache/ulauncher-firefox-bookmarks-favicons')
            os.makedirs(cache_dir, exist_ok=True)
            domains = self.fh.get_all_domains()
            logger.info(f"Found {len(domains)} unique domains in Firefox bookmarks.")
            
            cached_count = 0
            download_count = 0
            
            for domain in domains:
                domain_icon_path = os.path.join(cache_dir, f"{domain}.ico")
                if os.path.exists(domain_icon_path) and os.path.getsize(domain_icon_path) > 0:
                    cached_count += 1
                else:
                    self.download_favicon_async(domain, domain_icon_path)
                    download_count += 1
                    
            logger.info(f"Favicon cache status: {cached_count} already cached, queued {download_count} for download.")
            
        threading.Thread(target=worker, daemon=True).start()

    def download_favicon_async(self, domain, domain_icon_path):
        if domain in self.downloading_favicons:
            return
        self.downloading_favicons.add(domain)
        import threading

        def worker():
            try:
                best, msg = fetch_best_favicon(domain)
                if msg:
                    logger.info(msg)

                if best and best.content:
                    content = extract_largest_icon_bytes(best.content)
                    with open(domain_icon_path, 'wb') as out_file:
                        out_file.write(content)
                    logger.debug(
                        f"Successfully downloaded and cached favicon for {domain} "
                        f"(method={best.method}, ext={best.ext}, max_side={best.max_side}, bytes={best.bytes_len})"
                    )
                else:
                    logger.debug(f"Failed to find favicon for {domain}")
            except Exception as e:
                logger.debug(f"Error downloading favicon for {domain}: {str(e)}")
            finally:
                self.downloading_favicons.discard(domain)

        threading.Thread(target=worker, daemon=True).start()

class PreferencesEventListener(EventListener):
    def on_event(self,event,extension):
        try:
            n = int(event.new_value)
        except Exception:
            n = 10

        # `PreferencesEvent` can arrive before our history DB is initialized.
        # Initialize if possible; otherwise just skip setting the limit for now.
        if extension.fh is None:
            try:
                firefox_path = extension.preferences.get('path') if hasattr(extension, 'preferences') else None
                if firefox_path:
                    extension.init_fh(firefox_path)
            except Exception:
                pass

        if extension.fh is not None:
            extension.fh.limit = n
        else:
            logger.debug("PreferencesEvent received before FirefoxHistory was initialized; skipping limit update")

class PreferencesUpdateEventListener(EventListener):
    def on_event(self,event,extension):
        firefox_path = event.preferences.get('path') if hasattr(event, 'preferences') else None
        if firefox_path:
            extension.init_fh(firefox_path)
        else:
            logger.debug('PreferencesUpdateEvent received without a path; skipping FirefoxHistory init')
            return
        #   Results Order
        #if event.id == 'order':
        #    extension.fh.order = event.new_value
        #   Results Number
        if event.id == 'limit':
            try:
                n = int(event.new_value)
                extension.fh.limit = n
            except:
                pass
        #elif event.id == 'aggregate':
        #    extension.fh.aggregate = event.new_value

class SystemExitEventListener(EventListener):
    def on_event(self,event,extension):
        if extension.fh is not None:
            extension.fh.close()

class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        firefox_path = extension.preferences.get('path') if hasattr(extension, 'preferences') else None
        if firefox_path:
            extension.init_fh(firefox_path)
        else:
            logger.debug('KeywordQueryEvent received before preferences path was available; returning empty results')
            return RenderResultListAction([])
        query  = event.get_argument()
        #   Blank Query
        if query == None:
            query = ''
        items = []

        cache_dir = os.path.expanduser('~/.cache/ulauncher-firefox-bookmarks-favicons')
        os.makedirs(cache_dir, exist_ok=True)

        #   Search into Firefox History
        results = extension.fh.search(query)
        for link in results:
            url = link[1]
            title = link[0] if link[0] else url
            icon_path = 'images/icon.svg'

            if url:
                domain = urlparse(url).netloc
                if domain:
                    domain_icon_path = os.path.join(cache_dir, f"{domain}.ico")
                    
                    if os.path.exists(domain_icon_path):
                        # Some icons might be invalid (e.g., 0 bytes), so check size
                        if os.path.getsize(domain_icon_path) > 0:
                            icon_path = domain_icon_path
                        else:
                            extension.download_favicon_async(domain, domain_icon_path)
                    else:
                        extension.download_favicon_async(domain, domain_icon_path)

            items.append(ExtensionResultItem(icon=icon_path,
                                            name=title,
                                            description=url,
                                            on_enter=RunScriptAction(f"xdg-open {url}")))

        return RenderResultListAction(items)

if __name__ == '__main__':
    FirefoxHistoryExtension().run()
