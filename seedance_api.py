"""Seedance 2.0 REST API client: create task, poll status, download result."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from config import SEEDANCE_API_BASE, SEEDANCE_API_KEY, seedance_configured
from seedance_models import ModelSpec, ModeSpec, build_input

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=30, sock_read=90)


class SeedanceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        available: int | None = None,
        required: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.available = available
        self.required = required


def extract_remaining_credits(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "available",
        "available_credits",
        "credits_remaining",
        "remaining_credits",
        "balance",
    ):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _auth_headers() -> dict[str, str]:
    if not seedance_configured():
        raise SeedanceError(
            "Seedance API is not configured. Set SEEDANCE_API_KEY in .env"
        )
    return {
        "Authorization": f"Bearer {SEEDANCE_API_KEY}",
        "Content-Type": "application/json",
    }


def _parse_api_error(status: int, body: Any) -> SeedanceError:
    if isinstance(body, dict):
        err = body.get("error") or {}
        code = err.get("code", "unknown")
        message = err.get("message", f"HTTP {status}")
        if code == "insufficient_credits":
            required = err.get("required")
            available = err.get("available")
            if required is not None and available is not None:
                message = (
                    f"Not enough credits on the API account "
                    f"(required {required}, available {available})."
                )
            else:
                message = "Not enough credits on the API account."
            return SeedanceError(
                message,
                code=code,
                available=int(available) if available is not None else None,
                required=int(required) if required is not None else None,
            )
        elif code == "invalid_api_key":
            message = "Invalid Seedance API key."
        elif code == "rate_limited":
            retry = err.get("retry_after")
            if retry:
                message = f"Rate limit exceeded. Retry in {retry} sec."
        return SeedanceError(message, code=code if isinstance(code, str) else None)
    return SeedanceError(f"Seedance API error (HTTP {status}).")


async def create_video_task(
    session: aiohttp.ClientSession,
    model: ModelSpec,
    input_payload: dict[str, Any],
) -> tuple[str, int]:
    url = f"{SEEDANCE_API_BASE}/v1/videos/generations"
    body = {"model": model.api_model, "input": input_payload}
    async with session.post(url, json=body, headers=_auth_headers()) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 400:
            raise _parse_api_error(resp.status, data)
    task_id = data.get("taskId")
    if not task_id:
        raise SeedanceError("Seedance did not return a task ID.")
    credits = int(data.get("credits") or 0)
    return str(task_id), credits


async def get_task_status(
    session: aiohttp.ClientSession,
    task_id: str,
) -> dict[str, Any]:
    url = f"{SEEDANCE_API_BASE}/v1/tasks/{task_id}"
    async with session.get(url, headers=_auth_headers()) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 400:
            raise _parse_api_error(resp.status, data)
    return data


async def poll_task(
    session: aiohttp.ClientSession,
    task_id: str,
    *,
    interval_sec: int = POLL_INTERVAL_SEC,
    timeout_sec: int = 900,
    on_status: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        task = await get_task_status(session, task_id)
        status = task.get("status", "")
        logger.info("Seedance task %s status=%s", task_id, status or "unknown")
        if on_status:
            await on_status(status, task)
        if status == "completed":
            results = (task.get("data") or {}).get("results") or []
            if results and isinstance(results[0], str):
                return results[0], task
            raise SeedanceError("Generation completed but no video URL was returned.")
        if status == "failed":
            reason = task.get("failed_reason") or "provider_failed"
            raise SeedanceError(f"Generation failed: {reason}")
        await asyncio.sleep(interval_sec)
    raise SeedanceError(f"Generation timed out after {timeout_sec // 60} min.")


async def generate_video(
    mode: ModeSpec,
    model: ModelSpec,
    prompt: str,
    *,
    image_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
    on_task_created: Any | None = None,
    on_status: Any | None = None,
) -> tuple[str, int, dict[str, Any]]:
    """Create task, poll until done, return (video_url, credits, final_task)."""
    input_payload = build_input(
        mode,
        model,
        prompt,
        image_urls=image_urls,
        video_urls=video_urls,
        audio_urls=audio_urls,
    )
    logger.info(
        "Seedance generate mode=%s model=%s type=%s",
        mode.id,
        model.api_model,
        mode.generation_type,
    )
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        task_id, credits = await create_video_task(session, model, input_payload)
        if on_task_created:
            await on_task_created(task_id, credits)
        video_url, final_task = await poll_task(
            session,
            task_id,
            timeout_sec=model.timeout_sec,
            on_status=on_status,
        )
    return video_url, credits, final_task
