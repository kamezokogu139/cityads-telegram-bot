import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import cityads_api as api
import db
from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


# ── FSM states ───────────────────────────────────────────────────────────────

class ConnectFlow(StatesGroup):
    waiting_client_id = State()
    waiting_client_secret = State()


class GetLinksFlow(StatesGroup):
    waiting_search = State()


class DeeplinkFlow(StatesGroup):
    waiting_offer_id = State()
    waiting_url = State()


# ── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 My offers", callback_data="my_offers")],
        [InlineKeyboardButton(text="🌐 Create deeplink", callback_data="make_deeplink")],
        [InlineKeyboardButton(text="⚙️ Disconnect", callback_data="disconnect")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


# ── Auth helper ──────────────────────────────────────────────────────────────

async def require_auth(event: Message | CallbackQuery) -> bool:
    uid = event.from_user.id
    if await db.is_connected(uid):
        return True
    text = "⚠️ Account not connected.\nUse /connect to link your CityAds account."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


# ── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    if await db.is_connected(message.from_user.id):
        await message.answer("👋 Welcome back!", reply_markup=main_menu_kb())
    else:
        await message.answer(
            "👋 Hi! I'm a CityAds link bot.\n\n"
            "/connect — link your CityAds account\n\n"
            "You'll need:\n"
            "• <b>client_id</b>\n"
            "• <b>client_secret</b>\n\n"
            "Get them at: https://cityads.com/publisher/api",
            parse_mode="HTML",
        )


# ── /connect (2 steps: client_id → client_secret) ───────────────────────────

@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    await state.set_state(ConnectFlow.waiting_client_id)
    await message.answer(
        "🔐 Step 1/2: Send your <b>client_id</b>.\n\n"
        "https://cityads.com/publisher/api",
        parse_mode="HTML",
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
        "✅ Got client_id (message deleted).\n\n"
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
    status_msg = await message.answer("⏳ Verifying OAuth credentials...")

    try:
        token, expires_in = await api._fetch_new_token(client_id, client_secret)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ OAuth failed:\n<code>{e}</code>\n\nTry again: /connect",
            parse_mode="HTML",
        )
        await state.clear()
        return

    uid = message.from_user.id
    await db.save_credentials(uid, client_id, client_secret)
    await db.save_token(uid, token, time.time() + expires_in)
    await state.clear()

    await status_msg.edit_text(
        "✅ CityAds account connected!\n\n"
        "OAuth 2.0 credentials verified and encrypted.\n"
        "Messages with secrets deleted.",
    )
    await message.answer("Choose an action:", reply_markup=main_menu_kb())


# ── Disconnect ───────────────────────────────────────────────────────────────

@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message):
    await db.delete_user(message.from_user.id)
    await message.answer("🗑 Disconnected. All data deleted.\n/connect — connect again.")


@router.callback_query(F.data == "disconnect")
async def cb_disconnect(callback: CallbackQuery):
    await db.delete_user(callback.from_user.id)
    await callback.message.edit_text("🗑 Disconnected.\n/connect — connect again.")


# ── Main menu ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Choose an action:", reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not await require_auth(message):
        return
    await message.answer("Choose an action:", reply_markup=main_menu_kb())


# ── My offers: search by ID or name → select → show tracking links ───────────

@router.callback_query(F.data.in_({"my_offers", "search_again"}))
async def cb_my_offers(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()
    await state.set_state(GetLinksFlow.waiting_search)
    await callback.message.edit_text(
        "📋 <b>My offers</b>\n\n"
        "Enter the <b>offer ID</b> or <b>name</b> (or part of it).",
        parse_mode="HTML",
    )


@router.message(GetLinksFlow.waiting_search)
async def process_search_offers(message: Message, state: FSMContext):
    query = message.text.strip()
    uid = message.from_user.id

    if query.isdigit():
        await state.clear()
        status_msg = await message.answer("⏳ Fetching links...")
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
            await status_msg.edit_text(f"❌ Error: {e}", reply_markup=back_kb())
        return

    if len(query) < 2:
        await message.answer("⚠️ Enter at least 2 characters.")
        return

    await state.clear()
    status_msg = await message.answer("⏳ Searching...")

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
        rows.append([InlineKeyboardButton(text="🔍 Search again", callback_data="search_again")])
        rows.append([InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")])

        await status_msg.edit_text(
            f"🔍 Results for <b>{query}</b>:\n\nSelect an offer to get tracking links.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}", reply_markup=back_kb())


@router.callback_query(F.data.startswith("links:"))
async def cb_show_links(callback: CallbackQuery):
    if not await require_auth(callback):
        return
    await callback.answer()

    offer_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    await callback.message.edit_text("⏳ Fetching links...")

    try:
        offer = await api.get_offer_with_links(uid, offer_id)
        if not offer:
            await callback.message.edit_text(
                f"No offer found with ID {offer_id}.", reply_markup=_search_back_kb()
            )
            return
        await _send_offer_links(callback.message, offer, offer_id)
    except Exception as e:
        await callback.message.edit_text(f"❌ Error: {e}", reply_markup=back_kb())


def _search_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Search again", callback_data="search_again")],
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

    header = f"🔗 <b>{offer_name}</b> (ID: {offer_id})\n\n" if offer_name else ""
    text = header
    shown = 0
    for i, link in enumerate(links, 1):
        name = link.get("name", f"Link {i}")
        deep_link = link.get("deep_link", "—")
        is_default = link.get("is_default", False)
        star = "⭐ " if is_default else ""
        line = f"{star}<b>{name}</b> — <code>{deep_link}</code>\n\n"
        if len(text) + len(line) > 3900:
            text += f"… and {len(links) - shown} more links"
            break
        text += line
        shown += 1

    await msg.edit_text(text, parse_mode="HTML", reply_markup=_search_back_kb())


# ── Create deeplink (enter offer ID → get base link → enter URL) ────────────

@router.callback_query(F.data.in_({"make_deeplink", "dl_search_again"}))
async def cb_make_deeplink(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()
    await state.set_state(DeeplinkFlow.waiting_offer_id)
    await callback.message.edit_text(
        "🌐 <b>Create deeplink</b>\n\n"
        "Enter the <b>offer ID</b> or <b>name</b> (or part of it).",
        parse_mode="HTML",
    )


def _deeplink_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Search again", callback_data="dl_search_again")],
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


async def _dl_set_base_and_ask_url(status_msg: Message, state: FSMContext, base: str):
    await state.set_state(DeeplinkFlow.waiting_url)
    await state.update_data(dl_base_link=base)
    await status_msg.edit_text(
        f"✅ Base link: <code>{base}</code>\n\n"
        "Now send the <b>target URL</b> for the deeplink.",
        parse_mode="HTML",
    )


@router.message(DeeplinkFlow.waiting_offer_id)
async def process_dl_offer_id(message: Message, state: FSMContext):
    query = message.text.strip()
    uid = message.from_user.id

    if query.isdigit():
        status_msg = await message.answer("⏳ Fetching offer links...")
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
                await status_msg.edit_text("❌ No base link found.", reply_markup=_deeplink_back_kb())
                return

            await _dl_set_base_and_ask_url(status_msg, state, base)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {e}", reply_markup=_deeplink_back_kb())
            await state.clear()
        return

    if len(query) < 2:
        await message.answer("⚠️ Enter at least 2 characters.")
        return

    await state.clear()
    status_msg = await message.answer("⏳ Searching...")

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
        rows.append([InlineKeyboardButton(text="🔍 Search again", callback_data="dl_search_again")])
        rows.append([InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")])

        await status_msg.edit_text(
            f"🔍 Results for <b>{query}</b>:\n\nSelect an offer to create a deeplink.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}", reply_markup=_deeplink_back_kb())


@router.callback_query(F.data.startswith("dl_select:"))
async def cb_dl_select_offer(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()

    offer_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    await callback.message.edit_text("⏳ Fetching offer links...")

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
            await callback.message.edit_text("❌ No base link found.", reply_markup=_deeplink_back_kb())
            return

        await _dl_set_base_and_ask_url(callback.message, state, base)
    except Exception as e:
        await callback.message.edit_text(f"❌ Error: {e}", reply_markup=_deeplink_back_kb())


@router.message(DeeplinkFlow.waiting_url)
async def process_deeplink_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        await message.answer("⚠️ Send a valid URL starting with http:// or https://")
        return

    data = await state.get_data()
    base = data["dl_base_link"]
    await state.clear()

    deeplink = api.build_deeplink(base, url)
    await message.answer(
        f"✅ <b>Your deeplink:</b>\n\n<code>{deeplink}</code>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


# ── Help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Commands:</b>\n\n"
        "/start — Get started\n"
        "/connect — Connect CityAds (client_id + client_secret)\n"
        "/disconnect — Disconnect & delete data\n"
        "/menu — Main menu\n"
        "/help — This help\n\n"
        "<b>How it works:</b>\n"
        "1. /connect — enter client_id + client_secret (OAuth 2.0)\n"
        "2. 📋 My offers — search by ID or name, get tracking links\n"
        "3. 🌐 Create deeplink — search by ID or name, then enter target URL",
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
