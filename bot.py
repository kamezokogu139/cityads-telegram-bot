import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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
import seedance_api as sd
from config import BOT_TOKEN, SEEDANCE_INITIAL_CREDITS, media_upload_configured, seedance_configured
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# One active generation job per telegram user
_active_gen_jobs: set[int] = set()
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


# ── Keyboards ────────────────────────────────────────────────────────────────

BTN_CONNECT = "Connect"
BTN_GENERATE = "Generate"
BTN_MY_OFFERS = "My available offers"
BTN_CREATE_DEEPLINK = "Create deeplink"
BTN_SETTINGS = "Settings"
BTN_BACK = "Back"
BTN_HELP = "Help"
BTN_DISCONNECT = "Disconnect"

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
        [InlineKeyboardButton(text="Main menu", callback_data="main_menu")],
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
@router.message(F.text == BTN_CONNECT)
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

@router.message(F.text == BTN_BACK)
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

@router.message(F.text == BTN_SETTINGS)
async def msg_settings(message: Message):
    if not await require_auth(message):
        return
    await message.answer(
        "<b>Settings</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


@router.message(F.text == BTN_HELP)
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
        "• Generate — video via Seedance 2.0\n"
        "• My available offers — search by ID or name, get tracking links\n"
        "• Create deeplink — search offer, enter target URL, get deeplink",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


@router.message(F.text == BTN_DISCONNECT)
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


@router.message(F.text == BTN_MY_OFFERS)
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
        [InlineKeyboardButton(text="Main menu", callback_data="main_menu")],
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


@router.message(F.text == BTN_CREATE_DEEPLINK)
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
        [InlineKeyboardButton(text="Main menu", callback_data="main_menu")],
    ])


async def _dl_set_base_and_ask_url(status_msg: Message, state: FSMContext, base: str):
    await state.set_state(DeeplinkFlow.waiting_url)
    await state.update_data(dl_base_link=base)
    await status_msg.edit_text(
        f"Base link: <code>{base}</code>\n\n"
        "Now send the <b>target URL</b> for the deeplink.",
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
    url = (message.text or "").strip()
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
        f"<b>Your deeplink:</b>\n\n<code>{result}</code>",
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


def _mode_needs_upload(mode_id: str) -> bool:
    return mode_id in ("i2v", "r2v")


async def _ensure_seedance_credits_tracked():
    if await db.get_seedance_credits() is not None:
        return
    if SEEDANCE_INITIAL_CREDITS.isdigit():
        await db.set_seedance_credits(int(SEEDANCE_INITIAL_CREDITS))


async def _resolve_remaining_credits(*, cost: int, final_task: dict) -> int | None:
    from_api = sd.extract_remaining_credits(final_task)
    if from_api is not None:
        await db.set_seedance_credits(from_api)
        return from_api
    await _ensure_seedance_credits_tracked()
    return await db.adjust_seedance_credits(-cost)


async def _begin_data_collection(target: Message, state: FSMContext, mode_id: str):
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
@router.message(F.text == BTN_GENERATE)
async def cmd_generate(message: Message, state: FSMContext):
    if message.from_user.id in _active_gen_jobs:
        await message.answer("A generation is already running. Please wait.")
        return
    if not seedance_configured():
        await message.answer(
            "Seedance API is not configured on the server.\n"
            "Ask the admin to set SEEDANCE_API_KEY in .env"
        )
        return
    await state.clear()
    await state.set_state(GenerateFlow.choosing_mode)
    await message.answer(
        format_modes_menu(),
        parse_mode="HTML",
        reply_markup=_modes_inline_kb(),
    )


@router.callback_query(F.data == "gen:cancel")
async def cb_gen_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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
            "Send a mode number (1-3).\n\n" + format_modes_menu(),
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
    await _begin_data_collection(message, state, mode)


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
    await _begin_data_collection(callback.message, state, mode_id)


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
    await message.answer(f"Image saved ({len(paths)}). Send more or press Done.")


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

    if user_id in _active_gen_jobs:
        await message.answer("A generation is already running. Please wait.")
        return

    mode = get_mode(data.get("gen_mode_id", ""))
    model = get_model(data.get("gen_model_id", DEFAULT_MODEL_ID))
    prompt = (data.get("gen_prompt") or "").strip()
    await state.clear()

    if not mode or not model or not prompt:
        _cleanup_paths(image_paths, video_paths, audio_paths)
        await message.answer(
            "Session expired. Start again with Generate.",
            reply_markup=await _reply_home_kb(user_id),
        )
        return

    if mode.id == "i2v" and not image_paths:
        _cleanup_paths(image_paths, video_paths, audio_paths)
        await message.answer(
            "Image to Video requires a text prompt and at least one image.",
            reply_markup=await _reply_home_kb(user_id),
        )
        return

    if mode.id == "r2v" and not image_paths and not video_paths:
        _cleanup_paths(image_paths, video_paths, audio_paths)
        await message.answer(
            "Reference to Video requires a text prompt and at least one image or video.",
            reply_markup=await _reply_home_kb(user_id),
        )
        return

    _active_gen_jobs.add(user_id)
    status_msg = await message.answer("Generation in progress.")
    if from_callback:
        try:
            await status_msg.edit_text("Generation in progress.")
        except Exception:
            pass

    asyncio.create_task(
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
        )
    )


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
):
    mode = get_mode(mode_id)
    model = get_model(model_id)
    home_kb = await _reply_home_kb(user_id)
    try:
        if not mode or not model:
            raise sd.SeedanceError("Unknown mode or model.")

        image_urls: list[str] = []
        for path in image_paths:
            image_urls.append(await media_upload.upload_file(path))

        video_urls: list[str] = []
        for path in video_paths:
            video_urls.append(await media_upload.upload_file(path))

        audio_urls: list[str] = []
        for path in audio_paths:
            audio_urls.append(await media_upload.upload_file(path))

        async def on_task_created(_task_id: str, cost: int):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=f"Generation in progress. Cost: {cost} credits.",
                )
            except Exception:
                pass

        result_url, task_cost, final_task = await sd.generate_video(
            mode,
            model,
            prompt,
            image_urls=image_urls or None,
            video_urls=video_urls or None,
            audio_urls=audio_urls or None,
            on_task_created=on_task_created,
        )

        async with aiohttp.ClientSession() as session:
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
            caption=f"{mode_label(mode.id)}",
            reply_markup=home_kb,
        )
        remaining = await _resolve_remaining_credits(cost=task_cost, final_task=final_task)
        if remaining is not None:
            await bot.send_message(
                chat_id,
                f"Credits remaining: {remaining}",
                reply_markup=home_kb,
            )
        try:
            await bot.delete_message(chat_id=chat_id, message_id=status_message_id)
        except Exception:
            pass
    except (sd.SeedanceError, media_upload.MediaUploadError) as e:
        logger.warning("Seedance job failed: %s", e)
        if isinstance(e, sd.SeedanceError) and e.available is not None:
            await db.set_seedance_credits(e.available)
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
        _cleanup_paths(image_paths, video_paths, audio_paths)


# ── Help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Commands:</b>\n\n"
        "/start — Get started\n"
        "/connect — Connect CityAds (client_id + client_secret)\n"
        "/generate — Generate video (Seedance 2.0)\n"
        "/disconnect — Disconnect & delete data\n"
        "/menu — Main menu\n"
        "/help — This help\n\n"
        "<b>How it works:</b>\n"
        "1. /connect — enter client_id + client_secret (OAuth 2.0)\n"
        "2. Generate — Seedance 2.0 video (text/image/reference)\n"
        "3. My offers — search by ID or name, get tracking links\n"
        "4. Create deeplink — search by ID or name, then enter target URL",
        parse_mode="HTML",
    )


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    await db.init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
