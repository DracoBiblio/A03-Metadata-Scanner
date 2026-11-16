# online_lookup.py
#
# Optional online AO3 metadata lookup.
# This module does not run automatically. The caller must explicitly
# call fetch_ao3_metadata() (single) or fetch_ao3_metadata_batch()
# (throttled, designed to run inside a worker thread).

import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

# Share the URL pattern with the offline parser so tweaks happen in one
# place. The fallback keeps this module importable outside Calibre (tests).
try:
    from calibre_plugins.ao3_fandom_scanner.parser import AO3_WORK_RE
except ImportError:  # pragma: no cover - standalone/test use
    AO3_WORK_RE = re.compile(
        r"https?://(?:www\.)?archiveofourown\.org/works/\d+",
        re.I,
    )

# Use Calibre's configured proxies when available so users behind corporate,
# school, or national proxies are not cut off (plain urllib ignores them).
try:
    from calibre.utils.proxy import get_proxies
except Exception:  # pragma: no cover - standalone/test use
    get_proxies = None

USER_AGENT = "FanfictionMetadataScanner/1.0 (Calibre metadata plugin)"

MAX_PAGE_BYTES = 3 * 1024 * 1024

ALLOWED_LABELS = {
    "rating",
    "archive warnings",
    "categories",
    "category",
    "fandoms",
    "fandom",
    "relationships",
    "relationship",
    "characters",
    "additional tags",
    "language",
    "series",
}

# Machine-readable error kinds for the caller; result["error"] stays human-readable.
_ERROR_KIND_BY_STATUS = {
    403: "forbidden",
    404: "not_found",
    410: "not_found",
    429: "rate_limited",
    502: "unavailable",
    503: "unavailable",
}


def find_ao3_url(text):
    """Return the first AO3 work URL found in text, or None."""
    if not text:
        return None

    match = AO3_WORK_RE.search(text)
    return match.group(0) if match else None


def _build_opener():
    """Build an opener honoring Calibre's proxy settings when present."""
    if get_proxies is not None:
        try:
            proxies = get_proxies() or {}
        except Exception:
            proxies = {}
        if proxies:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler(proxies)
            )
    return urllib.request.build_opener()


class _AO3MetadataParser(HTMLParser):
    """Read AO3's labelled dt/dd metadata fields.

    State is reset defensively so malformed or truncated markup cannot
    bleed content across fields: finishing a dd (on </dd>, a following
    <dt>/<dd>, or </dl>) flushes any open field.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.current_label = None
        self.in_dt = False
        self.in_dd = False
        self.dt_parts = []
        self.dd_parts = []
        self.in_tag_link = False
        self.tag_link_parts = []
        self.tag_links = []

    def _close_tag_link(self):
        value = " ".join(" ".join(self.tag_link_parts).split())
        if value:
            self.tag_links.append(value)
        self.in_tag_link = False
        self.tag_link_parts = []

    def _finish_dd(self):
        if self.in_dd:
            if self.current_label:
                if self.tag_links:
                    value = self.tag_links
                else:
                    value = " ".join(" ".join(self.dd_parts).split())
                if value:
                    self.fields[self.current_label] = value
            self.current_label = None
            self.in_dd = False
        self.dd_parts = []
        self.tag_links = []
        # A nested or unclosed <a> must not leak text into the next field.
        if self.in_tag_link:
            self._close_tag_link()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "dt":
            self._finish_dd()
            self.in_dt = True
            self.dt_parts = []

        elif tag == "dd":
            self._finish_dd()
            self.in_dd = True
            self.dd_parts = []
            self.tag_links = []

        elif tag == "a" and self.in_dd:
            classes = (attrs.get("class") or "").split()
            if "tag" in classes:
                # Nested <a> inside a tag link: keep what we already have.
                if self.in_tag_link:
                    self._close_tag_link()
                self.in_tag_link = True
                self.tag_link_parts = []

    def handle_data(self, data):
        if self.in_dt:
            self.dt_parts.append(data)

        if self.in_dd:
            self.dd_parts.append(data)

        if self.in_tag_link:
            self.tag_link_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_tag_link:
            self._close_tag_link()

        elif tag == "dt":
            self.in_dt = False
            label = " ".join(" ".join(self.dt_parts).split())
            label = label.rstrip(":").casefold()
            self.current_label = (
                label if label in ALLOWED_LABELS else None
            )

        elif tag == "dd":
            self._finish_dd()

        elif tag == "dl":
            # End of the metadata block: flush anything left dangling.
            self._finish_dd()


def _adult_bypass_url(work_url):
    """Append AO3's adult-content bypass so guest visitors are not stopped
    by the 'This work could have adult content' interstitial, which has no
    dt/dd metadata block."""
    if "?" in work_url:
        return work_url + "&view_adult=true"
    return work_url + "?view_adult=true"


def _empty_result(url):
    return {
        "ok": False,
        "url": url,
        "fields": {},
        "error": None,
        "error_kind": None,
        "http_status": None,
        "truncated": False,
        "origin": "AO3 website",
    }


def fetch_ao3_metadata(url, timeout=8, cookie_header=None):
    """
    Fetch metadata from an AO3 work page.

    Returns a dict:
        {
            "ok": True/False,
            "url": final fetched URL,
            "fields": {label: value-or-list},
            "error": human-readable message or None,
            "error_kind": machine-readable kind or None,
                ('invalid_url', 'not_found', 'forbidden', 'rate_limited',
                 'unavailable', 'network', 'too_large', 'no_fields'),
            "http_status": HTTP status code or None,
            "truncated": True if the page exceeded MAX_PAGE_BYTES,
            "origin": "AO3 website",
        }

    This function is only called when explicitly requested by the caller.
    Callers looping over many books should use fetch_ao3_metadata_batch()
    instead so requests are throttled.
    """

    result = _empty_result(url)

    match = AO3_WORK_RE.search(url or "")
    if not match:
        result["error"] = "No valid AO3 work URL was supplied."
        result["error_kind"] = "invalid_url"
        return result

    # Use the matched work URL, not arbitrary text following it.
    work_url = _adult_bypass_url(match.group(0))
    result["url"] = work_url

    request = urllib.request.Request(
        work_url,
        headers={"User-Agent": USER_AGENT},
    )

    if cookie_header:
        request.add_header("Cookie", cookie_header)

    try:
        opener = _build_opener()
        with opener.open(request, timeout=timeout) as response:
            result["http_status"] = getattr(response, "status", None)
            raw = response.read(MAX_PAGE_BYTES + 1)

        if len(raw) > MAX_PAGE_BYTES:
            raw = raw[:MAX_PAGE_BYTES]
            result["truncated"] = True

        page = raw.decode("utf-8", "replace")

        parser = _AO3MetadataParser()
        parser.feed(page)
        parser.close()

        result["fields"] = parser.fields
        result["ok"] = bool(parser.fields)

        if result["ok"] and result["truncated"]:
            result["error"] = (
                "The AO3 page was larger than the download limit; some "
                "metadata fields may be missing. "
            )
        elif not result["ok"]:
            result["error"] = (
                "The AO3 page loaded, but no labelled metadata fields were "
                "found. The work may be login-restricted, or the page "
                "layout may have changed."
            )
            result["error_kind"] = "no_fields"

    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["error_kind"] = _ERROR_KIND_BY_STATUS.get(exc.code, "http_error")
        detail = {
            "not_found": "The work does not exist or has been deleted.",
            "forbidden": "AO3 refused the request (possibly bot protection).",
            "rate_limited": "AO3 rate-limited the request. Wait before retrying.",
            "unavailable": "AO3 is temporarily unavailable. Try again later.",
        }.get(result["error_kind"], f"AO3 returned HTTP {exc.code}.")
        result["error"] = detail

    except Exception as exc:
        result["error_kind"] = "network"
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def fetch_ao3_metadata_batch(items, delay=2.0, progress=None, should_continue=None, cookie_header=None):
    """
    Fetch metadata for many works with a mandatory delay between requests.

    AO3 rate-limits aggressive automated access and 429 bans can be
    temporary IP blocks, so callers looping over books must use this
    instead of calling fetch_ao3_metadata() in a tight loop.

    Args:
        items: iterable of (key, url) pairs; key is any hashable the
            caller chooses (e.g. a Calibre book id).
        delay: minimum seconds between request starts (default 2.0).
        progress: optional callable(key, result_dict) invoked after each
            fetch, so a worker thread can stream results to the UI.
        should_continue: optional callable returning False to cancel
            remaining lookups (e.g. user pressed Cancel).

    Returns dict mapping key -> result dict. Blocking; run it inside a
    QThread/worker, never on the GUI thread.
    """
    results = {}
    next_allowed = 0.0
    for key, url in items:
        if should_continue is not None and not should_continue():
            break
        wait = next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        results[key] = fetch_ao3_metadata(url, cookie_header=cookie_header)
        next_allowed = time.monotonic() + max(0.0, delay)
        if progress is not None:
            progress(key, results[key])
    return results
