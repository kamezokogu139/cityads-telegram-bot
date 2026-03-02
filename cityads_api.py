import json
import time
import logging
from urllib.parse import urlencode, quote

import aiohttp
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


# ── Link builders ────────────────────────────────────────────────────────────

def build_deeplink(base_link: str, target_url: str, sub1: str = "", sub2: str = "") -> str:
    sep = "&" if "?" in base_link else "?"
    params = {"url": target_url}
    if sub1:
        params["sub1"] = sub1
    if sub2:
        params["sub2"] = sub2
    return base_link + sep + urlencode(params, quote_via=quote)
