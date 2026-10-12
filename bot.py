import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import cityads_api as api
import db
import media_upload
import shopee_resolver
import seedance_api as sd
import comfy_worker_api as cw
from config import (
    BOT_TOKEN,
    SEEDANCE_MONTHLY_CREDITS,
    VIDEO_BACKEND,
    COMFY_WORKER_TASK_CREDITS_COST,
    COMFY_WORKER_JOB_POLL_INTERVAL_SEC,
    COMFY_WORKER_JOB_TIMEOUT_SEC,
    comfy_worker_configured,
    media_upload_configured,
    seedance_configured,
)
from seedance_models import (
    DEFAULT_MODEL_ID,
    MODE_ORDER,
    format_mode_instructions,
    format_modes_menu,
    get_mode,
    get_model,
    mode_label,
    parse_mode_choice,
)
import scenario_flow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# One active generation job per telegram user
_active_gen_jobs: set[int] = set()
_bg_tasks: set[asyncio.Task] = set()
MAX_MEDIA_BYTES = 45 * 1024 * 1024  # stay under Telegram Bot API ~50MB


# ── FSM states ───────────────────────────────────────────────────────────────

class ConnectFlow(StatesGroup):
    waiting_client_id = State()
    waiting_client_secret = State()


class GetLinksFlow(StatesGroup):
    waiting_search = State()


class DeeplinkFlow(StatesGroup):
    waiting_offer_id = State()
    waiting_url = State()


class GenerateFlow(StatesGroup):
    choosing_mode = State()
    waiting_data = State()
    choosing_video_settings = State()


VIDEO_QUALITY_PRESETS: dict[str, float] = {
    "fast": 0.5,
    "standard": 1.0,
    "hd": 1.8,
}

VIDEO_ASPECT_CHOICES: dict[str, str] = {
    "16:9": "16:9 (Widescreen)",
    "9:16": "9:16 (Portrait Widescreen)",
    "1:1": "1:1 (Square)",
    "4:3": "4:3 (Standard)",
    "3:4": "3:4 (Portrait Standard)",
}
_ASPECT_CALLBACK_PREFIX = "gen:aspect:"


def parse_aspect_callback(data: str) -> str | None:
    # ponytail: callback is gen:aspect:16:9 — rsplit(":") would keep only "9"
    if not (data or "").startswith(_ASPECT_CALLBACK_PREFIX):
        return None
    key = data[len(_ASPECT_CALLBACK_PREFIX):]
    return key if key in VIDEO_ASPECT_CHOICES else None


# ── Keyboards ────────────────────────────────────────────────────────────────

BTN_CONNECT = "🔗 Connect"
BTN_GENERATE = "🎬 Generate"
BTN_MY_OFFERS = "📋 My available offers"
BTN_CREATE_DEEPLINK = "🌐 Create deeplink"
BTN_SETTINGS = "⚙️ Settings"
BTN_BACK = "◀️ Back"
BTN_HELP = "❓ Help"
BTN_DISCONNECT = "🔌 Disconnect"

# Plain labels from keyboards cached before emoji restore
_BTN_ALIASES: dict[str, set[str]] = {
    BTN_CONNECT: {BTN_CONNECT, "Connect"},
    BTN_GENERATE: {BTN_GENERATE, "Generate"},
    BTN_MY_OFFERS: {BTN_MY_OFFERS, "My available offers"},
    BTN_CREATE_DEEPLINK: {BTN_CREATE_DEEPLINK, "Create deeplink"},
    BTN_SETTINGS: {BTN_SETTINGS, "Settings"},
    BTN_BACK: {BTN_BACK, "Back"},
    BTN_HELP: {BTN_HELP, "Help"},
    BTN_DISCONNECT: {BTN_DISCONNECT, "Disconnect"},
}


def _btn(label: str):
    return F.text.in_(_BTN_ALIASES[label])

def connect_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CONNECT)],
            [KeyboardButton(text=BTN_GENERATE)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GENERATE)],
            [KeyboardButton(text=BTN_MY_OFFERS)],
            [KeyboardButton(text=BTN_CREATE_DEEPLINK)],
            [KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def settings_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_DISCONNECT)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _inline_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


# ── Auth helper ──────────────────────────────────────────────────────────────

async def require_auth(event: Message | CallbackQuery) -> bool:
    uid = event.from_user.id
    if await db.is_connected(uid):
        return True
    text = "Account not connected.\nUse the Connect button to link your CityAds account."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


# ── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    if await db.is_connected(message.from_user.id):
        await message.answer("Welcome back!", reply_markup=main_menu_kb())
    else:
        await message.answer(
            "Hi! I'm a CityAds link bot.\n\n"
            "Use the <b>Connect</b> button to link your CityAds account.\n\n"
            "You'll need:\n"
            "• <b>client_id</b>\n"
            "• <b>client_secret</b>\n\n"
            "Get them at: https://cityads.com/publisher/api",
            parse_mode="HTML",
            reply_markup=connect_kb(),
        )


# ── Connect (2 steps: client_id → client_secret) ──────────────────────────────

@router.message(Command("connect"))
@router.message(_btn(BTN_CONNECT))
async def cmd_connect(message: Message, state: FSMContext):
    if await state.get_state() and "ConnectFlow" in str(await state.get_state()):
        return
    if await db.is_connected(message.from_user.id):
        await message.answer("Already connected.", reply_markup=main_menu_kb())
        return
    await state.set_state(ConnectFlow.waiting_client_id)
    await message.answer(
        "Step 1/2: Send your <b>client_id</b>.\n\n"
        "https://cityads.com/publisher/api",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(ConnectFlow.waiting_client_id)
async def process_client_id(message: Message, state: FSMContext):
    client_id = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(client_id=client_id)
    await state.set_state(ConnectFlow.waiting_client_secret)
    await message.answer(
        "Got client_id (message deleted).\n\n"
        "Step 2/2: Send your <b>client_secret</b>.",
        parse_mode="HTML",
    )


@router.message(ConnectFlow.waiting_client_secret)
async def process_client_secret(message: Message, state: FSMContext):
    client_secret = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    client_id = data["client_id"]
    status_msg = await message.answer("Verifying OAuth credentials...")

    try:
        token, expires_in = await api._fetch_new_token(client_id, client_secret)
    except Exception as e:
        await status_msg.edit_text(
            f"OAuth failed:\n<code>{e}</code>\n\nTry again using the Connect button.",
            parse_mode="HTML",
        )
        await message.answer("Use the Connect button to try again.", reply_markup=connect_kb())
        await state.clear()
        return

    uid = message.from_user.id
    await db.save_credentials(uid, client_id, client_secret)
    await db.save_token(uid, token, time.time() + expires_in)
    await state.clear()

    await status_msg.edit_text(
        "CityAds account connected!\n\n"
        "OAuth 2.0 credentials verified and encrypted.\n"
        "Messages with secrets deleted.",
    )
    await message.answer("Choose an action:", reply_markup=main_menu_kb())


# ── Disconnect ───────────────────────────────────────────────────────────────

@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message):
    await db.delete_user(message.from_user.id)
    await message.answer(
        "Disconnected. All data deleted.",
        reply_markup=connect_kb(),
    )


# ── Main menu ────────────────────────────────────────────────────────────────

@router.message(_btn(BTN_BACK))
async def msg_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Choose an action:", reply_markup=await _reply_home_kb(message.from_user.id))


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Choose an action:")


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not await require_auth(message):
        return
    await message.answer("Choose an action:", reply_markup=main_menu_kb())


# ── Settings (Help, Disconnect) ───────────────────────────────────────────────

@router.message(_btn(BTN_SETTINGS))
async def msg_settings(message: Message):
    if not await require_auth(message):
        return
    await message.answer(
        "<b>Settings</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


@router.message(_btn(BTN_HELP))
async def msg_help(message: Message):
    if not await require_auth(message):
        return
    await message.answer(
        "<b>Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start — Restart bot, show main menu\n"
        "/menu — Open main menu\n"
        "/disconnect — Unlink CityAds account and delete data\n"
        "/help — This help\n\n"
        "<b>Main menu:</b>\n"
        "• Generate — video via Seedance 2.0 / Comfy\n"
        "• Scenario → video — text and/or photo, then LTX video\n"
        "• My available offers — search by ID or name, get tracking links\n"
        "• Create deeplink — search offer, enter target URL, get deeplink",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


@router.message(_btn(BTN_DISCONNECT))
async def msg_disconnect(message: Message):
    if not await require_auth(message):
        return
    await db.delete_user(message.from_user.id)
    await message.answer(
        "Disconnected. All data deleted.",
        reply_markup=connect_kb(),
    )


# ── My offers: search by ID or name → select → show tracking links ───────────

async def _start_my_offers(target: Message | CallbackQuery, state: FSMContext):
    await state.set_state(GetLinksFlow.waiting_search)
    text = (
        "<b>My available offers</b>\n\n"
        "Enter the <b>offer ID</b> or <b>name</b> (or part of it)."
    )
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.edit_text(text, parse_mode="HTML")
    else:
        await target.answer(text, parse_mode="HTML")


@router.message(_btn(BTN_MY_OFFERS))
async def msg_my_offers(message: Message, state: FSMContext):
    if not await require_auth(message):
        return
    await _start_my_offers(message, state)


@router.callback_query(F.data == "search_again")
async def cb_search_again(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await _start_my_offers(callback, state)


@router.message(GetLinksFlow.waiting_search)
async def process_search_offers(message: Message, state: FSMContext):
    query = message.text.strip()
    uid = message.from_user.id

    if query.isdigit():
        await state.clear()
        status_msg = await message.answer("Fetching links...")
        try:
            offer = await api.get_offer_with_links(uid, query)
            if not offer:
                await status_msg.edit_text(
                    f"No offer found with ID {query}.",
                    reply_markup=_search_back_kb(),
                )
                return
            await _send_offer_links(status_msg, offer, query)
        except Exception as e:
            await status_msg.edit_text(f"Error: {e}", reply_markup=_inline_back_kb())
        return

    if len(query) < 2:
        await message.answer("Enter at least 2 characters.")
        return

    await state.clear()
    status_msg = await message.answer("Searching...")

    try:
        offers = await api.search_my_offers(uid, query)
        if not offers:
            await status_msg.edit_text(
                f"No active offers found for <b>{query}</b>.",
                parse_mode="HTML",
                reply_markup=_search_back_kb(),
            )
            return

        rows = []
        for o in offers[:15]:
            oid = str(o.get("id", ""))
            name = o.get("name", f"Offer {oid}")
            label = f"{name} ({oid})" if len(name) <= 35 else f"{name[:32]}… ({oid})"
            rows.append([InlineKeyboardButton(
                text=label, callback_data=f"links:{oid}"
            )])
        rows.append([InlineKeyboardButton(text="Search again", callback_data="search_again")])
        rows.append([InlineKeyboardButton(text="Main menu", callback_data="main_menu")])

        await status_msg.edit_text(
            f"Results for <b>{query}</b>:\n\nSelect an offer to get tracking links.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as e:
        await status_msg.edit_text(f"Error: {e}", reply_markup=_inline_back_kb())


@router.callback_query(F.data.startswith("links:"))
async def cb_show_links(callback: CallbackQuery):
    if not await require_auth(callback):
        return
    await callback.answer()

    offer_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    await callback.message.edit_text("Fetching links...")

    try:
        offer = await api.get_offer_with_links(uid, offer_id)
        if not offer:
            await callback.message.edit_text(
                f"No offer found with ID {offer_id}.", reply_markup=_search_back_kb()
            )
            return
        await _send_offer_links(callback.message, offer, offer_id)
    except Exception as e:
        await callback.message.edit_text(f"Error: {e}", reply_markup=_inline_back_kb())


def _search_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Search again", callback_data="search_again")],
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


async def _send_offer_links(msg: Message, offer: dict, offer_id: str):
    offer_name = offer.get("name", "")
    links = offer.get("links", [])

    if not links:
        await msg.edit_text(
            f"No links found for offer {offer_id}.", reply_markup=_search_back_kb()
        )
        return

    await msg.edit_text("Shortening links...", reply_markup=_search_back_kb())
    short_tasks = [api.shorten_link(link.get("deep_link", "")) for link in links]
    shortened = await asyncio.gather(*short_tasks, return_exceptions=True)

    header = f"<b>{offer_name}</b> (ID: {offer_id})\n\n" if offer_name else ""
    text = header
    shown = 0
    for i, link in enumerate(links):
        name = link.get("name", f"Link {i}")
        orig = link.get("deep_link", "—")
        short = shortened[i] if i < len(shortened) and not isinstance(shortened[i], Exception) else orig
        display = short if isinstance(short, str) else orig
        is_default = link.get("is_default", False)
        star = "(default) " if is_default else ""
        line = f"{star}<b>{name}</b> — <code>{display}</code>\n\n"
        if len(text) + len(line) > 3900:
            text += f"… and {len(links) - shown} more links"
            break
        text += line
        shown += 1

    await msg.edit_text(text, parse_mode="HTML", reply_markup=_search_back_kb())


# ── Create deeplink (search offer → get base link → enter URL) ─────────────────

async def _start_create_deeplink(target: Message | CallbackQuery, state: FSMContext):
    await state.set_state(DeeplinkFlow.waiting_offer_id)
    text = (
        "<b>Create deeplink</b>\n\n"
        "Enter the <b>offer ID</b> or <b>name</b> (or part of it)."
    )
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.edit_text(text, parse_mode="HTML")
    else:
        await target.answer(text, parse_mode="HTML")


@router.message(_btn(BTN_CREATE_DEEPLINK))
async def msg_create_deeplink(message: Message, state: FSMContext):
    if not await require_auth(message):
        return
    await _start_create_deeplink(message, state)


@router.callback_query(F.data == "dl_search_again")
async def cb_dl_search_again(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await _start_create_deeplink(callback, state)


def _deeplink_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Search again", callback_data="dl_search_again")],
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


async def _dl_set_base_and_ask_url(status_msg: Message, state: FSMContext, base: str):
    await state.set_state(DeeplinkFlow.waiting_url)
    await state.update_data(dl_base_link=base)
    await status_msg.edit_text(
        f"Base link: <code>{base}</code>\n\n"
        "Now send the <b>target URL</b> for the deeplink.\n"
        "<i>Shopee: short links (s.shopee.vn, shp.ee) and share text are OK — "
        "they will be resolved to the full product URL.</i>",
        parse_mode="HTML",
    )


@router.message(DeeplinkFlow.waiting_offer_id)
async def process_dl_offer_id(message: Message, state: FSMContext):
    query = message.text.strip()
    uid = message.from_user.id

    if query.isdigit():
        status_msg = await message.answer("Fetching offer links...")
        try:
            offer = await api.get_offer_with_links(uid, query)
            if not offer:
                await status_msg.edit_text(
                    f"No offer found with ID {query}.", reply_markup=_deeplink_back_kb()
                )
                return

            links = offer.get("links", [])
            if not links:
                await status_msg.edit_text(
                    f"No links for offer {query}.", reply_markup=_deeplink_back_kb()
                )
                return

            default_link = next((l for l in links if l.get("is_default")), links[0])
            base = default_link.get("deep_link", "")
            if not base:
                await status_msg.edit_text("No base link found.", reply_markup=_deeplink_back_kb())
                return

            await _dl_set_base_and_ask_url(status_msg, state, base)
        except Exception as e:
            await status_msg.edit_text(f"Error: {e}", reply_markup=_deeplink_back_kb())
            await state.clear()
        return

    if len(query) < 2:
        await message.answer("Enter at least 2 characters.")
        return

    await state.clear()
    status_msg = await message.answer("Searching...")

    try:
        offers = await api.search_my_offers(uid, query)
        if not offers:
            await status_msg.edit_text(
                f"No active offers found for <b>{query}</b>.",
                parse_mode="HTML",
                reply_markup=_deeplink_back_kb(),
            )
            return

        rows = []
        for o in offers[:15]:
            oid = str(o.get("id", ""))
            name = o.get("name", f"Offer {oid}")
            label = f"{name} ({oid})" if len(name) <= 35 else f"{name[:32]}… ({oid})"
            rows.append([InlineKeyboardButton(
                text=label, callback_data=f"dl_select:{oid}"
            )])
        rows.append([InlineKeyboardButton(text="Search again", callback_data="dl_search_again")])
        rows.append([InlineKeyboardButton(text="Main menu", callback_data="main_menu")])

        await status_msg.edit_text(
            f"Results for <b>{query}</b>:\n\nSelect an offer to create a deeplink.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as e:
        await status_msg.edit_text(f"Error: {e}", reply_markup=_deeplink_back_kb())


@router.callback_query(F.data.startswith("dl_select:"))
async def cb_dl_select_offer(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()

    offer_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    await callback.message.edit_text("Fetching offer links...")

    try:
        offer = await api.get_offer_with_links(uid, offer_id)
        if not offer:
            await callback.message.edit_text(
                f"No offer found with ID {offer_id}.", reply_markup=_deeplink_back_kb()
            )
            return

        links = offer.get("links", [])
        if not links:
            await callback.message.edit_text(
                f"No links for offer {offer_id}.", reply_markup=_deeplink_back_kb()
            )
            return

        default_link = next((l for l in links if l.get("is_default")), links[0])
        base = default_link.get("deep_link", "")
        if not base:
            await callback.message.edit_text("No base link found.", reply_markup=_deeplink_back_kb())
            return

        await _dl_set_base_and_ask_url(callback.message, state, base)
    except Exception as e:
        await callback.message.edit_text(f"Error: {e}", reply_markup=_deeplink_back_kb())


@router.message(DeeplinkFlow.waiting_url)
async def process_deeplink_url(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    resolved_note = ""

    if shopee_resolver.is_shopee_url(raw):
        # Shopee: short link / universal-link / share text -> clean product URL
        status_msg = await message.answer("Resolving Shopee link...")
        res = await shopee_resolver.resolve_full_link(raw, clean_tracking=True)
        if res.ok:
            url = res.full_url
            resolved_note = f"<b>Resolved Shopee link:</b>\n<code>{url}</code>\n\n"
        else:
            # Fallback: keep the original URL so the webmaster still gets a link
            url = shopee_resolver.extract_url(raw) or raw
            resolved_note = (
                f"⚠️ Could not resolve Shopee link ({res.error}). "
                f"Using original URL.\n\n"
            )
        await status_msg.delete()
    else:
        url = raw
        if not url.startswith(("http://", "https://")):
            await message.answer("Send a valid URL starting with http:// or https://")
            return

    data = await state.get_data()
    base = data.get("dl_base_link", "")
    await state.clear()
    if not base:
        await message.answer("Session expired. Start again.", reply_markup=main_menu_kb())
        return

    deeplink = api.build_deeplink(base, url)
    await message.answer("Shortening link...", reply_markup=main_menu_kb())
    try:
        short_link = await api.shorten_link(deeplink)
        result = short_link if short_link != deeplink else deeplink
    except Exception as e:
        logger.exception("shorten_link error: %s", e)
        result = deeplink
    await message.answer(
        f"{resolved_note}<b>Your deeplink:</b>\n\n<code>{result}</code>",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


# ── Generate video (Seedance 2.0) ─────────────────────────────────────────────

async def _reply_home_kb(user_id: int) -> ReplyKeyboardMarkup:
    if await db.is_connected(user_id):
        return main_menu_kb()
    return connect_kb()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")],
    ])


def _modes_inline_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{i}. {label}", callback_data=f"gen:mode:{mode}")]
        for i, (mode, label) in enumerate(MODE_ORDER, start=1)
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _data_kb(*, show_done: bool) -> InlineKeyboardMarkup:
    rows = []
    if show_done:
        rows.append([InlineKeyboardButton(text="Done", callback_data="gen:done")])
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _video_quality_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ Fast", callback_data="gen:quality:fast"),
            InlineKeyboardButton(text="✅ Standard", callback_data="gen:quality:standard"),
            InlineKeyboardButton(text="🎬 HD", callback_data="gen:quality:hd"),
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")],
    ])


def _video_aspect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="16:9", callback_data=f"{_ASPECT_CALLBACK_PREFIX}16:9"),
            InlineKeyboardButton(text="9:16", callback_data=f"{_ASPECT_CALLBACK_PREFIX}9:16"),
            InlineKeyboardButton(text="1:1", callback_data=f"{_ASPECT_CALLBACK_PREFIX}1:1"),
        ],
        [
            InlineKeyboardButton(text="4:3", callback_data=f"{_ASPECT_CALLBACK_PREFIX}4:3"),
            InlineKeyboardButton(text="3:4", callback_data=f"{_ASPECT_CALLBACK_PREFIX}3:4"),
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")],
    ])


def _mode_needs_upload(mode_id: str) -> bool:
    return mode_id in ("i2v", "r2v")


def _format_credits(remaining: int) -> str:
    return f"Credits remaining: {remaining} / {SEEDANCE_MONTHLY_CREDITS}"


async def _begin_data_collection(target: Message, state: FSMContext, mode_id: str, user_id: int | None = None):
    if mode_id == "scenario":
        uid = user_id or (target.from_user.id if target.from_user else target.chat.id)
        await scenario_flow.begin_scenario(target, state, uid)
        return
    mode = get_mode(mode_id)
    if not mode:
        return
    await state.update_data(
        gen_mode_id=mode_id,
        gen_model_id=DEFAULT_MODEL_ID,
        gen_prompt="",
        gen_image_paths=[],
        gen_video_paths=[],
        gen_audio_paths=[],
    )
    await state.set_state(GenerateFlow.waiting_data)
    await target.answer(
        format_mode_instructions(mode.id),
        parse_mode="HTML",
        reply_markup=_data_kb(show_done=mode.id != "t2v"),
    )


@router.message(Command("generate"))
@router.message(_btn(BTN_GENERATE))
async def cmd_generate(message: Message, state: FSMContext):
    if message.from_user.id in _active_gen_jobs:
        await message.answer("A generation is already running. Please wait.")
        return
    if VIDEO_BACKEND == "seedance":
        if not seedance_configured():
            await message.answer(
                "Seedance API is not configured on the server.\n"
                "Ask the admin to set SEEDANCE_API_KEY in .env"
            )
            return
    elif VIDEO_BACKEND == "comfy":
        if not comfy_worker_configured():
            await message.answer(
                "ComfyUI worker is not configured.\n"
                "Ask the admin to set COMFY_WORKER_BASE_URL and COMFY_WORKER_API_TOKEN in .env"
            )
            return
    elif VIDEO_BACKEND == "comfy_poll":
        # Polling backend: бот создаёт job в БД на VPS, а Windows worker
        # периодически забирает job и выкладывает результат обратно.
        pass
    else:
        await message.answer(f"Unknown VIDEO_BACKEND: {VIDEO_BACKEND}")
        return
    remaining = await db.get_user_credits(message.from_user.id)
    if VIDEO_BACKEND == "seedance":
        if remaining <= 0:
            await message.answer(
                f"No credits left this month.\n"
                f"Your allowance resets to {SEEDANCE_MONTHLY_CREDITS} on the 1st (UTC)."
            )
            return
    else:
        if COMFY_WORKER_TASK_CREDITS_COST > 0 and remaining < COMFY_WORKER_TASK_CREDITS_COST:
            await message.answer(
                "Not enough credits to start generation (comfy backend).\n"
                f"Need {COMFY_WORKER_TASK_CREDITS_COST}, have {remaining}.\n"
                f"Allowance resets to {SEEDANCE_MONTHLY_CREDITS} on the 1st (UTC)."
            )
            return
    await state.clear()
    await state.set_state(GenerateFlow.choosing_mode)
    await message.answer(
        f"{format_modes_menu()}\n\n{_format_credits(remaining)}",
        parse_mode="HTML",
        reply_markup=_modes_inline_kb(),
    )


@router.callback_query(F.data == "gen:cancel")
async def cb_gen_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    _active_gen_jobs.discard(callback.from_user.id)
    await state.clear()
    kb = await _reply_home_kb(callback.from_user.id)
    try:
        await callback.message.edit_text("Cancelled.")
    except Exception:
        pass
    await callback.message.answer("Choose an action:", reply_markup=kb)


@router.message(GenerateFlow.choosing_mode)
async def process_gen_mode_text(message: Message, state: FSMContext):
    mode = parse_mode_choice(message.text or "")
    if not mode:
        await message.answer(
            "Send a mode number (1-4).\n\n" + format_modes_menu(),
            parse_mode="HTML",
            reply_markup=_modes_inline_kb(),
        )
        return
    if _mode_needs_upload(mode) and not media_upload_configured():
        await message.answer(
            "This mode requires S3 storage.\n"
            "Ask the admin to set S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, "
            "S3_PUBLIC_BASE_URL in .env.\n\n"
            "Text to Video works without S3.",
            reply_markup=_modes_inline_kb(),
        )
        return
    await _begin_data_collection(message, state, mode, user_id=message.from_user.id)


@router.callback_query(GenerateFlow.choosing_mode, F.data.startswith("gen:mode:"))
async def cb_gen_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode_id = callback.data.split(":")[-1]
    mode = get_mode(mode_id)
    if not mode:
        return
    if _mode_needs_upload(mode_id) and not media_upload_configured():
        await callback.message.answer(
            "This mode requires S3 storage (see .env.example).",
            reply_markup=_modes_inline_kb(),
        )
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _begin_data_collection(
        callback.message, state, mode_id, user_id=callback.from_user.id
    )


async def _save_telegram_file(bot: Bot, file_id: str, suffix: str) -> str:
    tg_file = await bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > MAX_MEDIA_BYTES:
        raise ValueError("File is too large (max ~45 MB).")
    fd, path = tempfile.mkstemp(prefix="sd_", suffix=suffix)
    os.close(fd)
    await bot.download_file(tg_file.file_path, destination=path)
    return path


@router.message(GenerateFlow.waiting_data, F.text)
async def process_gen_text(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("Send a text prompt.")
        return
    await state.update_data(gen_prompt=prompt)
    data = await state.get_data()
    mode = get_mode(data.get("gen_mode_id", ""))
    if not mode:
        await state.clear()
        await message.answer("Session expired.", reply_markup=await _reply_home_kb(message.from_user.id))
        return
    if mode.id == "t2v":
        await _start_generation(message, state)
        return
    await message.answer("Prompt saved. Send media files, then press Done.")


@router.message(GenerateFlow.waiting_data, F.photo)
async def process_gen_photo(message: Message, state: FSMContext):
    await _maybe_save_caption_as_prompt(message, state)
    photo = message.photo[-1]
    try:
        path = await _save_telegram_file(message.bot, photo.file_id, ".jpg")
    except ValueError as e:
        await message.answer(str(e))
        return
    await _add_image(message, state, path)


@router.message(GenerateFlow.waiting_data, F.document)
async def process_gen_document(message: Message, state: FSMContext):
    doc = message.document
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    try:
        if mime.startswith("image/"):
            suffix = Path(doc.file_name or "image.jpg").suffix or ".jpg"
            path = await _save_telegram_file(message.bot, doc.file_id, suffix)
            await _add_image(message, state, path)
            return
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".webm")):
            suffix = Path(doc.file_name or "video.mp4").suffix or ".mp4"
            path = await _save_telegram_file(message.bot, doc.file_id, suffix)
            await _add_video(message, state, path)
            return
        if mime.startswith("audio/") or name.endswith((".mp3", ".wav", ".m4a", ".ogg")):
            suffix = Path(doc.file_name or "audio.mp3").suffix or ".mp3"
            path = await _save_telegram_file(message.bot, doc.file_id, suffix)
            await _add_audio(message, state, path)
            return
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("Send an image, video, or audio file.")


@router.message(GenerateFlow.waiting_data, F.video)
async def process_gen_video(message: Message, state: FSMContext):
    await _maybe_save_caption_as_prompt(message, state)
    video = message.video
    try:
        path = await _save_telegram_file(message.bot, video.file_id, ".mp4")
    except ValueError as e:
        await message.answer(str(e))
        return
    await _add_video(message, state, path)


@router.message(GenerateFlow.waiting_data, F.audio)
async def process_gen_audio(message: Message, state: FSMContext):
    audio = message.audio
    try:
        path = await _save_telegram_file(message.bot, audio.file_id, ".mp3")
    except ValueError as e:
        await message.answer(str(e))
        return
    await _add_audio(message, state, path)


async def _add_image(message: Message, state: FSMContext, path: str):
    data = await state.get_data()
    mode = get_mode(data.get("gen_mode_id", ""))
    if not mode or mode.id == "t2v":
        os.unlink(path)
        await message.answer("This mode does not accept images.")
        return
    paths = list(data.get("gen_image_paths") or [])
    if mode.max_images and len(paths) >= mode.max_images:
        os.unlink(path)
        await message.answer(f"Maximum {mode.max_images} images.")
        return
    paths.append(path)
    await state.update_data(gen_image_paths=paths)
    data = await state.get_data()
    prompt_note = ""
    if (data.get("gen_prompt") or "").strip():
        prompt_note = "\nPrompt saved."
    await message.answer(
        f"Image saved ({len(paths)}). Send more or press Done.{prompt_note}",
        reply_markup=_data_kb(show_done=True),
    )


async def _add_video(message: Message, state: FSMContext, path: str):
    data = await state.get_data()
    mode = get_mode(data.get("gen_mode_id", ""))
    if not mode or mode.id != "r2v":
        os.unlink(path)
        await message.answer("Only Reference to Video accepts reference videos.")
        return
    paths = list(data.get("gen_video_paths") or [])
    if mode.max_videos and len(paths) >= mode.max_videos:
        os.unlink(path)
        await message.answer(f"Maximum {mode.max_videos} videos.")
        return
    paths.append(path)
    await state.update_data(gen_video_paths=paths)
    await message.answer(f"Video saved ({len(paths)}). Send more or press Done.")


async def _add_audio(message: Message, state: FSMContext, path: str):
    data = await state.get_data()
    mode = get_mode(data.get("gen_mode_id", ""))
    if not mode or mode.id != "r2v":
        os.unlink(path)
        await message.answer("Only Reference to Video accepts reference audio.")
        return
    paths = list(data.get("gen_audio_paths") or [])
    if mode.max_audios and len(paths) >= mode.max_audios:
        os.unlink(path)
        await message.answer(f"Maximum {mode.max_audios} audio files.")
        return
    paths.append(path)
    await state.update_data(gen_audio_paths=paths)
    await message.answer(f"Audio saved ({len(paths)}). Send more or press Done.")


@router.callback_query(GenerateFlow.waiting_data, F.data == "gen:done")
async def cb_gen_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if VIDEO_BACKEND == "comfy_poll":
        await state.set_state(GenerateFlow.choosing_video_settings)
        await callback.message.answer(
            "Choose quality:\n"
            "⚡ Fast — 0.5 MP\n"
            "✅ Standard — 1.0 MP\n"
            "🎬 HD — 1.8 MP",
            reply_markup=_video_quality_kb(),
        )
        return
    await _start_generation(callback.message, state, from_callback=True)


@router.callback_query(GenerateFlow.choosing_video_settings, F.data.startswith("gen:quality:"))
async def cb_gen_quality(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    quality = callback.data.rsplit(":", 1)[-1]
    if quality not in VIDEO_QUALITY_PRESETS:
        await callback.message.answer("Unknown quality preset.", reply_markup=_video_quality_kb())
        return
    await state.update_data(gen_quality=quality)
    await callback.message.answer(
        "Choose aspect ratio:",
        reply_markup=_video_aspect_kb(),
    )


@router.callback_query(GenerateFlow.choosing_video_settings, F.data.startswith(_ASPECT_CALLBACK_PREFIX))
async def cb_gen_aspect(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    aspect_key = parse_aspect_callback(callback.data or "")
    if not aspect_key:
        await callback.message.answer("Unknown aspect ratio.", reply_markup=_video_aspect_kb())
        return
    await state.update_data(gen_aspect_key=aspect_key)
    await _start_generation(callback.message, state, from_callback=True)


@router.message(GenerateFlow.waiting_data)
async def process_gen_data_fallback(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = get_mode(data.get("gen_mode_id", ""))
    if mode and mode.id == "t2v":
        await message.answer("Send a text prompt.")
    else:
        await message.answer(
            "Send a text prompt and media files, then press Done.",
            reply_markup=_data_kb(show_done=True),
        )


async def _maybe_save_caption_as_prompt(message: Message, state: FSMContext):
    caption = (message.caption or "").strip()
    if not caption:
        return
    data = await state.get_data()
    if not (data.get("gen_prompt") or "").strip():
        await state.update_data(gen_prompt=caption)


async def _start_generation(
    message: Message,
    state: FSMContext,
    *,
    from_callback: bool = False,
):
    data = await state.get_data()
    user_id = message.from_user.id if message.from_user else message.chat.id
    image_paths = list(data.get("gen_image_paths") or [])
    video_paths = list(data.get("gen_video_paths") or [])
    audio_paths = list(data.get("gen_audio_paths") or [])

    mode = get_mode(data.get("gen_mode_id", ""))
    model = get_model(data.get("gen_model_id", DEFAULT_MODEL_ID))
    prompt = (data.get("gen_prompt") or "").strip()

    if user_id in _active_gen_jobs:
        await message.answer("A generation is already running. Please wait.")
        return

    if not mode or not model:
        _cleanup_paths(image_paths, video_paths, audio_paths)
        await state.clear()
        await message.answer(
            "Session expired. Start again with Generate.",
            reply_markup=await _reply_home_kb(user_id),
        )
        return

    if not prompt:
        await message.answer(
            "Please send a text prompt describing the video, then press Done.",
            reply_markup=_data_kb(show_done=True),
        )
        return

    if mode.id == "i2v" and not image_paths:
        await message.answer(
            "Image to Video requires at least one image. Send an image, then press Done.",
            reply_markup=_data_kb(show_done=True),
        )
        return

    if mode.id == "r2v":
        if VIDEO_BACKEND in ("comfy", "comfy_poll"):
            if not image_paths:
                await message.answer(
                    "Reference to Video (comfy backend) requires at least one image.",
                    reply_markup=_data_kb(show_done=True),
                )
                return
        elif not image_paths and not video_paths:
            await message.answer(
                "Reference to Video requires at least one image or video.",
                reply_markup=_data_kb(show_done=True),
            )
            return

    remaining = await db.get_user_credits(user_id)
    # Seedance backend requires credits gating. For the comfy backend we can
    # optionally charge a small fixed amount to prevent spam.
    if VIDEO_BACKEND == "seedance":
        if remaining <= 0:
            _cleanup_paths(image_paths, video_paths, audio_paths)
            await state.clear()
            await message.answer(
                f"No credits left this month.\n"
                f"Your allowance resets to {SEEDANCE_MONTHLY_CREDITS} on the 1st (UTC).",
                reply_markup=await _reply_home_kb(user_id),
            )
            return
    else:
        if remaining < COMFY_WORKER_TASK_CREDITS_COST:
            _cleanup_paths(image_paths, video_paths, audio_paths)
            await state.clear()
            await message.answer(
                "Not enough credits to start generation.\n"
                f"Need {COMFY_WORKER_TASK_CREDITS_COST}, have {remaining}.\n"
                f"Allowance resets to {SEEDANCE_MONTHLY_CREDITS} on the 1st (UTC).",
                reply_markup=await _reply_home_kb(user_id),
            )
            return

    await state.clear()

    video_aspect_ratio: str | None = None
    video_megapixels: float | None = None
    if VIDEO_BACKEND == "comfy_poll":
        quality = str(data.get("gen_quality") or "standard")
        aspect_key = str(data.get("gen_aspect_key") or "16:9")
        video_megapixels = VIDEO_QUALITY_PRESETS.get(quality, 1.0)
        video_aspect_ratio = VIDEO_ASPECT_CHOICES.get(
            aspect_key,
            VIDEO_ASPECT_CHOICES["16:9"],
        )

    _active_gen_jobs.add(user_id)
    status_msg = await message.answer("Generation in progress.")
    if from_callback:
        try:
            await status_msg.edit_text("Generation in progress.")
        except Exception:
            pass

    task = asyncio.create_task(
        _run_generation(
            bot=message.bot,
            chat_id=message.chat.id,
            user_id=user_id,
            status_message_id=status_msg.message_id,
            mode_id=mode.id,
            model_id=model.id,
            prompt=prompt,
            image_paths=image_paths,
            video_paths=video_paths,
            audio_paths=audio_paths,
            aspect_ratio=video_aspect_ratio,
            megapixels=video_megapixels,
        )
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _cleanup_paths(
    image_paths: list[str],
    video_paths: list[str] | None = None,
    audio_paths: list[str] | None = None,
):
    for paths in (image_paths, video_paths or [], audio_paths or []):
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


async def _run_generation(
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
    status_message_id: int,
    mode_id: str,
    model_id: str,
    prompt: str,
    image_paths: list[str],
    video_paths: list[str],
    audio_paths: list[str],
    aspect_ratio: str | None = None,
    megapixels: float | None = None,
):
    mode = get_mode(mode_id)
    model = get_model(model_id)
    home_kb = await _reply_home_kb(user_id)
    poll_job_id: str | None = None
    try:
        if not mode or not model:
            raise sd.SeedanceError("Unknown mode or model.")

        image_urls: list[str] = []
        for path in image_paths:
            image_urls.append(await media_upload.upload_file(path))

        # Seedance использует видео/аудио, а comfy/comfy_poll пока только стартовую картинку
        # (для i2v/r2v) или промпт (для t2v).
        video_urls: list[str] = []
        audio_urls: list[str] = []
        if VIDEO_BACKEND == "seedance":
            for path in video_paths:
                video_urls.append(await media_upload.upload_file(path))
            for path in audio_paths:
                audio_urls.append(await media_upload.upload_file(path))

        status_labels = {
            "queued": "Queued",
            "generating": "Generating video",
            "completed": "Completed",
            "failed": "Failed",
        }

        async def on_task_created(_task_id: str, cost: int):
            remaining = await db.get_user_credits(user_id)
            if cost > remaining:
                raise sd.SeedanceError(
                    f"Not enough credits this month (need {cost}, have {remaining})."
                )
            wait_msg = (
                "Usually takes 3–8 minutes. Please wait."
                if VIDEO_BACKEND == "seedance"
                else "Local generation can take several minutes. Please wait."
            )
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=(
                        f"Generation started. Cost: {cost} credits.\n"
                        f"{_format_credits(remaining)}\n"
                        + wait_msg
                    ),
                )
            except Exception:
                pass

        async def on_status(status: str, _task: dict):
            label = status_labels.get(status, status or "processing")
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=f"{label}…\nThis may take several minutes.",
                )
            except Exception:
                pass

        if VIDEO_BACKEND == "seedance":
            result_url, task_cost, final_task = await sd.generate_video(
                mode,
                model,
                prompt,
                image_urls=image_urls or None,
                video_urls=video_urls or None,
                audio_urls=audio_urls or None,
                on_task_created=on_task_created,
                on_status=on_status,
            )
        elif VIDEO_BACKEND == "comfy":
            # For the first iteration we only support:
            # - t2v: prompt only
            # - i2v/r2v: use the first image as the start frame
            result_url, task_cost, final_task = await cw.generate_video(
                mode,
                prompt,
                image_urls=image_urls or None,
                on_task_created=on_task_created,
                on_status=on_status,
            )
        elif VIDEO_BACKEND == "comfy_poll":
            # Бот создаёт job в БД, Windows worker забирает job и обновляет статус.
            worker_mode_id = "t2v" if mode.id == "t2v" else "i2v"
            image_url_for_worker = image_urls[0] if worker_mode_id != "t2v" else None
            if worker_mode_id != "t2v" and not image_url_for_worker:
                raise cw.ComfyWorkerError("comfy_poll: missing image_url for i2v/r2v.")

            poll_job_id = uuid.uuid4().hex
            task_cost = COMFY_WORKER_TASK_CREDITS_COST

            await db.create_job(
                job_id=poll_job_id,
                status="queued",
                mode_id=worker_mode_id,
                prompt=prompt,
                image_url=image_url_for_worker,
                chat_id=chat_id,
                locked_until=0,
                aspect_ratio=aspect_ratio,
                megapixels=megapixels,
                duration_sec=None,
            )

            # Стартовое сообщение (как у других бэкендов)
            await on_task_created(poll_job_id, task_cost)

            started_at = time.time()
            job_timeout = COMFY_WORKER_JOB_TIMEOUT_SEC
            poll_interval = COMFY_WORKER_JOB_POLL_INTERVAL_SEC
            last_status: str | None = None

            while True:
                if time.time() - started_at > job_timeout:
                    raise cw.ComfyWorkerError(
                        f"comfy_poll timeout after {int(job_timeout)}s."
                    )

                job = await db.get_job(poll_job_id)
                if not job:
                    raise cw.ComfyWorkerError("comfy_poll: job disappeared.")

                status = str(job.get("status") or "")
                if status != last_status:
                    await on_status(status, job)
                    last_status = status

                if status == "completed":
                    result_url = job.get("result_url")
                    if not isinstance(result_url, str) or not result_url:
                        raise cw.ComfyWorkerError("comfy_poll: completed but missing result_url.")
                    final_task = job
                    break

                if status == "failed":
                    err = job.get("error") or "Unknown worker error"
                    raise cw.ComfyWorkerError(str(err))

                await asyncio.sleep(poll_interval)
        else:
            raise sd.SeedanceError(f"Unknown VIDEO_BACKEND: {VIDEO_BACKEND}")

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text="Downloading video…",
            )
        except Exception:
            pass

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=240)
        ) as session:
            async with session.get(result_url) as resp:
                if resp.status >= 400:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_message_id,
                        text=f"Generation finished but download failed.\n{result_url}",
                    )
                    return
                data = await resp.read()

        await bot.send_video(
            chat_id,
            video=BufferedInputFile(data, filename="generation.mp4"),
            caption=f"{mode_label(mode.id)}\n{_format_credits(await db.deduct_user_credits(user_id, task_cost))}",
            reply_markup=home_kb,
        )
        if poll_job_id and VIDEO_BACKEND == "comfy_poll":
            try:
                await db.delete_job(poll_job_id)
            except Exception:
                pass
        uploaded_urls = image_urls + video_urls + audio_urls
        if uploaded_urls:
            await media_upload.delete_files(uploaded_urls)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=status_message_id)
        except Exception:
            pass
    except (sd.SeedanceError, cw.ComfyWorkerError, media_upload.MediaUploadError) as e:
        logger.warning("Seedance job failed: %s", e)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=str(e),
            )
        except Exception:
            await bot.send_message(chat_id, str(e), reply_markup=home_kb)
        else:
            await bot.send_message(chat_id, "Choose an action:", reply_markup=home_kb)
    except Exception as e:
        logger.exception("Unexpected generation error")
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=f"Error: {e}",
            )
        except Exception:
            await bot.send_message(chat_id, f"Error: {e}", reply_markup=home_kb)
        else:
            await bot.send_message(chat_id, "Choose an action:", reply_markup=home_kb)
    finally:
        _active_gen_jobs.discard(user_id)
        if poll_job_id and VIDEO_BACKEND == "comfy_poll":
            try:
                await db.delete_job(poll_job_id)
            except Exception:
                pass
        _cleanup_paths(image_paths, video_paths, audio_paths)


# ── Help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Commands:</b>\n\n"
        "/start — Get started\n"
        "/connect — Connect CityAds (client_id + client_secret)\n"
        f"/generate — Generate video ({VIDEO_BACKEND})\n"
        "/disconnect — Disconnect & delete data\n"
        "/menu — Main menu\n"
        "/help — This help\n\n"
        "<b>How it works:</b>\n"
        "1. /connect — enter client_id + client_secret (OAuth 2.0)\n"
        f"2. Generate — video via backend ({VIDEO_BACKEND}) (text / image / scenario)\n"
        "3. My offers — search by ID or name, get tracking links\n"
        "4. Create deeplink — search by ID or name, then enter target URL",
        parse_mode="HTML",
    )


# ── Entry point ──────────────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand(command="start", description="Get started"),
    BotCommand(command="menu", description="Open main menu"),
    BotCommand(command="generate", description="Generate video: text, image, or scenario"),
    BotCommand(command="connect", description="Connect CityAds account"),
    BotCommand(command="disconnect", description="Unlink CityAds account"),
    BotCommand(command="help", description="Show help"),
]


async def main():
    await db.init_db()
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code="ru")
    except Exception:
        logger.exception("Failed to clear Russian bot commands")
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
    dp = Dispatcher()
    dp.include_router(scenario_flow.router)
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
