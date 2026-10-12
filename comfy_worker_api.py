"""ComfyUI local worker client.

This module is used by the VPS bot to offload video generation to a
Windows-laptop service which:
1) runs ComfyUI workflows locally
2) uploads generated mp4 to the same S3/R2 bucket
3) returns `result_url` back to the bot
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from config import (
    COMFY_WORKER_API_TOKEN,
    COMFY_WORKER_BASE_URL,
    COMFY_WORKER_JOB_POLL_INTERVAL_SEC,
    COMFY_WORKER_JOB_TIMEOUT_SEC,
    COMFY_WORKER_TASK_CREDITS_COST,
)
from seedance_models import ModeSpec


class ComfyWorkerError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {"X-Worker-Auth": COMFY_WORKER_API_TOKEN}


async def _create_job(
    session: aiohttp.ClientSession,
    *,
    mode_id: str,
    prompt: str,
    image_url: str | None,
) -> str:
    url = f"{COMFY_WORKER_BASE_URL}/api/jobs"
    payload: dict[str, Any] = {
        "mode_id": mode_id,
        "prompt": prompt,
        "image_url": image_url,
    }
    async with session.post(url, json=payload, headers=_headers()) as resp:
        body = await resp.text()
        if resp.status >= 400:
            raise ComfyWorkerError(f"Worker create job failed ({resp.status}): {body[:300]}")
        data = await resp.json()
        job_id = data.get("job_id")
        if not isinstance(job_id, str):
            raise ComfyWorkerError("Worker create job: missing job_id in response.")
        return job_id


async def _get_job(session: aiohttp.ClientSession, job_id: str) -> dict[str, Any]:
    url = f"{COMFY_WORKER_BASE_URL}/api/jobs/{job_id}"
    async with session.get(url, headers=_headers()) as resp:
        body = await resp.text()
        if resp.status >= 400:
            raise ComfyWorkerError(f"Worker get job failed ({resp.status}): {body[:300]}")
        data = await resp.json()
        if not isinstance(data, dict):
            raise ComfyWorkerError("Worker get job: invalid JSON response.")
        return data


def _pick_input_image_url(mode: ModeSpec, image_urls: list[str]) -> str | None:
    # For our first iteration we only support:
    # - t2v: image_url=None
    # - i2v/r2v: use the first image_url as "start frame"
    if mode.id == "t2v":
        return None
    if not image_urls:
        return None
    return image_urls[0]


async def generate_video(
    mode: ModeSpec,
    prompt: str,
    *,
    image_urls: list[str] | None = None,
    on_task_created=None,
    on_status=None,
) -> tuple[str, int, dict[str, Any]]:
    """Create worker job, poll until done, return (result_url, task_cost, final_task)."""

    image_urls = list(image_urls or [])
    image_url = _pick_input_image_url(mode, image_urls)

    worker_mode_id = "t2v" if mode.id == "t2v" else "i2v"
    if worker_mode_id != "t2v" and not image_url:
        raise ComfyWorkerError("Comfy worker: missing image_url for i2v/r2v mode.")

    timeout = aiohttp.ClientTimeout(total=120)
    poll_interval = COMFY_WORKER_JOB_POLL_INTERVAL_SEC
    job_timeout = COMFY_WORKER_JOB_TIMEOUT_SEC

    async with aiohttp.ClientSession(timeout=timeout) as session:
        job_id = await _create_job(
            session,
            mode_id=worker_mode_id,
            prompt=prompt,
            image_url=image_url,
        )

        if on_task_created:
            await on_task_created(job_id, COMFY_WORKER_TASK_CREDITS_COST)

        started_at = time.time()
        last_status = None
        while True:
            if time.time() - started_at > job_timeout:
                raise ComfyWorkerError(f"Comfy worker timeout after {int(job_timeout)}s.")

            task = await _get_job(session, job_id)
            status = str(task.get("status") or "")
            if status and status != last_status and on_status:
                await on_status(status, task)
            last_status = status

            if status == "completed":
                result_url = task.get("result_url")
                if not isinstance(result_url, str) or not result_url:
                    raise ComfyWorkerError("Comfy worker completed but result_url is missing.")
                return result_url, COMFY_WORKER_TASK_CREDITS_COST, task

            if status == "failed":
                err = task.get("error") or "Unknown worker error"
                raise ComfyWorkerError(str(err))

            await asyncio.sleep(poll_interval)

