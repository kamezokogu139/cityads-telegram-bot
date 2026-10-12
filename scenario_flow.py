"""Telegram FSM for scenario → duration → video."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid

import aiohttp
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
import media_upload
from speech_text import extract_speech
from config import (
    COMFY_WORKER_JOB_POLL_INTERVAL_SEC,
    COMFY_WORKER_JOB_TIMEOUT_SEC,
    COMFY_WORKER_TASK_CREDITS_COST,
    SEEDANCE_MONTHLY_CREDITS,
    VIDEO_BACKEND,
    media_upload_configured,
)
from seedance_models import DEFAULT_MODEL_ID, format_mode_instructions

logger = logging.getLogger(__name__)
router = Router()

DURATION_CHOICES = (3, 5, 8, 10)
MAX_DURATION_SEC = 40
MIN_DURATION_SEC = 2
DEFAULT_ANIMATE_PROMPT = (
    "Animate this image. Keep the same face, wardrobe, product, and composition. "
    "Natural motion that fits the scene, static camera, no cut, no extra people, no text overlay."
)
_album_buf: dict[str, list[Message]] = {}
_album_tasks: dict[str, asyncio.Task] = {}


class ScenarioFlow(StatesGroup):
    waiting_scenario = State()
    waiting_duration = State()


def _active_jobs() -> set[int]:
    from bot import _active_gen_jobs

    return _active_gen_jobs


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")],
    ])


def _duration_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{n} s", callback_data=f"sc:dur:{n}")
            for n in DURATION_CHOICES
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="gen:cancel")],
    ])


def _duration_text() -> str:
    return (
        "Video duration in seconds.\n"
        "Use the buttons below or send a number (2–40). "
        "Longer than 10 s will be stitched from several LTX clips."
    )


def _send_prompt_text() -> str:
    return "Send scenario text, your own image, or an image with a caption."


def resolve_speech(*parts: str) -> str:
    for part in parts:
        found = extract_speech(part or "")
        if found:
            return found
    return ""


async def begin_scenario(target: Message, state: FSMContext, user_id: int) -> None:
    if VIDEO_BACKEND != "comfy_poll":
        await target.answer("Scenario mode requires VIDEO_BACKEND=comfy_poll.")
        return
    await state.update_data(
        gen_mode_id="scenario",
        gen_model_id=DEFAULT_MODEL_ID,
        sc_scenario="",
        sc_image_url="",
    )
    await state.set_state(ScenarioFlow.waiting_scenario)
    await target.answer(
        format_mode_instructions("scenario"),
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


async def _poll_job(job_id: str, *, timeout_sec: float) -> dict:
    started = time.time()
    last_status = None
    while True:
        if time.time() - started > timeout_sec:
            raise RuntimeError(f"Job timed out ({int(timeout_sec)} s).")
        job = await db.get_job(job_id)
        if not job:
            raise RuntimeError("Job disappeared from the queue.")
        status = str(job.get("status") or "")
        if status != last_status:
            last_status = status
        if status == "completed":
            return job
        if status == "failed":
            raise RuntimeError(str(job.get("error") or "Worker error"))
        await asyncio.sleep(COMFY_WORKER_JOB_POLL_INTERVAL_SEC)


async def _queue_job(
    *,
    mode_id: str,
    prompt: str,
    chat_id: int,
    image_url: str | None = None,
    duration_sec: int | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    await db.create_job(
        job_id=job_id,
        status="queued",
        mode_id=mode_id,
        prompt=prompt,
        image_url=image_url,
        chat_id=chat_id,
        locked_until=0,
        duration_sec=duration_sec,
        aspect_ratio="9:16 (Portrait Widescreen)",
        megapixels=1.0,
    )
    return job_id


async def _ask_duration(message: Message, state: FSMContext) -> None:
    await state.set_state(ScenarioFlow.waiting_duration)
    await message.answer(_duration_text(), reply_markup=_duration_kb())


async def _save_telegram_file(bot: Bot, file_id: str, suffix: str) -> str:
    tg_file = await bot.get_file(file_id)
    fd, path = tempfile.mkstemp(prefix="sc_", suffix=suffix)
    os.close(fd)
    await bot.download_file(tg_file.file_path, destination=path)
    return path


def _image_file_id(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, ".jpg"
    doc: Document | None = message.document
    if not doc or not (doc.mime_type or "").startswith("image/"):
        return None
    name = (doc.file_name or "").lower()
    suffix = ".png" if name.endswith(".png") else ".webp" if name.endswith(".webp") else ".jpg"
    return doc.file_id, suffix


async def _first_image_url(messages: list[Message]) -> str:
    for msg in messages:
        ident = _image_file_id(msg)
        if not ident:
            continue
        file_id, suffix = ident
        path = await _save_telegram_file(msg.bot, file_id, suffix)
        try:
            return await media_upload.upload_file(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return ""


async def _apply_user_photos(message: Message, state: FSMContext, messages: list[Message]) -> None:
    if not media_upload_configured():
        await message.answer("Your own image needs S3. Send the scenario as text.")
        return
    try:
        url = await _first_image_url(messages)
    except Exception:
        logger.exception("scenario user photo upload failed")
        await message.answer("Could not save the image. Send it again or send scenario text.")
        return
    if not url:
        await message.answer("I don't see an image. Send a photo or scenario text.")
        return

    data = await state.get_data()
    caption = ""
    for msg in messages:
        caption = (msg.caption or "").strip()
        if caption:
            break
    prompt = caption or (data.get("sc_scenario") or "").strip() or DEFAULT_ANIMATE_PROMPT
    await state.update_data(
        sc_scenario=prompt,
        sc_image_url=url,
    )
    extra = " Using the first photo." if len(messages) > 1 else ""
    await message.answer(f"Got the image.{extra} The prompt and photo will complement each other.")
    await _ask_duration(message, state)


async def _collect_album(message: Message, state: FSMContext) -> None:
    group = message.media_group_id
    if not group:
        await _apply_user_photos(message, state, [message])
        return
    key = f"{message.chat.id}:{group}"
    _album_buf.setdefault(key, []).append(message)
    prev = _album_tasks.get(key)
    if prev:
        prev.cancel()

    async def _flush() -> None:
        await asyncio.sleep(0.8)
        msgs = _album_buf.pop(key, [])
        _album_tasks.pop(key, None)
        if msgs:
            await _apply_user_photos(msgs[0], state, msgs)

    _album_tasks[key] = asyncio.create_task(_flush())


@router.message(ScenarioFlow.waiting_scenario, F.text)
async def process_scenario_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer(_send_prompt_text())
        return
    await state.update_data(sc_scenario=text, sc_image_url="")
    await _ask_duration(message, state)


@router.message(ScenarioFlow.waiting_scenario, F.photo)
async def process_scenario_photo(message: Message, state: FSMContext):
    await _collect_album(message, state)


@router.message(ScenarioFlow.waiting_scenario, F.document)
async def process_scenario_document(message: Message, state: FSMContext):
    if not _image_file_id(message):
        await message.answer(_send_prompt_text())
        return
    await _collect_album(message, state)


async def _start_video(message: Message, state: FSMContext, duration_sec: int) -> None:
    data = await state.get_data()
    image_url = (data.get("sc_image_url") or "").strip()
    scenario = (data.get("sc_scenario") or "").strip()
    if not scenario:
        await state.clear()
        _active_jobs().discard(message.chat.id)
        await message.answer("Session expired. Start Generate again.")
        return

    duration_sec = max(MIN_DURATION_SEC, min(MAX_DURATION_SEC, duration_sec))
    speech = resolve_speech(scenario)
    if image_url and speech:
        mode_id = "lipsync"
    elif image_url:
        mode_id = "i2v"
    else:
        mode_id = "t2v"
    await state.clear()
    user_id = message.chat.id
    jobs = _active_jobs()
    jobs.add(user_id)
    status = await message.answer(
        "Enhancing the prompt and generating video. This may take a few minutes.",
        reply_markup=_cancel_kb(),
    )
    job_id = None
    try:
        job_id = await _queue_job(
            mode_id=mode_id,
            prompt=scenario,
            chat_id=message.chat.id,
            image_url=image_url or None,
            duration_sec=duration_sec,
        )
        timeout = max(COMFY_WORKER_JOB_TIMEOUT_SEC, 2400)
        job = await _poll_job(job_id, timeout_sec=timeout)
        result_url = job.get("result_url")
        if not isinstance(result_url, str) or not result_url:
            raise RuntimeError("Missing result_url")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=240)
        ) as session:
            async with session.get(result_url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Failed to download video: {result_url}")
                blob = await resp.read()
        remaining = await db.deduct_user_credits(user_id, COMFY_WORKER_TASK_CREDITS_COST)
        await message.answer_video(
            video=BufferedInputFile(blob, filename="scenario.mp4"),
            caption=f"Scenario\nCredits remaining: {remaining} / {SEEDANCE_MONTHLY_CREDITS}",
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.exception("scenario video failed")
        try:
            await status.edit_text(f"Video error: {e}")
        except Exception:
            await message.answer(f"Video error: {e}")
    finally:
        jobs.discard(user_id)
        if job_id:
            try:
                await db.delete_job(job_id)
            except Exception:
                pass


@router.callback_query(ScenarioFlow.waiting_duration, F.data.startswith("sc:dur:"))
async def cb_duration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    duration_sec = int(callback.data.rsplit(":", 1)[-1])
    await _start_video(callback.message, state, duration_sec)


@router.message(ScenarioFlow.waiting_duration, F.text)
async def process_duration_text(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        duration_sec = int(float(raw.split()[0]))
    except (ValueError, IndexError):
        await message.answer("Send a number of seconds (2–40) or tap a button.", reply_markup=_duration_kb())
        return
    await _start_video(message, state, duration_sec)
