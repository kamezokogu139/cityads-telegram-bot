import asyncio
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, quote

import aiohttp
import requests
from config import CITYADS_AUTH_URL, CITYADS_API_BASE
import db

logger = logging.getLogger(__name__)


# ── OAuth 2.0 ────────────────────────────────────────────────────────────────

async def _fetch_new_token(client_id: str, client_secret: str) -> tuple[str, int]:
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        async with session.post(CITYADS_AUTH_URL, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"CityAds auth error ({resp.status}): {text}")
            text = await resp.text()
            data = json.loads(text)
            return data["access_token"], data["expires_in"]


async def get_token(telegram_id: int) -> str:
    cached, expires_at = await db.get_cached_token(telegram_id)
    if cached and time.time() < expires_at - 60:
        return cached

    creds = await db.get_credentials(telegram_id)
    if not creds:
        raise RuntimeError("Account not connected. Use /connect")

    client_id, client_secret = creds
    token, expires_in = await _fetch_new_token(client_id, client_secret)
    await db.save_token(telegram_id, token, time.time() + expires_in)
    return token


# ── v2 API request ───────────────────────────────────────────────────────────

async def _v2_request(
    telegram_id: int,
    endpoint: str,
    *,
    params: dict | None = None,
) -> dict | list:
    token = await get_token(telegram_id)
    url = f"{CITYADS_API_BASE}/v2/{endpoint.lstrip('/')}"
    headers = {"X-Access-Token": token}

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        async with session.get(url, headers=headers, params=params) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"API error ({resp.status}): {body[:200]}")
            return json.loads(body)


# ── Offers ───────────────────────────────────────────────────────────────────

async def get_my_offers(telegram_id: int, limit: int = 50) -> list[dict]:
    """Subscribed offers via /v2/offers/list (includes links)."""
    data = await _v2_request(
        telegram_id,
        "offers/list",
        params={
            "limit": limit,
            "user_has_offer": "true",
            "sort": "name",
            "sort_type": "asc",
        },
    )
    return _extract_offers(data, key="offers")


async def search_my_offers(telegram_id: int, query: str, limit: int = 20) -> list[dict]:
    """Search subscribed offers by name (API-side substring match, case-insensitive)."""
    data = await _v2_request(
        telegram_id,
        "offers/list",
        params={
            "name": query,
            "user_has_offer": "true",
            "limit": limit,
            "sort": "name",
            "sort_type": "asc",
        },
    )
    offers = _extract_offers(data, key="offers")
    return [o for o in offers if o.get("is_available")]


async def get_offer_with_links(telegram_id: int, offer_id: str) -> dict | None:
    """Single offer with its tracking links via /v2/offers/list?ids=..."""
    data = await _v2_request(
        telegram_id,
        "offers/list",
        params={"ids": offer_id},
    )
    offers = _extract_offers(data, key="offers")
    return offers[0] if offers else None


def _extract_offers(data, *, key: str = "offers") -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        val = data.get(key, [])
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
    return []


# ── Link shortener ────────────────────────────────────────────────────────────

SHORTLINK_URL = "https://cityads.com/saduka/shortLink"
SHORTLINK_TIMEOUT = 15
_shorten_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shorten")


def _shorten_sync(url: str) -> str:
    """Sync shortLink request — runs in thread, avoids aiogram event loop conflict."""
    try:
        resp = requests.post(
            SHORTLINK_URL,
            json={"urls": url},
            headers={"content-type": "application/json"},
            timeout=SHORTLINK_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("shortLink API error %s: %s", resp.status_code, resp.text[:100])
            return url
        data = resp.json()
        short = _extract_short_url(data)
        return short if short else url
    except Exception as e:
        logger.warning("shortLink failed for %s: %s", url[:50], e)
        return url


async def shorten_link(url: str) -> str:
    """Shorten URL via CityAds shortLink API. Uses thread to avoid event loop hang."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_shorten_executor, _shorten_sync, url)


def _extract_short_url(data: dict | list) -> str | None:
    """Extract shortened URL from API response.
    API returns: {"shortLink": {"original_url": "https://lnk.do/xxx"}}
    """
    if isinstance(data, str):
        return data if data.startswith("http") else None
    if isinstance(data, list) and data:
        item = data[0]
        return item if isinstance(item, str) and item.startswith("http") else _extract_short_url(item)
    if isinstance(data, dict):
        # CityAds format: {"shortLink": {"orig": "shortened"}}
        short_link = data.get("shortLink")
        if isinstance(short_link, dict) and short_link:
            return next((v for v in short_link.values() if isinstance(v, str) and v.startswith("http")), None)
        for key in ("urls", "short_url", "shortUrl", "short_link", "url", "link"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
            if isinstance(val, list) and val:
                return _extract_short_url(val[0])
    return None


# ── Link builders ────────────────────────────────────────────────────────────

def build_deeplink(base_link: str, target_url: str, sub1: str = "", sub2: str = "") -> str:
    sep = "&" if "?" in base_link else "?"
    params = {"url": target_url}
    if sub1:
        params["sub1"] = sub1
    if sub2:
        params["sub2"] = sub2
    return base_link + sep + urlencode(params, quote_via=quote)
