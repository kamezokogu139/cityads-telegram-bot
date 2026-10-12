"""Resolve Shopee short / universal / shared-text links into a clean product URL.

Port of the partner C# guide (ShopeeUrlResolver). Flow:
  raw text -> extract URL -> validate Shopee domain -> follow redirects manually
  -> unwrap universal-link -> normalize path -> strip third-party tracking params.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import aiohttp

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SEC = 8
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Country TLDs where Shopee operates. Matches shopee.<tld>, www.shopee.<tld>,
# s.shopee.<tld> (short links), mall.shopee.<tld>, etc.
_SHOPEE_TLDS = (
    "vn", "co\\.id", "sg", "co\\.th", "com\\.my", "ph", "tw",
    "com\\.br", "com\\.mx", "cl", "com\\.co", "com",
)
_SHOPEE_HOST_RE = re.compile(
    r"^(?:[\w-]+\.)*shopee\.(?:" + "|".join(_SHOPEE_TLDS) + r")$", re.IGNORECASE
)
_SHOPEE_MAIN_HOST_RE = re.compile(
    r"^(?:www\.)?shopee\.(?:" + "|".join(_SHOPEE_TLDS) + r")$", re.IGNORECASE
)
# Global short-link domains that are not under shopee.<tld>
_SHOPEE_SHORT_HOSTS = {"shp.ee", "shope.ee"}

TRACKING_PARAMS = {
    "d_id", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "traffic_id", "aff_sid", "uls_trackid", "gads_t_sig", "__mobile__",
    "credential_token", "exp_group", "mmp_pid",
    "share_channel_code",  # added by the Shopee app "Share" button
}

_URL_RE = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
_HTTP_PREFIX_RE = re.compile(r"https?://", re.IGNORECASE)
_REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass
class ResolveResult:
    ok: bool
    full_url: str = ""
    error: str = ""
    redirects: int = 0


# ── Pure helpers ─────────────────────────────────────────────────────────────

def extract_url(text: str) -> str | None:
    """First http(s) URL found in arbitrary text (share text, caption, etc.)."""
    if not text:
        return None
    text = text.strip()
    match = _URL_RE.search(text)
    return match.group(0).rstrip(".,;!?") if match else None


def _host(url: str) -> str | None:
    try:
        host = urlparse(url).hostname
        return host.lower() if host else None
    except ValueError:
        return None


def _is_shopee_host(host: str | None) -> bool:
    if not host:
        return False
    return host in _SHOPEE_SHORT_HOSTS or bool(_SHOPEE_HOST_RE.match(host))


def _is_main_host(host: str | None) -> bool:
    """shopee.<tld> / www.shopee.<tld> — the final destination, not a short domain."""
    return bool(host) and bool(_SHOPEE_MAIN_HOST_RE.match(host))


def is_shopee_url(text: str) -> bool:
    """True if the text contains a URL on any Shopee domain."""
    url = extract_url(text)
    return _is_shopee_host(_host(url)) if url else False


def unwrap_universal_link(url: str) -> str | None:
    """Pull the real product URL out of a universal-link wrapper.

    Example: https://shopee.vn/universal-link?...&redir=https%3A%2F%2Fshopee.vn%2Fproduct%2F...
    Returns None if the URL is not a wrapper.
    """
    try:
        parsed = urlparse(url)
        # Prefer explicit redir= param when present
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in ("redir", "redirect", "url") and _is_main_host(_host(value)):
                return value
        # Fallback heuristic from the guide: second http(s):// in the decoded string
        decoded = unquote(url)
        prefixes = list(_HTTP_PREFIX_RE.finditer(decoded))
        if len(prefixes) > 1:
            candidate = extract_url(decoded[prefixes[1].start():])
            if candidate and _is_main_host(_host(candidate)):
                return candidate
    except ValueError:
        pass
    return None


def normalize_url(url: str) -> str:
    """http -> https; legacy /opaanlp/ product path -> /product/."""
    parsed = urlparse(url)
    if parsed.scheme.lower() == "http":
        parsed = parsed._replace(scheme="https")
    if parsed.path.startswith("/opaanlp/"):
        parsed = parsed._replace(path="/product/" + parsed.path[len("/opaanlp/"):])
    return urlunparse(parsed)


def clean_tracking_params(url: str) -> str:
    """Remove third-party tracking query params; keep everything else as is."""
    try:
        parsed = urlparse(url)
        kept = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        return urlunparse(parsed._replace(query=urlencode(kept, doseq=True), fragment=""))
    except ValueError:
        return url


# ── Network ──────────────────────────────────────────────────────────────────

async def resolve_full_link(raw: str, clean_tracking: bool = True) -> ResolveResult:
    """Resolve any Shopee link / share text to a clean full product URL."""
    url = extract_url(raw)
    if not url:
        return ResolveResult(ok=False, error="No URL found in text")

    host = _host(url)
    if not _is_shopee_host(host):
        return ResolveResult(ok=False, error=f"Domain '{host}' is not a Shopee domain")

    current = url
    hops = 0
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    try:
        # ssl=False matches the rest of the project (cityads_api.py); we only read
        # Location headers here, no credentials are sent.
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers,
            connector=aiohttp.TCPConnector(ssl=False),
        ) as session:
            while hops < MAX_REDIRECTS:
                if _is_main_host(_host(current)):
                    # Already on shopee.<tld>; only unwrap universal-link if needed
                    current = unwrap_universal_link(current) or current
                    break

                hops += 1
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in _REDIRECT_CODES:
                        location = resp.headers.get("Location")
                        if not location:
                            return ResolveResult(
                                ok=False, redirects=hops,
                                error=f"HTTP {resp.status} without Location header",
                            )
                        current = urljoin(current, location)
                    elif resp.status == 200:
                        current = unwrap_universal_link(current) or current
                        break
                    else:
                        return ResolveResult(
                            ok=False, redirects=hops,
                            error=f"Unexpected HTTP {resp.status} at {current[:80]}",
                        )
            else:
                return ResolveResult(
                    ok=False, redirects=hops,
                    error=f"Too many redirects (>{MAX_REDIRECTS})",
                )
    except TimeoutError:
        return ResolveResult(ok=False, redirects=hops, error="Timeout")
    except aiohttp.ClientError as e:
        logger.warning("Shopee resolve failed for %s: %s", url[:60], e)
        return ResolveResult(ok=False, redirects=hops, error=f"Network error: {e}")

    final_host = _host(current)
    if not _is_main_host(final_host):
        if _is_shopee_host(final_host):
            # e.g. s.shopee.vn/xxx -> shope.ee/error_page: invalid or expired short code
            return ResolveResult(
                ok=False, redirects=hops,
                error="Short link is invalid or expired (did not reach a product page)",
            )
        return ResolveResult(
            ok=False, redirects=hops,
            error=f"Resolved to non-Shopee host '{final_host}'",
        )

    current = normalize_url(current)
    if clean_tracking:
        current = clean_tracking_params(current)

    return ResolveResult(ok=True, full_url=current, redirects=hops)
