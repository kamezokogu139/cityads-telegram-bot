import time
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, quote
import aiohttp
from config import CITYADS_AUTH_URL, CITYADS_API_BASE
import db

logger = logging.getLogger(__name__)

CLICK_DOMAIN = "yajgm.com"


# ── OAuth 2.0 (for v2 API) ──────────────────────────────────────────────────

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
            data = await resp.json()
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


# ── v2 API (OAuth X-Access-Token) ───────────────────────────────────────────

async def _v2_request(
    telegram_id: int,
    endpoint: str,
    *,
    params: dict | None = None,
) -> dict | str:
    token = await get_token(telegram_id)
    url = f"{CITYADS_API_BASE}/v2/{endpoint.lstrip('/')}"
    headers = {"X-Access-Token": token}

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        async with session.get(url, headers=headers, params=params) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"v2 API error ({resp.status}): {body[:200]}")
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                import json
                return json.loads(body)
            return body


# ── v1 XML API (remote_auth) ────────────────────────────────────────────────

async def _v1_request(
    telegram_id: int,
    endpoint: str,
    *,
    params: dict | None = None,
) -> str:
    remote_auth = await db.get_remote_auth(telegram_id)
    if not remote_auth:
        raise RuntimeError("remote_auth not set. Use /connect")

    url = f"{CITYADS_API_BASE}/xml/{endpoint.lstrip('/')}"
    p = dict(params) if params else {}
    p["remote_auth"] = remote_auth

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        async with session.get(url, params=p) as resp:
            body = await resp.text()
            if resp.status == 403:
                raise PermissionError("Access denied. Check your remote_auth key (/connect)")
            if resp.status >= 400:
                raise RuntimeError(f"v1 API error ({resp.status}): {body[:200]}")
            return body


# ── Offers (v2) ──────────────────────────────────────────────────────────────

async def get_my_offers(telegram_id: int, limit: int = 50) -> list[dict]:
    data = await _v2_request(
        telegram_id, "offers",
        params={"limit": limit, "user_has_offer": "true", "sort": "name", "sort_type": "asc"},
    )
    return _extract_offers(data)


async def get_offer(telegram_id: int, offer_id: str) -> dict:
    data = await _v2_request(telegram_id, f"offers/{offer_id}")
    if isinstance(data, dict):
        return data.get("offer", data)
    return {}


def _extract_offers(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        val = data.get("offer", data.get("offers", []))
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
    return []


# ── Offer links (v1 XML) ────────────────────────────────────────────────────

async def get_offer_links(telegram_id: int, offer_id: int | str) -> list[dict]:
    xml_body = await _v1_request(telegram_id, f"offer-links/{offer_id}")
    return _parse_offer_links_xml(xml_body)


def _parse_offer_links_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items = root.findall(".//items/item") or root.findall(".//data/items/item")
    result = []
    for item in items:
        entry = {}
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            entry["title"] = title_el.text
        default_el = item.find("is_default")
        if default_el is not None and default_el.text:
            entry["is_default"] = default_el.text.lower() in ("true", "1")
        link_el = item.find("deep_link")
        if link_el is not None and link_el.text:
            entry["deep_link"] = link_el.text
        if entry:
            result.append(entry)
    return result


# ── Link builders ────────────────────────────────────────────────────────────

def build_deeplink(base_link: str, target_url: str, sub1: str = "", sub2: str = "") -> str:
    sep = "&" if "?" in base_link else "?"
    params = {"url": target_url}
    if sub1:
        params["sub1"] = sub1
    if sub2:
        params["sub2"] = sub2
    return base_link + sep + urlencode(params, quote_via=quote)
