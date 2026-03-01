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
    waiting_remote_auth = State()


class GetLinksFlow(StatesGroup):
    waiting_offer_id = State()


class DeeplinkFlow(StatesGroup):
    waiting_offer_id = State()
    waiting_url = State()


# ── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 My offers", callback_data="my_offers")],
        [InlineKeyboardButton(text="🔗 Get tracking links", callback_data="get_links")],
        [InlineKeyboardButton(text="🌐 Create deeplink", callback_data="make_deeplink")],
        [InlineKeyboardButton(text="⚙️ Disconnect", callback_data="disconnect")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main menu", callback_data="main_menu")],
    ])


def _offers_kb(items: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for o in items[:30]:
        oid = o.get("id", "")
        name = o.get("name", o.get("translated_name", f"Offer {oid}"))
        if len(name) > 40:
            name = name[:37] + "…"
        rows.append([InlineKeyboardButton(
            text=f"{name} ({oid})", callback_data=f"{prefix}:{oid}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            "• <b>client_id</b> + <b>client_secret</b> (OAuth 2.0)\n"
            "• <b>remote_auth</b> key\n\n"
            "All from: https://cityads.com/publisher/api",
            parse_mode="HTML",
        )


# ── /connect (3 steps: client_id → client_secret → remote_auth) ─────────────

@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    await state.set_state(ConnectFlow.waiting_client_id)
    await message.answer(
        "🔐 Step 1/3: Send your <b>client_id</b>.\n\n"
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
        "Step 2/3: Send your <b>client_secret</b>.",
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

    await state.update_data(
        client_secret=client_secret, oauth_token=token, oauth_expires_in=expires_in
    )
    await state.set_state(ConnectFlow.waiting_remote_auth)
    await status_msg.edit_text(
        "✅ OAuth verified!\n\n"
        "Step 3/3: Send your <b>remote_auth</b> key.\n\n"
        "Find it on the same page:\nhttps://cityads.com/publisher/api",
        parse_mode="HTML",
    )


@router.message(ConnectFlow.waiting_remote_auth)
async def process_remote_auth(message: Message, state: FSMContext):
    remote_auth = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer("⏳ Verifying remote_auth...")

    try:
        import aiohttp
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            url = "https://cityads.com/api/rest/webmaster/xml/profile"
            async with session.get(url, params={"remote_auth": remote_auth}) as resp:
                if resp.status == 403:
                    raise RuntimeError("Invalid remote_auth key")
                if resp.status >= 400:
                    raise RuntimeError(f"API error ({resp.status})")
    except RuntimeError as e:
        await status_msg.edit_text(
            f"❌ remote_auth check failed:\n<code>{e}</code>\n\nTry again: /connect",
            parse_mode="HTML",
        )
        await state.clear()
        return
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Connection error:\n<code>{e}</code>\n\nTry again: /connect",
            parse_mode="HTML",
        )
        await state.clear()
        return

    data = await state.get_data()
    uid = message.from_user.id
    await db.save_credentials(uid, data["client_id"], data["client_secret"], remote_auth)
    await db.save_token(uid, data["oauth_token"], time.time() + data["oauth_expires_in"])
    await state.clear()

    await status_msg.edit_text(
        "✅ CityAds account fully connected!\n\n"
        "• OAuth 2.0 — for offers list\n"
        "• remote_auth — for tracking links\n\n"
        "All credentials encrypted. Messages deleted.",
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


# ── My offers (v2 API) ──────────────────────────────────────────────────────

@router.callback_query(F.data == "my_offers")
async def cb_my_offers(callback: CallbackQuery):
    if not await require_auth(callback):
        return
    await callback.answer()
    try:
        items = await api.get_my_offers(callback.from_user.id)
        if not items:
            await callback.message.edit_text("No connected offers.", reply_markup=back_kb())
            return

        text = "📋 <b>Your offers:</b>\n\n"
        for i, o in enumerate(items[:20], 1):
            name = o.get("name", "—")
            oid = o.get("id", "")
            site = o.get("site_url", "")
            dl = "✅" if o.get("is_deeplink_enabled") == "1" else "❌"
            text += f"{i}. <b>{name}</b> (ID: {oid})\n"
            if site:
                text += f"   🌐 {site}\n"
            text += f"   Deeplink: {dl}\n\n"

        await callback.message.edit_text(text[:4000], parse_mode="HTML", reply_markup=back_kb())
    except Exception as e:
        await callback.message.edit_text(f"❌ Error: {e}", reply_markup=back_kb())


# ── Get tracking links (enter offer ID → API response) ──────────────────────

@router.callback_query(F.data == "get_links")
async def cb_get_links(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()
    await state.set_state(GetLinksFlow.waiting_offer_id)
    await callback.message.edit_text(
        "🔗 <b>Get tracking links</b>\n\n"
        "Send the <b>Offer ID</b>.",
        parse_mode="HTML",
    )


@router.message(GetLinksFlow.waiting_offer_id)
async def process_get_links(message: Message, state: FSMContext):
    offer_id = message.text.strip()
    if not offer_id.isdigit():
        await message.answer("⚠️ Offer ID should be a number. Try again.")
        return

    await state.clear()
    uid = message.from_user.id
    status_msg = await message.answer("⏳ Fetching links...")

    try:
        offer = await api.get_offer(uid, offer_id)
        offer_name = offer.get("name", offer.get("translated_name", ""))

        links = await api.get_offer_links(uid, offer_id)
        if not links:
            await status_msg.edit_text(
                f"No links found for offer {offer_id}.", reply_markup=back_kb()
            )
            return

        header = f"🔗 <b>{offer_name}</b>\n\n" if offer_name else ""
        text = header
        for i, link in enumerate(links, 1):
            title = link.get("title", f"Link {i}")
            deep_link = link.get("deep_link", "—")
            is_default = link.get("is_default", False)
            star = "⭐ " if is_default else ""
            text += f"{star}<b>{title}</b> — <code>{deep_link}</code>\n\n"

        await status_msg.edit_text(text[:4000], parse_mode="HTML", reply_markup=back_kb())
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}", reply_markup=back_kb())


# ── Create deeplink (enter offer ID → get base link → enter URL) ────────────

@router.callback_query(F.data == "make_deeplink")
async def cb_make_deeplink(callback: CallbackQuery, state: FSMContext):
    if not await require_auth(callback):
        return
    await callback.answer()
    await state.set_state(DeeplinkFlow.waiting_offer_id)
    await callback.message.edit_text(
        "🌐 <b>Create deeplink</b>\n\n"
        "Send the <b>Offer ID</b>.",
        parse_mode="HTML",
    )


@router.message(DeeplinkFlow.waiting_offer_id)
async def process_dl_offer_id(message: Message, state: FSMContext):
    offer_id = message.text.strip()
    if not offer_id.isdigit():
        await message.answer("⚠️ Offer ID should be a number. Try again.")
        return

    uid = message.from_user.id
    status_msg = await message.answer("⏳ Fetching offer links...")

    try:
        links = await api.get_offer_links(uid, offer_id)
        if not links:
            await status_msg.edit_text(
                f"No links for offer {offer_id}.", reply_markup=back_kb()
            )
            await state.clear()
            return

        default_link = next((l for l in links if l.get("is_default")), links[0])
        base = default_link.get("deep_link", "")
        if not base:
            await status_msg.edit_text("❌ No base link found.", reply_markup=back_kb())
            await state.clear()
            return

        await state.set_state(DeeplinkFlow.waiting_url)
        await state.update_data(dl_base_link=base)
        await status_msg.edit_text(
            f"✅ Base link: <code>{base}</code>\n\n"
            "Now send the <b>target URL</b> for the deeplink.",
            parse_mode="HTML",
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}", reply_markup=back_kb())
        await state.clear()


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
        "/connect — Connect CityAds\n"
        "/disconnect — Disconnect & delete data\n"
        "/menu — Main menu\n"
        "/help — This help\n\n"
        "<b>How it works:</b>\n"
        "1. /connect (client_id + client_secret + remote_auth)\n"
        "2. 📋 My offers — browse your offers (v2 API)\n"
        "3. 🔗 Get tracking links — from CityAds API (v1)\n"
        "4. 🌐 Create deeplink — base link + target URL",
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
